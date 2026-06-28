#!/usr/bin/env python3
"""Build agent review packets for iterative simple-keyword query refinement."""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


def clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def int_or(value: Any, default: int = 0) -> int:
    try:
        return int(str(value))
    except Exception:
        return default


def safe_name(text: str, limit: int = 55) -> str:
    text = re.sub(r"[\\/:*?\"<>|]+", "_", text or "")
    text = re.sub(r"\s+", "_", text).strip(" ._-")
    return (text[:limit].strip(" ._-") or "untitled")


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def query_group_key(row: dict[str, Any]) -> tuple[str, str, str, str, int]:
    return (
        clean(row.get("subquestion_id")),
        clean(row.get("subquestion_text")),
        clean(row.get("query_text")),
        clean(row.get("publisher")),
        int_or(row.get("page"), 1),
    )


def group_candidates(rows: list[dict[str, Any]], page: int, top_n: int) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str, int], list[dict[str, Any]]] = {}
    for row in rows:
        stage = clean(row.get("stage"))
        if stage not in {"candidate", "no_candidates", "publisher-blocker"}:
            continue
        if int_or(row.get("page"), 1) != page:
            continue
        grouped.setdefault(query_group_key(row), []).append(row)

    out: list[dict[str, Any]] = []
    for key, values in sorted(grouped.items(), key=lambda item: item[0]):
        subquestion_id, subquestion_text, query_text, publisher, page_num = key
        candidate_values = [row for row in values if clean(row.get("stage")) == "candidate"]
        candidate_values.sort(key=lambda row: int_or(row.get("unique_rank"), 9999))
        candidates: list[dict[str, Any]] = []
        for row in candidate_values[:top_n]:
            candidates.append({
                "rank": int_or(row.get("unique_rank"), 0),
                "title": clean(row.get("title")),
                "href": clean(row.get("href")),
                "landing_url": clean(row.get("landing_url")),
                "context": clean(row.get("context"))[:700],
                "abstract": clean(row.get("abstract"))[:1600],
                "abstract_source": clean(row.get("abstract_source")),
                "page_url": clean(row.get("page_url")),
                "dedupe_key": clean(row.get("dedupe_key")),
                "doi": clean(row.get("doi")),
                "pdf_url": clean(row.get("pdf_url") or row.get("public_pdf_candidate")),
                "source_key": clean(row.get("source_key")),
                "publisher_key": clean(row.get("publisher_key")),
                "duplicate_status": clean(row.get("duplicate_status")) or "new",
                "duplicate_of": clean(row.get("duplicate_of")),
                "screening_priority": clean(row.get("screening_priority")) or "normal",
                "preselected_by_discovery_audit": str(row.get("selected")).lower() == "true" or row.get("selected") is True,
            })
        status_rows = [row for row in values if clean(row.get("stage")) != "candidate"]
        status_row = status_rows[0] if status_rows else {}
        out.append({
            "group_id": safe_name(f"{subquestion_id}_{publisher}_p{page_num}_{query_text}", 90),
            "subquestion_id": subquestion_id,
            "subquestion_text": subquestion_text,
            "query_text": query_text,
            "publisher": publisher,
            "page": page_num,
            "subquestion_slug": clean(values[0].get("subquestion_slug")) if values else "",
            "subquestion_group_slug": clean(values[0].get("subquestion_group_slug")) if values else "",
            "subquestion_group_title": clean(values[0].get("subquestion_group_title")) if values else "",
            "query_family": clean(values[0].get("query_family")) if values else "",
            "candidate_count": len(candidate_values),
            "reviewed_candidate_limit": top_n,
            "result_stage": clean(status_row.get("stage")) or ("candidate" if candidate_values else ""),
            "result_status": clean(status_row.get("status")) or ("candidate" if candidate_values else ""),
            "result_error": clean(status_row.get("error")),
            "candidates": candidates,
        })
    return out


def redact_abstracts_for_triage(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hide abstract text from search-page triage packets.

    Discovery keeps search-page abstracts for the later abstract-preview stage.
    The first subagent pass should only know whether an abstract is available,
    not read or score the abstract body.
    """
    redacted = json.loads(json.dumps(groups, ensure_ascii=False))
    for group in redacted:
        for candidate in group.get("candidates") or []:
            candidate["abstract_available"] = bool(clean(candidate.get("abstract")))
            candidate["abstract"] = ""
            candidate["abstract_source"] = clean(candidate.get("abstract_source"))
    return redacted


def write_candidates_csv(path: Path, groups: list[dict[str, Any]]) -> None:
    fields = [
        "group_id", "subquestion_id", "subquestion_text", "query_text", "publisher",
        "page", "candidate_count", "result_stage", "result_status", "result_error",
        "rank", "title", "href", "landing_url", "context", "abstract", "abstract_source",
        "page_url", "dedupe_key", "doi", "pdf_url", "source_key", "publisher_key",
        "duplicate_status", "duplicate_of", "screening_priority",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for group in groups:
            candidates = group.get("candidates", [])
            if not candidates:
                writer.writerow({
                    **{key: group.get(key, "") for key in fields},
                    "group_id": group.get("group_id", ""),
                })
                continue
            for candidate in candidates:
                writer.writerow({
                    **{key: group.get(key, "") for key in fields},
                    **candidate,
                    "group_id": group.get("group_id", ""),
                })


def recommendation_template(groups: list[dict[str, Any]], picks_per_query: int, iteration: int, total_iterations: int) -> dict[str, Any]:
    return {
        "generated_by": "query-refinement-subagent",
        "review_mode": "",
        "agent_id": "",
        "fallback_reason": "",
        "review_phase": "search_page_triage",
        "iteration": iteration,
        "total_iterations": total_iterations,
        "rules": {
            "query_style": "simple keywords only; no Boolean operators, no parentheses",
            "max_next_queries_per_group": 3,
            "picks_per_query": picks_per_query,
        },
        "groups": [
            {
                "group_id": group.get("group_id", ""),
                "subquestion_id": group.get("subquestion_id", ""),
                "subquestion_slug": group.get("subquestion_slug", ""),
                "subquestion_group_slug": group.get("subquestion_group_slug", ""),
                "subquestion_group_title": group.get("subquestion_group_title", ""),
                "subquestion_text": group.get("subquestion_text", ""),
                "query_family": group.get("query_family", ""),
                "publisher": group.get("publisher", ""),
                "page": group.get("page", 1),
                "current_query": group.get("query_text", ""),
                "decision": "",
                "rcs_0_to_10": "",
                "rcs_reasoning": "",
                "rcs_flag": "",
                "abstract_preview_status": "",
                "query_diagnosis": "",
                "top_articles": [
                    {
                        "rank": "",
                        "title": "",
                        "href": "",
                        "doi": "",
                        "pdf_url": "",
                        "rcs_0_to_10": "",
                        "reason": "",
                    }
                    for _ in range(picks_per_query)
                ],
                "abstract_probe_articles": [],
                "capture_articles": [],
                "terms_to_keep": [],
                "terms_to_drop": [],
                "terms_to_add": [],
                "next_simple_queries": [],
            }
            for group in groups
        ],
    }


def render_brief(
    claim: str,
    groups: list[dict[str, Any]],
    picks_per_query: int,
    iteration: int,
    total_iterations: int,
    scope_title: str,
) -> str:
    lines = [
        "# Query Refinement Subagent Brief",
        "",
        f"- Scope: {scope_title}",
        f"- Big question: {claim or 'see question.json'}",
        f"- Iteration: {iteration} of {total_iterations}",
        f"- Candidate source: first saved search-result page for each query/publisher group",
        f"- Pick target: {picks_per_query} most relevant articles per query group",
        "- Search-page abstracts may have been saved for the later abstract-preview stage, but their text is intentionally hidden in this triage brief.",
        "",
        "## Rules",
        "",
        "- Judge relevance from title, snippet/context, publisher, query text, and subquestion.",
        "- This packet is `review_phase=search_page_triage`: judge only publisher search-page title, snippet/context, query text, publisher, and subquestion lens.",
        "- `rcs_0_to_10` is Relevance to Core Search for this query branch; in this phase it selects abstract preview, query iteration, or branch stop.",
        "- Use RCS 0-1 for off-topic, 2-3 tangential, 4-5 partial, 6-7 highly relevant, and 8-10 foundational or seminal.",
        "- A high search-page RCS means an excellent abstract-preview candidate; it cannot approve full-text capture yet.",
        "- Title/snippet/context evidence can justify abstract preview, but cannot justify full-text capture or prove subquestion coverage.",
        "- Use simple keyword queries only. Do not use AND, OR, NOT, parentheses, or nested Boolean expressions.",
        "- Prefer specific domain phrases and named resources over generic terms like method, model, application, or framework.",
        "- If results are too broad, drop generic terms and add domain-specific phrases.",
        "- If results are too sparse, remove over-specific terms and keep the core domain phrase plus 1-2 method/resource terms.",
        "- For each group set one decision: `needs_abstract_preview`, `iterate_query`, or `stop_low_yield`. Do not use `ready_to_capture` in this search-page triage phase.",
        "- Put promising articles into `abstract_probe_articles`; be inclusive here because the stricter capture decision happens after abstract preview.",
        "- Leave `capture_articles` empty in this phase. Full-text capture requires a later `review_phase=abstract_capture_review` pass after abstract preview.",
        "- If this is not the final iteration and RCS is not enough, propose up to 3 next simple keyword queries per group.",
        "- If this is the final iteration, record the best articles and explain whether the query is good enough for capture.",
        "- Treat RCS >= 5 as enough for abstract preview in this phase. RCS <= 4 requires query iteration or branch stop unless the agent gives a specific reason.",
        "- Stopping a query branch never means the whole subquestion is sufficient.",
        "",
        "## Required Output",
        "",
        "- Fill `query-refinement-recommendations.json` using the provided template.",
        "- Set top-level `review_mode` to `subagent` and `agent_id` to the actual subagent/tool identifier. If no subagent tool is callable, set `review_mode` to `main_agent_fallback`, fill `fallback_reason`, and write `main-agent-fallback.md`.",
        "- Write `subagent-response.md` with `review_mode`, `agent_id`, the groups reviewed, and concise evidence. A blank or generic response is invalid.",
        "- Every group must explicitly set `decision`, `rcs_0_to_10`, `rcs_reasoning`, and optional `rcs_flag`; blank decisions are invalid.",
        "- Optionally write `query-refinement-notes.md` with concise reasoning.",
        "- Keep article picks bounded to the configured pick target.",
        "",
        "## Review Groups",
        "",
    ]
    for group in groups:
        lines.extend([
            f"### {group.get('group_id', '')}",
            "",
            f"- Subquestion: {group.get('subquestion_text', '')}",
            f"- Publisher: {group.get('publisher', '')}",
            f"- Page: {group.get('page', '')}",
            f"- Current query: `{group.get('query_text', '')}`",
            f"- Candidates on page: {group.get('candidate_count', 0)}",
            "",
        ])
        for candidate in group.get("candidates", []):
            abstract_available = bool(clean(candidate.get("abstract"))) or bool(candidate.get("abstract_available"))
            abstract_source = clean(candidate.get("abstract_source")) or "none"
            abstract_status = f"available from {abstract_source}" if abstract_available else "not available on the saved result page"
            duplicate_status = clean(candidate.get("duplicate_status")) or "new"
            screening_priority = clean(candidate.get("screening_priority")) or "normal"
            duplicate_of = clean(candidate.get("duplicate_of"))
            duplicate_marker = f"status={duplicate_status}; priority={screening_priority}"
            if duplicate_of:
                duplicate_marker += f"; duplicate_of={duplicate_of}"
            lines.extend([
                f"{candidate.get('rank', '')}. {candidate.get('title', '')}",
                f"   - URL: {candidate.get('href', '')}",
                f"   - Duplicate marker: {duplicate_marker}",
                f"   - Context: {candidate.get('context', '')}",
                f"   - Search-page abstract: {abstract_status}",
            ])
        if not group.get("candidates"):
            lines.extend([
                "No candidates recorded for this query/publisher branch.",
                f"- Result status: `{group.get('result_status', '') or 'no_candidates'}`",
                f"- Error: {group.get('result_error', '') or 'none'}",
                "- Agent action required: decide whether to broaden, replace, stop, or keep this branch for a later iteration.",
            ])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def subquestion_folder(run_dir: Path, query_rounds: list[dict[str, Any]], subquestion_id: str) -> Path:
    for row in query_rounds:
        if clean(row.get("subquestion_id")) == subquestion_id:
            group_slug = clean(row.get("subquestion_group_slug")) or "general"
            return run_dir / "subquestions" / group_slug / subquestion_id
    return run_dir / "subquestions" / "general" / subquestion_id


def build_packets(
    run_dir: Path,
    page: int = 1,
    top_candidates_per_query: int = 12,
    picks_per_query: int = 3,
    iteration: int = 1,
    total_iterations: int = 3,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    rows = load_json(run_dir / "discovery-audit.json", [])
    if not isinstance(rows, list):
        raise SystemExit("discovery-audit.json must contain a JSON list")
    query_rounds = load_json(run_dir / "query-rounds.json", [])
    if not isinstance(query_rounds, list):
        query_rounds = []
    question = load_json(run_dir / "question.json", {})
    claim = clean(question.get("question") if isinstance(question, dict) else "")

    groups = group_candidates(rows, page, top_candidates_per_query)
    output_dir = (output_dir or (run_dir / "query-refinement" / f"iteration-{iteration:02d}")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    subagent_groups = redact_abstracts_for_triage(groups)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "claim": claim,
        "page": page,
        "iteration": iteration,
        "total_iterations": total_iterations,
        "top_candidates_per_query": top_candidates_per_query,
        "picks_per_query": picks_per_query,
        "groups": subagent_groups,
    }
    (output_dir / "query-refinement-input.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_candidates_csv(output_dir / "query-refinement-candidates.csv", groups)
    template = recommendation_template(subagent_groups, picks_per_query, iteration, total_iterations)
    (output_dir / "query-refinement-recommendations.template.json").write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
    brief = render_brief(claim, subagent_groups, picks_per_query, iteration, total_iterations, "all subquestions")
    (output_dir / "query-refinement-agent-brief.md").write_text(brief, encoding="utf-8")
    (output_dir / "subagent-prompt.md").write_text(brief, encoding="utf-8")

    groups_by_subquestion: dict[str, list[dict[str, Any]]] = {}
    for group in groups:
        groups_by_subquestion.setdefault(clean(group.get("subquestion_id")), []).append(group)
    for subquestion_id, sub_groups in groups_by_subquestion.items():
        folder = subquestion_folder(run_dir, query_rounds, subquestion_id)
        folder.mkdir(parents=True, exist_ok=True)
        sub_brief = render_brief(claim, redact_abstracts_for_triage(sub_groups), picks_per_query, iteration, total_iterations, subquestion_id)
        (folder / f"query-refinement-agent-brief.iteration-{iteration:02d}.md").write_text(sub_brief, encoding="utf-8")
        (folder / f"query-refinement-subagent-prompt.iteration-{iteration:02d}.md").write_text(sub_brief, encoding="utf-8")

    return {"output_dir": str(output_dir), "groups": len(groups)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path, help="Existing capture/preflight run directory containing discovery-audit.json.")
    parser.add_argument("--page", type=int, default=1, help="Search result page to review; defaults to first page.")
    parser.add_argument("--top-candidates-per-query", type=int, default=12)
    parser.add_argument("--picks-per-query", type=int, default=3)
    parser.add_argument("--iteration", type=int, default=1)
    parser.add_argument("--total-iterations", type=int, default=3)
    parser.add_argument("--output-dir", type=Path, help="Override output directory; defaults to <run>/query-refinement/iteration-NN.")
    args = parser.parse_args()

    result = build_packets(
        args.run_dir,
        page=args.page,
        top_candidates_per_query=args.top_candidates_per_query,
        picks_per_query=args.picks_per_query,
        iteration=args.iteration,
        total_iterations=args.total_iterations,
        output_dir=args.output_dir,
    )
    print(f"query_refinement_dir={result['output_dir']}")
    print(f"groups={result['groups']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
