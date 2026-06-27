#!/usr/bin/env python3
"""Apply search-agent decisions into next-query and capture queues."""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


ALLOWED_DECISIONS = {
    "needs_abstract_preview",
    "ready_to_capture",
    "iterate_query",
    "stop_low_yield",
}
CAPTURE_DECISIONS = {"ready_to_capture"}
ABSTRACT_CAPTURE_PHASES = {"abstract_capture_review", "abstract_review"}
DECISION_ALIASES = {
    "capture_articles": "ready_to_capture",
    "capture": "ready_to_capture",
}
STRUCTURED_PUBLISHERS = {"elsevier", "sciencedirect", "acs", "wiley", "springer"}


def clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def is_simple_keyword_query(query: str, max_words: int = 6) -> bool:
    query = clean(query)
    if not query:
        return False
    if re.search(r"\b(?:AND|OR|NOT)\b", query, flags=re.IGNORECASE):
        return False
    if any(token in query for token in "()[]{}"):
        return False
    words = re.findall(r"[A-Za-z0-9-]+|[\u4e00-\u9fff]+", query)
    return 0 < len(words) <= max_words


def numeric_rcs(value: Any) -> float | None:
    try:
        score = float(str(value).strip())
    except Exception:
        return None
    if score < 0 or score > 10:
        return None
    return score


def normalize_decision(value: Any) -> str:
    decision = clean(value)
    return DECISION_ALIASES.get(decision, decision)


def publisher_supported(value: Any) -> bool:
    return clean(value).lower() in STRUCTURED_PUBLISHERS


def group_review_phase(group: dict[str, Any], recommendations: dict[str, Any]) -> str:
    return clean(group.get("review_phase") or recommendations.get("review_phase") or "search_page_triage").lower()


def has_abstract_evidence(group: dict[str, Any]) -> bool:
    if clean(group.get("abstract_preview_status")):
        return True
    return any(clean(item.get("abstract")) or clean(item.get("abstract_source")) for item in group.get("capture_articles") or [])


def validate_phase_decision(group: dict[str, Any], recommendations: dict[str, Any]) -> None:
    phase = group_review_phase(group, recommendations)
    decision = normalize_decision(group.get("decision"))
    if phase not in ABSTRACT_CAPTURE_PHASES and (decision in CAPTURE_DECISIONS or group.get("capture_articles")):
        raise SystemExit(
            "ready_to_capture requires review_phase=abstract_capture_review after abstract preview; "
            f"group={clean(group.get('group_id')) or '<unknown>'}"
        )


def can_enqueue_capture(group: dict[str, Any], recommendations: dict[str, Any]) -> bool:
    if group_review_phase(group, recommendations) not in ABSTRACT_CAPTURE_PHASES:
        return False
    decision = normalize_decision(group.get("decision"))
    if decision not in CAPTURE_DECISIONS:
        return False
    score = numeric_rcs(group.get("rcs_0_to_10"))
    if score is None or score < 7:
        return False
    if not publisher_supported(group.get("publisher")):
        return False
    if not clean(group.get("rcs_reasoning")):
        return False
    if not has_abstract_evidence(group):
        return False
    return bool(group.get("capture_articles"))


def can_enqueue_abstract(group: dict[str, Any]) -> bool:
    if normalize_decision(group.get("decision")) != "needs_abstract_preview":
        return False
    score = numeric_rcs(group.get("rcs_0_to_10"))
    if score is None or score < 5:
        return False
    if not clean(group.get("rcs_reasoning")):
        return False
    return bool(group.get("abstract_probe_articles"))


def can_enqueue_next_queries(group: dict[str, Any]) -> bool:
    if normalize_decision(group.get("decision")) != "iterate_query":
        return False
    score = numeric_rcs(group.get("rcs_0_to_10"))
    return score is None or score <= 4


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def valid_agent_text(path: Path, *, fallback: bool = False) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    if not clean(text):
        return False
    lower = text.lower()
    if fallback:
        return "main_agent_fallback" in lower and ("fallback_reason" in lower or "reason:" in lower)
    return "review_mode" in lower and ("subagent" in lower or "agent_id" in lower or "main_agent_fallback" in lower)


def validate_agent_provenance(recommendations_path: Path, recommendations: dict[str, Any]) -> None:
    root = recommendations_path.parent
    mode = clean(recommendations.get("review_mode")).lower()
    has_subagent = mode == "subagent" and clean(recommendations.get("agent_id")) and valid_agent_text(root / "subagent-response.md")
    has_fallback = (
        mode == "main_agent_fallback"
        and clean(recommendations.get("fallback_reason"))
        and valid_agent_text(root / "main-agent-fallback.md", fallback=True)
    )
    if not (has_subagent or has_fallback):
        raise SystemExit("Missing or invalid agent provenance for query-refinement recommendations.")


def validate_group_coverage(recommendations_path: Path, recommendations: dict[str, Any]) -> None:
    input_path = recommendations_path.parent / "query-refinement-input.json"
    if not input_path.exists():
        return
    packet = load_json(input_path, {})
    expected = {
        clean(group.get("group_id"))
        for group in (packet.get("groups") or [])
        if isinstance(group, dict) and clean(group.get("group_id"))
    }
    if not expected:
        return
    actual = {
        clean(group.get("group_id"))
        for group in (recommendations.get("groups") or [])
        if isinstance(group, dict) and clean(group.get("group_id"))
    }
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        raise SystemExit(f"query-refinement recommendations do not cover original groups; missing={missing}; extra={extra}")


def latest_recommendations(run_dir: Path) -> Path:
    candidates = sorted(
        run_dir.glob("query-refinement/iteration-*/query-refinement-recommendations.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise SystemExit("No query-refinement-recommendations.json found.")
    return candidates[0]


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def article_lookup_keys(group_id: str, rank: Any, href: str, title: str) -> list[tuple[str, str, str]]:
    rank_text = clean(rank)
    href_text = clean(href).lower()
    title_text = clean(title).lower()
    keys: list[tuple[str, str, str]] = []
    if group_id and rank_text and href_text:
        keys.append((group_id, rank_text, href_text))
    if group_id and rank_text and title_text:
        keys.append((group_id, rank_text, title_text))
    if group_id and href_text:
        keys.append((group_id, "href", href_text))
    if group_id and title_text:
        keys.append((group_id, "title", title_text))
    return keys


def load_candidate_lookup(refinement_dir: Path) -> dict[tuple[str, str, str], dict[str, str]]:
    lookup: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in read_csv_rows(refinement_dir / "query-refinement-candidates.csv"):
        group_id = clean(row.get("group_id"))
        if not group_id:
            continue
        for key in article_lookup_keys(group_id, row.get("rank"), row.get("href"), row.get("title")):
            lookup.setdefault(key, row)
    return lookup


def candidate_for_item(
    lookup: dict[tuple[str, str, str], dict[str, str]],
    group_id: str,
    item: dict[str, Any],
) -> dict[str, str]:
    href = clean(item.get("href") or item.get("landing_url"))
    title = clean(item.get("title"))
    for key in article_lookup_keys(group_id, item.get("rank"), href, title):
        if key in lookup:
            return lookup[key]
    return {}


def first_clean(*values: Any) -> str:
    for value in values:
        text = clean(value)
        if text:
            return text
    return ""


def article_rows(
    group: dict[str, Any],
    bucket: str,
    candidate_lookup: dict[tuple[str, str, str], dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    candidate_lookup = candidate_lookup or {}
    group_id = clean(group.get("group_id"))
    for item in group.get(bucket) or []:
        href = clean(item.get("href") or item.get("landing_url"))
        title = clean(item.get("title"))
        if not href or not title:
            continue
        candidate = candidate_for_item(candidate_lookup, group_id, item)
        rows.append({
            "group_id": group_id,
            "subquestion_id": clean(group.get("subquestion_id")),
            "subquestion_slug": clean(group.get("subquestion_slug")),
            "subquestion_group_slug": clean(group.get("subquestion_group_slug")),
            "subquestion_group_title": clean(group.get("subquestion_group_title")),
            "subquestion_text": clean(group.get("subquestion_text")),
            "query_family": clean(group.get("query_family")),
            "publisher": clean(group.get("publisher")),
            "query_text": clean(group.get("current_query")),
            "decision": clean(group.get("decision")),
            "rcs_0_to_10": group.get("rcs_0_to_10", ""),
            "rcs_flag": clean(group.get("rcs_flag")),
            "rcs_reasoning": clean(group.get("rcs_reasoning")),
            "abstract_preview_status": clean(item.get("abstract_preview_status") or group.get("abstract_preview_status")),
            "rank": item.get("rank", ""),
            "title": title,
            "href": href,
            "landing_url": clean(item.get("landing_url")) or href,
            "doi": first_clean(item.get("doi"), candidate.get("doi")),
            "context": first_clean(item.get("context"), candidate.get("context")),
            "page_url": first_clean(item.get("page_url"), item.get("search_url"), item.get("searchUrl"), candidate.get("page_url")),
            "abstract": first_clean(item.get("abstract"), candidate.get("abstract")),
            "abstract_source": first_clean(item.get("abstract_source"), candidate.get("abstract_source")),
            "pdf_url": first_clean(item.get("pdf_url"), item.get("direct_pdf_url"), item.get("crossref_pdf_url"), candidate.get("pdf_url")),
            "source_key": first_clean(item.get("source_key"), candidate.get("source_key")),
            "publisher_key": first_clean(item.get("publisher_key"), candidate.get("publisher_key")),
            "agent_reason": clean(item.get("reason")),
            "agent_score": item.get("rcs_0_to_10", group.get("rcs_0_to_10", "")),
            "source_bucket": bucket,
        })
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--recommendations", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--max-next-queries-per-group", type=int, default=3)
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    recommendations_path = (args.recommendations or latest_recommendations(run_dir)).resolve()
    recommendations = load_json(recommendations_path, {})
    if not isinstance(recommendations, dict):
        raise SystemExit("recommendations must be a JSON object")
    validate_agent_provenance(recommendations_path, recommendations)
    validate_group_coverage(recommendations_path, recommendations)
    output_dir = (args.output_dir or recommendations_path.parent / "applied-decisions").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_lookup = load_candidate_lookup(recommendations_path.parent)

    next_queries: list[dict[str, Any]] = []
    abstract_queue: list[dict[str, Any]] = []
    capture_queue: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []

    for group in recommendations.get("groups") or []:
        validate_phase_decision(group, recommendations)
        decision = normalize_decision(group.get("decision")) or "needs_abstract_preview"
        if decision not in ALLOWED_DECISIONS:
            raise SystemExit(f"Unknown query refinement decision: {decision}")
        group["decision"] = decision
        group_id = clean(group.get("group_id"))
        status = {
            "group_id": group_id,
            "subquestion_id": clean(group.get("subquestion_id")),
            "publisher": clean(group.get("publisher")),
            "current_query": clean(group.get("current_query")),
            "decision": decision,
            "rcs_0_to_10": group.get("rcs_0_to_10", ""),
            "rcs_reasoning": clean(group.get("rcs_reasoning")),
            "rcs_flag": clean(group.get("rcs_flag")),
        }
        statuses.append(status)
        if can_enqueue_abstract(group):
            abstract_queue.extend(article_rows(group, "abstract_probe_articles", candidate_lookup))
        if can_enqueue_capture(group, recommendations):
            capture_queue.extend(article_rows(group, "capture_articles", candidate_lookup))
        if can_enqueue_next_queries(group):
            for query in (group.get("next_simple_queries") or [])[: args.max_next_queries_per_group]:
                query = clean(query)
                if not is_simple_keyword_query(query):
                    continue
                next_queries.append({
                    "group_id": group_id,
                    "subquestion_id": status["subquestion_id"],
                    "subquestion_slug": clean(group.get("subquestion_slug")),
                    "subquestion_group_slug": clean(group.get("subquestion_group_slug")),
                    "subquestion_group_title": clean(group.get("subquestion_group_title")),
                    "subquestion_text": clean(group.get("subquestion_text")),
                    "query_family": clean(group.get("query_family")),
                    "iteration": recommendations.get("iteration", ""),
                    "total_iterations": recommendations.get("total_iterations", ""),
                    "year_start": recommendations.get("year_start", ""),
                    "year_end": recommendations.get("year_end", ""),
                    "publisher": status["publisher"],
                    "previous_query": status["current_query"],
                    "next_query": query,
                    "reason": clean(group.get("query_diagnosis")),
                })

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "recommendations": str(recommendations_path),
        "statuses": statuses,
        "next_queries": next_queries,
        "abstract_queue": abstract_queue,
        "capture_queue": capture_queue,
    }
    (output_dir / "query-iteration-status.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    common_fields = [
        "group_id", "subquestion_id", "subquestion_slug", "subquestion_group_slug",
        "subquestion_group_title", "subquestion_text", "query_family", "publisher",
        "query_text", "decision", "rcs_0_to_10", "rcs_flag", "rcs_reasoning", "abstract_preview_status",
        "rank", "title", "href", "landing_url", "doi", "context", "page_url", "abstract", "abstract_source", "pdf_url", "source_key", "publisher_key",
        "agent_score", "agent_reason", "source_bucket",
    ]
    write_csv(output_dir / "abstract-preview-queue.csv", abstract_queue, common_fields)
    write_csv(output_dir / "capture-queue.csv", capture_queue, common_fields)
    write_csv(
        output_dir / "next-simple-queries.csv",
        next_queries,
        [
            "group_id", "subquestion_id", "subquestion_slug", "subquestion_group_slug",
            "subquestion_group_title", "subquestion_text", "query_family", "iteration",
            "total_iterations", "year_start", "year_end", "publisher", "previous_query",
            "next_query", "reason",
        ],
    )

    lines = [
        "# Query Iteration Decisions",
        "",
        f"- Recommendations: `{recommendations_path}`",
        f"- Next queries: {len(next_queries)}",
        f"- Abstract previews: {len(abstract_queue)}",
        f"- Capture candidates: {len(capture_queue)}",
        "",
        "## Status",
        "",
    ]
    for status in statuses:
        lines.append(
            f"- `{status['group_id']}`: {status['decision']} "
            f"(RCS={status['rcs_0_to_10']}) - {status['rcs_reasoning']}"
        )
    lines.extend(["", "## Next Commands", ""])
    if abstract_queue:
        lines.append(
            "Run abstract preview sequentially for this queue through the connected "
            "OpenCLI browser session: "
            f"`python literature-loop-capture\\scripts\\abstract_preview.py \"{run_dir}\" "
            f"--recommendations \"{recommendations_path}\" --abstract-queue \"{output_dir / 'abstract-preview-queue.csv'}\" "
            "--opencli-session lit-preview`"
        )
    if next_queries:
        lines.append(
            "`next-simple-queries.csv` contains queued candidate queries only. "
            "Do not continue discovery from them until the responsible subquestion "
            "subagent has read captured full text, written reading notes, extracted "
            "high-value seeds/references/gaps, and recorded "
            "`coverage_decision=iterate_query` in the coverage review."
        )
    if capture_queue:
        lines.append("Use `capture-queue.csv` as the agent-approved list for full capture.")
    (output_dir / "query-iteration-status.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"applied_decisions_dir={output_dir}")
    print(f"next_queries={len(next_queries)}")
    print(f"abstract_queue={len(abstract_queue)}")
    print(f"capture_queue={len(capture_queue)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
