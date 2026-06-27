#!/usr/bin/env python3
"""Build user-reviewable query iteration artifacts from agent query rationale."""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import discovery_core
import query_plan_common
import seed_ledger


SUPPORTED_PUBLISHERS = ("elsevier", "acs", "wiley", "springer")
GENERIC_ACRONYMS = {"KG", "AI", "ML", "LLM", "NLP", "RDF", "URI", "URL", "DOI"}
BROAD_STOPWORDS = {
    "a", "an", "and", "are", "as", "by", "for", "from", "in", "into", "is", "it", "of", "on", "or", "the", "to", "with",
    "not", "no", "none", "missing", "lack", "lacks", "absent", "limited", "only", "need", "needs", "needed", "proposed",
    "next", "query", "full", "final", "table", "article", "paper", "study", "studies", "evidence", "workflow",
}
NON_QUERY_TOKENS = {
    "csv", "xlsx", "pdf", "html", "xml", "json", "api", "doi", "url", "urls", "http", "https",
}
PUBLISHER_ARTIFACT_TOKENS = {
    "springer", "wiley", "elsevier", "sciencedirect", "acs", "pubmed", "arxiv",
}
OPERATIONAL_QUERY_RE = re.compile(
    r"\b(?:download|downloads|api|apis|endpoint|license|licence|access|login|supplement|supplementary|github|code)\b",
    flags=re.IGNORECASE,
)


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def coverage_item(run_dir: Path, subquestion_id: str) -> dict[str, Any]:
    data = load_json(run_dir / "coverage-review" / "subquestion-coverage-review.json", {})
    items = data.get("subquestions") if isinstance(data, dict) else []
    if not isinstance(items, list):
        items = []
    for item in items:
        if isinstance(item, dict) and clean(item.get("subquestion_id")) == subquestion_id:
            return item
    raise SystemExit(f"subquestion {subquestion_id!r} not found in coverage review")


def article_dirs(item: dict[str, Any]) -> list[Path]:
    articles = item.get("articles")
    if not isinstance(articles, list):
        return []
    out: list[Path] = []
    seen: set[str] = set()
    for article in articles:
        if not isinstance(article, dict):
            continue
        value = clean(article.get("article_dir") or article.get("source_article_dir") or article.get("path"))
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(Path(value))
    return out


def _note_seed_roles(article_dir: Path) -> dict[str, str]:
    note_path = article_dir / "reading-note-zh.md"
    if not note_path.exists():
        return {}
    roles: dict[str, str] = {}
    text = note_path.read_text(encoding="utf-8", errors="replace")
    for raw_line in text.splitlines():
        line = clean(raw_line)
        if not line:
            continue
        parts = re.split(r"[:：]", line, maxsplit=1)
        seed_text = clean(parts[1] if len(parts) > 1 else line)
        if not seed_text:
            continue
        lowered = line.lower()
        if "proposed next query" in lowered:
            roles[seed_text] = "proposed_next_query"
        elif "gap list" in lowered:
            roles.setdefault(seed_text, "gap")
        elif "high-value seed" in lowered or "高价值 seed" in lowered:
            roles.setdefault(seed_text, "seed")
    return roles


def _annotate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    role_cache: dict[str, dict[str, str]] = {}
    annotated: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if clean(item.get("source_type")) == "reading_note":
            article_dir = clean(item.get("source_article_dir"))
            role_cache.setdefault(article_dir, _note_seed_roles(Path(article_dir)))
            item["seed_role"] = role_cache[article_dir].get(clean(item.get("seed_text")), "seed")
        annotated.append(item)
    return annotated


def _query_text(row: dict[str, Any]) -> str:
    action = clean(row.get("recommended_action"))
    role = clean(row.get("seed_role"))
    if action == "query_iteration" and role != "seed":
        return clean(row.get("proposed_short_query") or row.get("seed_text"))
    if action == "reference_followup":
        return clean(row.get("seed_text") or row.get("proposed_short_query"))
    return ""


def _seed_query_text(row: dict[str, Any]) -> str:
    if clean(row.get("recommended_action")) != "query_iteration":
        return ""
    return clean(row.get("proposed_short_query") or row.get("seed_text"))


def unique_queries(rows: list[dict[str, Any]], limit: int = 8) -> list[str]:
    priority = {"query_iteration": 0, "reference_followup": 1}
    indexed = list(enumerate(rows))
    indexed.sort(key=lambda item: (priority.get(clean(item[1].get("recommended_action")), 9), item[0]))
    queries: list[str] = []
    seen: set[str] = set()
    for extractor in [_query_text, _seed_query_text]:
        for _index, row in indexed:
            query = extractor(row)
            if not query:
                continue
            key = query.lower()
            if key in seen:
                continue
            seen.add(key)
            queries.append(query)
            if len(queries) >= limit:
                return queries
        if queries:
            return queries
    return queries


def prior_plan_queries(run_dir: Path, subquestion_id: str) -> list[str]:
    for name in ["agent-query-plan.json", "query-plan-preview.json"]:
        payload = load_json(run_dir / name, {})
        subquestions = payload.get("subquestions") if isinstance(payload, dict) else []
        if not isinstance(subquestions, list):
            continue
        for item in subquestions:
            if not isinstance(item, dict) or clean(item.get("subquestion_id")) != subquestion_id:
                continue
            queries: list[str] = []
            for value in item.get("queries") or []:
                if isinstance(value, dict):
                    queries.append(clean(value.get("query")))
                else:
                    queries.append(clean(value))
            return _dedupe([query for query in queries if query])
    return []


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        query = clean(value)
        if not query:
            continue
        key = query.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(query)
    return out


def _strip_operational_terms(query: str) -> str:
    stripped = OPERATIONAL_QUERY_RE.sub(" ", clean(query))
    stripped = re.sub(r"\s+", " ", stripped).strip(" -_:;,")
    return clean(stripped)


def _strip_seed_prefixes(query: str) -> str:
    return clean(re.sub(
        r"^(?:proposed\s+next\s+query|high[-\s]+value\s+seed\s+ledger|gap\s+list|reference\s+pick|selected\s+reference)\s*[:：-]?\s*",
        "",
        clean(query),
        flags=re.IGNORECASE,
    ))


def _english_query_text(query: str) -> str:
    query = clean(query)
    query = re.sub(r"[\u4e00-\u9fff]+", " ", query)
    query = re.sub(r"[^A-Za-z0-9 /_-]+", " ", query)
    return clean(query.replace("/", " "))


def refine_broad_query(query: str) -> str:
    query = _strip_seed_prefixes(_strip_operational_terms(query))
    if not query:
        return ""
    text = _english_query_text(query)
    if not text:
        return ""
    raw_tokens = [token.lower() for token in re.findall(r"[A-Za-z][A-Za-z0-9-]*", text)]
    if not (2 <= len(raw_tokens) <= 4):
        return ""
    if set(raw_tokens) & (NON_QUERY_TOKENS | PUBLISHER_ARTIFACT_TOKENS):
        return ""
    tokens = [
        token for token in raw_tokens
        if token not in BROAD_STOPWORDS and len(token) > 1 and not re.fullmatch(r"\d+", token)
    ]
    if len(tokens) != len(raw_tokens) or len(tokens) < 2:
        return ""
    if "kg" in tokens:
        tokens = ["graph" if token == "kg" else token for token in tokens]
    compact = _dedupe(tokens)
    if len(compact) != len(tokens):
        return ""
    query_out = " ".join(compact)
    if not discovery_core.is_simple_publisher_query(query_out, max_words=4):
        return ""
    return query_out


def _query_token_set(query: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", query.lower()))


def _query_semantic_key(query: str) -> tuple[str, ...]:
    return tuple(sorted(_query_token_set(query)))


def refined_broad_queries(
    candidates: list[str],
    prior_queries: list[str] | None = None,
    limit: int = 8,
    remove_broad_queries: set[str] | None = None,
) -> list[str]:
    removals = remove_broad_queries or set()
    prior_values = [refine_broad_query(query) for query in (prior_queries or [])]
    prior_exact = {query.lower() for query in prior_values if query}
    prior_semantic = {_query_semantic_key(query) for query in prior_values if query}
    refined: list[str] = []
    for query in candidates:
        value = refine_broad_query(query)
        if not value:
            continue
        value_key = value.lower()
        semantic_key = _query_semantic_key(value)
        if value_key in removals or value_key in prior_exact or semantic_key in prior_semantic:
            continue
        refined.append(value)
    out: list[str] = []
    seen: set[str] = set()
    seen_semantic: set[tuple[str, ...]] = set()
    for query in refined:
        key = query.lower()
        tokens = _query_token_set(query)
        semantic_key = _query_semantic_key(query)
        if (
            key in seen
            or semantic_key in seen_semantic
            or any(
                tokens
                and (tokens < _query_token_set(existing) or _query_token_set(existing) < tokens)
                for existing in out
            )
        ):
            continue
        seen.add(key)
        seen_semantic.add(semantic_key)
        out.append(query)
        if len(out) >= limit:
            break
    return out


def broad_query_details(
    broad_queries: list[str],
    prior_queries: list[str],
    rows: list[dict[str, Any]],
    candidate_sources: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    prior_context = _dedupe([clean(query) for query in prior_queries if clean(query)])
    row_sources: dict[str, list[str]] = {
        clean(key).lower(): [clean(value) for value in values if clean(value)]
        for key, values in (candidate_sources or {}).items()
        if clean(key)
    }
    details: list[dict[str, Any]] = []
    for query in broad_queries:
        query = clean(query)
        if not query:
            continue
        source = "gap_seed_refined"
        reason = "Validated English short query from query-rationale review."
        examples = _dedupe(row_sources.get(query.lower(), []))[:3]
        if examples:
            reason = "Accepted from explicit query-rationale review evidence."
        details.append({
            "query": query,
            "source": source,
            "reason": reason,
            "previous_query_plan_context": prior_context,
            "evidence_examples": examples,
        })
    return details


def override_removed_broad_queries(override_path: Path) -> set[str]:
    overrides = load_json(override_path, {})
    if not isinstance(overrides, dict):
        return set()
    return {
        clean(value).lower()
        for value in overrides.get("remove_broad_queries") or []
        if clean(value)
    }


def _rationale_items(payload: dict[str, Any], names: list[str]) -> list[Any]:
    for name in names:
        value = payload.get(name)
        if isinstance(value, list):
            return value
    return []


def _rationale_item_text(item: Any, fields: list[str]) -> str:
    if isinstance(item, str):
        return clean(item)
    if isinstance(item, dict):
        for field in fields:
            text = clean(item.get(field))
            if text:
                return text
    return ""


def _rationale_item_source(item: Any, fallback: str) -> str:
    if isinstance(item, dict):
        reason = clean(
            item.get("rationale")
            or item.get("reason")
            or item.get("evidence")
            or item.get("source")
        )
        if reason:
            return reason
    return fallback


def load_query_rationale_review(output_dir: Path) -> dict[str, Any]:
    path = output_dir / "query-rationale-review.json"
    if not path.exists():
        raise SystemExit(
            "missing query-rationale-review.json; iteration 2+ queries must be "
            "authored by a subagent/main-agent rationale review before Python "
            "builds query-plan-amendment.json"
        )
    payload = load_json(path, {})
    if not isinstance(payload, dict):
        raise SystemExit(f"invalid query-rationale-review.json: {path}")
    review_mode = clean(payload.get("review_mode")).lower()
    if review_mode not in {"subagent", "main_agent_fallback", "user_correction"}:
        raise SystemExit(
            "query-rationale-review.json must declare review_mode=subagent, "
            "main_agent_fallback, or user_correction"
        )
    return payload


def split_rationale_review_queries(
    rationale: dict[str, Any],
    prior_queries: list[str] | None = None,
    broad_limit: int = 6,
    remove_broad_queries: set[str] | None = None,
) -> dict[str, list[str] | dict[str, list[str]]]:
    broad_items = _rationale_items(
        rationale,
        ["broad_discovery_queries", "broad_queries"],
    )
    exact_items = _rationale_items(
        rationale,
        ["exact_openalex_targets", "exact_target_queries", "exact_queries"],
    )
    operational_items = _rationale_items(
        rationale,
        ["operational_queries", "manual_audit_queries", "manual_queries"],
    )

    broad_candidates: list[str] = []
    broad_sources: dict[str, list[str]] = {}
    for item in broad_items:
        query = _rationale_item_text(item, ["query", "broad_query", "text"])
        if not query:
            continue
        broad_candidates.append(query)
        refined = refine_broad_query(query)
        if refined:
            broad_sources.setdefault(refined.lower(), []).append(
                _rationale_item_source(item, f"query-rationale-review: {query}")
            )

    exact = _dedupe(
        [
            query
            for item in exact_items
            for query in [_rationale_item_text(item, ["query", "target", "exact_query", "title", "text"])]
            if query
        ]
    )
    broad_candidates = [
        query
        for query in _dedupe(broad_candidates)
        if query.lower() not in {item.lower() for item in exact}
    ]
    broad = refined_broad_queries(
        broad_candidates,
        prior_queries=prior_queries,
        limit=broad_limit,
        remove_broad_queries=remove_broad_queries,
    )
    operational = _dedupe(
        [
            query
            for item in operational_items
            for query in [_rationale_item_text(item, ["query", "text", "reason"])]
            if query
        ]
    )
    return {
        "exact_queries": exact,
        "broad_queries": broad[:broad_limit],
        "broad_query_sources": broad_sources,
        "operational_queries": operational,
    }


def _looks_exact_target(query: str) -> bool:
    query = clean(query)
    if not query:
        return False
    words = re.findall(r"[A-Za-z0-9-]+", query)
    if ":" in query and len(words) <= 14:
        return True
    lower_tokens = {token.lower() for token in words}
    acronyms = re.findall(r"\b[A-Z]{2,}[A-Za-z0-9-]*\b", query)
    if any(acronym.upper() not in GENERIC_ACRONYMS for acronym in acronyms):
        return True
    if re.search(r"\b[A-Za-z]+[0-9]+[A-Za-z0-9-]*\b", query):
        return True
    if re.search(r"\b[A-Z][a-z]+[A-Z][A-Za-z0-9-]*\b", query):
        return True
    quoted = re.findall(r'"([^"]+)"', query)
    if quoted and any(len(item.split()) <= 14 for item in quoted):
        return True
    return False


def split_iteration_queries(
    item: dict[str, Any],
    rows: list[dict[str, Any]],
    broad_limit: int = 6,
    prior_queries: list[str] | None = None,
    remove_broad_queries: set[str] | None = None,
) -> dict[str, list[str]]:
    exact: list[str] = []
    broad_candidates: list[str] = []
    broad_sources: dict[str, list[str]] = {}
    operational: list[str] = []

    def add_candidate(value: str, *, prefer_exact: bool = False, source_label: str = "") -> None:
        query = clean(value)
        if not query:
            return
        query = _strip_seed_prefixes(query)
        normalized = _strip_operational_terms(query)
        if OPERATIONAL_QUERY_RE.search(query):
            operational.append(query)
            if normalized and normalized.lower() != query.lower() and _looks_exact_target(normalized):
                exact.append(normalized)
            return
        if prefer_exact or _looks_exact_target(query):
            exact.append(query)
        elif discovery_core.is_simple_publisher_query(query, max_words=6):
            broad_candidates.append(query)
            refined = refine_broad_query(query)
            if refined:
                label = clean(source_label)
                source_text = f"{label}: {query}" if label else query
                broad_sources.setdefault(refined.lower(), []).append(source_text)

    for query in item.get("next_simple_queries") or []:
        add_candidate(str(query), source_label="coverage_next_query")

    indexed = list(enumerate(rows))
    priority = {"reference_followup": 0, "query_iteration": 1}
    indexed.sort(key=lambda item_row: (priority.get(clean(item_row[1].get("recommended_action")), 9), item_row[0]))
    for _index, row in indexed:
        action = clean(row.get("recommended_action"))
        role = clean(row.get("seed_role"))
        if action == "reference_followup":
            add_candidate(clean(row.get("seed_text") or row.get("proposed_short_query")), prefer_exact=True)
        elif action == "query_iteration" and role == "seed":
            add_candidate(clean(row.get("proposed_short_query") or row.get("seed_text")), prefer_exact=True)
        elif action == "query_iteration" and role == "proposed_next_query":
            add_candidate(clean(row.get("seed_text")), prefer_exact=False, source_label="reading_note_proposed_next_query")

    exact = _dedupe(exact)
    broad_candidates = [query for query in _dedupe(broad_candidates) if query.lower() not in {item.lower() for item in exact}]
    broad = refined_broad_queries(
        broad_candidates,
        prior_queries=prior_queries,
        limit=broad_limit,
        remove_broad_queries=remove_broad_queries,
    )
    return {
        "exact_queries": exact,
        "broad_queries": broad[:broad_limit],
        "broad_query_sources": broad_sources,
        "operational_queries": _dedupe(operational),
    }


def _work_title(work: dict[str, Any]) -> str:
    return clean(work.get("display_name") or work.get("title"))


def _work_source(work: dict[str, Any]) -> dict[str, Any]:
    primary = work.get("primary_location") if isinstance(work.get("primary_location"), dict) else {}
    source = primary.get("source") if isinstance(primary.get("source"), dict) else {}
    return source


def _work_landing_page(work: dict[str, Any]) -> str:
    primary = work.get("primary_location") if isinstance(work.get("primary_location"), dict) else {}
    return clean(primary.get("landing_page_url") if isinstance(primary, dict) else "")


def _flat_openalex_work(work: dict[str, Any], query: str) -> dict[str, Any]:
    source = _work_source(work)
    landing = _work_landing_page(work)
    return {
        "query": query,
        "id": clean(work.get("id")),
        "title": _work_title(work),
        "year": work.get("publication_year") or "",
        "doi": clean(work.get("doi")),
        "cited_by_count": work.get("cited_by_count") or 0,
        "venue": clean(source.get("display_name") if isinstance(source, dict) else ""),
        "source_display_name": clean(source.get("display_name") if isinstance(source, dict) else ""),
        "publisher": clean(source.get("host_organization_name") if isinstance(source, dict) else ""),
        "host_organization_name": clean(source.get("host_organization_name") if isinstance(source, dict) else ""),
        "landing_page_url": landing,
    }


def _title_score(query: str, work: dict[str, Any]) -> float:
    title = _work_title(work)
    q = query.lower()
    t = title.lower()
    if not q or not t:
        return 0.0
    score = 0.0
    if q == t:
        score += 20.0
    elif q in t or t in q:
        score += 12.0
    q_tokens = set(re.findall(r"[a-z0-9]+", q))
    t_tokens = set(re.findall(r"[a-z0-9]+", t))
    if q_tokens:
        score += 8.0 * (len(q_tokens & t_tokens) / len(q_tokens))
    return score + min(float(work.get("cited_by_count") or 0), 1000.0) / 10000.0


def openalex_best_work(query: str) -> dict[str, Any]:
    data = discovery_core.openalex_request({"search": query, "per-page": "5"})
    if not isinstance(data, dict) or data.get("_error"):
        return {"_error": clean(data.get("_error") if isinstance(data, dict) else "openalex_request_failed")}
    works = [work for work in data.get("results") or [] if isinstance(work, dict)]
    if not works:
        return {}
    best = max(works, key=lambda work: _title_score(query, work))
    best_score = _title_score(query, best)
    if best_score < 4.0:
        flat = _flat_openalex_work(best, query)
        return {
            "_low_confidence": True,
            "score": round(best_score, 3),
            "_low_confidence_score": round(best_score, 3),
            "candidate_title": flat["title"],
            **flat,
        }
    return best


def ground_exact_targets(exact_queries: list[str]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for query in exact_queries:
        work = openalex_best_work(query)
        if work.get("_error"):
            targets.append({
                "exact_query": query,
                "publisher_route": "manual_hold",
                "manual_reason": clean(work.get("_error")) or "openalex_request_failed",
                "agent_openalex_verified": "false",
                "openalex_verification_status": "openalex_request_failed",
            })
            continue
        if work.get("_low_confidence"):
            targets.append({
                "exact_query": query,
                "openalex_id": clean(work.get("id")),
                "openalex_title": clean(work.get("title") or work.get("candidate_title")),
                "year": work.get("year") or "",
                "doi": clean(work.get("doi")),
                "venue": clean(work.get("venue")),
                "publisher": clean(work.get("publisher")),
                "landing_page_url": clean(work.get("landing_page_url")),
                "cited_by_count": work.get("cited_by_count") or 0,
                "publisher_route": "manual_hold",
                "manual_reason": f"needs_agent_openalex_verification: low_confidence_candidate {clean(work.get('candidate_title'))}",
                "low_confidence_score": work.get("_low_confidence_score") or work.get("score") or "",
                "agent_openalex_verified": "false",
                "openalex_verification_status": "needs_agent_disambiguation",
            })
            continue
        if not work:
            targets.append({
                "exact_query": query,
                "publisher_route": "manual_hold",
                "manual_reason": "openalex_not_found",
                "agent_openalex_verified": "false",
                "openalex_verification_status": "openalex_not_found",
            })
            continue
        flat = _flat_openalex_work(work, query)
        route = query_plan_common.supported_publisher_for_openalex_work(flat)
        target = {
            "exact_query": query,
            "openalex_id": flat["id"],
            "openalex_title": flat["title"],
            "year": flat["year"],
            "doi": flat["doi"],
            "venue": flat["venue"],
            "publisher": flat["publisher"],
            "landing_page_url": flat["landing_page_url"],
            "cited_by_count": flat["cited_by_count"],
            "publisher_route": "manual_hold",
            "candidate_publisher_route": route or "",
            "manual_reason": "needs_agent_openalex_verification",
            "agent_openalex_verified": "false",
            "openalex_verification_status": "needs_agent_disambiguation",
        }
        targets.append(target)
    return targets


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def write_target_exports(output_dir: Path, exact_targets: list[dict[str, Any]], operational_queries: list[str]) -> None:
    fields = [
        "exact_query",
        "publisher_route",
        "openalex_title",
        "year",
        "doi",
        "venue",
        "publisher",
        "landing_page_url",
        "openalex_id",
        "cited_by_count",
        "manual_reason",
        "candidate_publisher_route",
        "agent_openalex_verified",
        "openalex_verification_status",
    ]
    output_dir.joinpath("exact-targets.json").write_text(json.dumps(exact_targets, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(output_dir / "exact-targets.csv", exact_targets, fields)
    manual_rows = [target for target in exact_targets if target.get("publisher_route") not in SUPPORTED_PUBLISHERS]
    _write_csv(output_dir / "manual.csv", manual_rows, fields)
    _write_csv(
        output_dir / "operational-resource-audit.csv",
        [{"query": query, "reason": "not_a_publisher_literature_query"} for query in operational_queries],
        ["query", "reason"],
    )


def _target_work_for_audit(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "query": clean(target.get("exact_query")),
        "id": clean(target.get("openalex_id")),
        "title": clean(target.get("openalex_title")),
        "year": target.get("year") or "",
        "doi": clean(target.get("doi")),
        "cited_by_count": target.get("cited_by_count") or 0,
        "venue": clean(target.get("venue")),
        "source_display_name": clean(target.get("venue")),
        "publisher": clean(target.get("publisher")),
        "host_organization_name": clean(target.get("publisher")),
        "landing_page_url": clean(target.get("landing_page_url")),
    }


def build_openalex_grounding(run_dir: Path, exact_targets: list[dict[str, Any]], broad_queries: list[str]) -> dict[str, Any]:
    base_plan = load_json(run_dir / "query-plan-preview.json", {})
    base_grounding = base_plan.get("openalex_grounding") if isinstance(base_plan, dict) else {}
    if not isinstance(base_grounding, dict):
        base_grounding = {}
    exact_works = [
        _target_work_for_audit(target)
        for target in exact_targets
        if clean(target.get("openalex_id")) or clean(target.get("openalex_title"))
    ]
    base_works = base_grounding.get("works") if isinstance(base_grounding.get("works"), list) else []
    terms = _dedupe(
        [term for target in exact_targets for term in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", clean(target.get("openalex_title") or target.get("exact_query")))]
        + [term for query in broad_queries for term in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", query)]
        + [clean(term) for term in (base_grounding.get("terms") or []) if clean(term)]
    )[:16]
    works = exact_works + [work for work in base_works if isinstance(work, dict)][: max(0, 16 - len(exact_works))]
    return {
        "api_key_present": bool(os.environ.get("OPENALEX_API_KEY")) or bool(works) or bool(base_grounding.get("api_key_present")),
        "status": "ok" if works and terms else clean(base_grounding.get("status")) or "needs_openalex_grounding",
        "terms": terms,
        "works": works,
        "probe_queries": broad_queries,
        "exact_target_queries": [clean(target.get("exact_query")) for target in exact_targets if clean(target.get("exact_query"))],
        "source": "openalex",
    }


def _html_list(values: Any, empty: str) -> str:
    if not isinstance(values, list):
        values = []
    items = [clean(value) for value in values if clean(value)]
    if not items:
        return f"<li class='muted'>{html.escape(empty)}</li>"
    return "\n".join(f"<li>{html.escape(item)}</li>" for item in items)


def _seed_cards(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p class='muted'>No seed ledger rows were found for this subquestion.</p>"
    cards: list[str] = []
    for row in rows:
        action = clean(row.get("recommended_action")) or "review"
        why = clean(row.get("why_it_matters")) or clean(row.get("seed_kind")) or "seed from captured reading artifact"
        query = _query_text(row) or clean(row.get("proposed_short_query")) or clean(row.get("seed_text"))
        title = clean(row.get("seed_text")) or "Untitled seed"
        source = clean(row.get("source_article_title")) or clean(row.get("source_article_dir"))
        cards.append(
            "<article class='seed-card'>"
            f"<div class='card-meta'>{html.escape(action)}</div>"
            f"<h3>{html.escape(title)}</h3>"
            f"<p><strong>Why:</strong> {html.escape(why)}</p>"
            f"<p><strong>Short query:</strong> <code>{html.escape(query)}</code></p>"
            f"<p class='muted'>{html.escape(source)}</p>"
            "</article>"
        )
    return "\n".join(cards)


def _exact_target_html(targets: list[dict[str, Any]]) -> str:
    if not targets:
        return "<p class='muted'>No exact article/resource/method targets were derived for this iteration.</p>"
    lines = [
        "<table>",
        "<thead><tr><th>Exact query</th><th>OpenAlex match</th><th>Publisher route</th><th>DOI / reason</th></tr></thead>",
        "<tbody>",
    ]
    for target in targets:
        route = clean(target.get("publisher_route")) or "manual_hold"
        detail = clean(target.get("doi")) or clean(target.get("manual_reason"))
        title = clean(target.get("openalex_title")) or "No OpenAlex match"
        meta = " · ".join(value for value in [clean(target.get("year")), clean(target.get("venue")), clean(target.get("publisher"))] if value)
        lines.append(
            "<tr>"
            f"<td><code>{html.escape(clean(target.get('exact_query')))}</code></td>"
            f"<td><strong>{html.escape(title)}</strong><br><span class='muted'>{html.escape(meta)}</span></td>"
            f"<td>{html.escape(route)}</td>"
            f"<td>{html.escape(detail)}</td>"
            "</tr>"
        )
    lines.extend(["</tbody>", "</table>"])
    return "\n".join(lines)


def _manual_hold_html(targets: list[dict[str, Any]]) -> str:
    manual_targets = [
        target
        for target in targets
        if clean(target.get("publisher_route")) not in SUPPORTED_PUBLISHERS
    ]
    if not manual_targets:
        return "<p class='muted'>No manual holds were derived for this iteration.</p>"
    return _exact_target_html(manual_targets)


def write_html(path: Path, item: dict[str, Any], rows: list[dict[str, Any]], review: dict[str, Any], iteration: int) -> None:
    subquestion = clean(item.get("claim_subquestion") or item.get("subquestion_text") or item.get("subquestion_id"))
    score = clean(item.get("coverage_score_0_to_5"))
    status = clean(item.get("coverage_stage_status") or item.get("coverage_decision"))
    decision = clean(item.get("coverage_decision"))
    reason = clean(item.get("coverage_reasoning") or item.get("reason_to_turn") or item.get("iteration_reason"))
    broad_queries = review.get("broad_queries") if isinstance(review.get("broad_queries"), list) else []
    exact_targets = review.get("exact_targets") if isinstance(review.get("exact_targets"), list) else []
    operational_queries = review.get("operational_queries") if isinstance(review.get("operational_queries"), list) else []
    detail_by_query = {
        clean(detail.get("query")): detail
        for detail in (review.get("broad_query_details") or [])
        if isinstance(detail, dict) and clean(detail.get("query"))
    }
    broad_item_parts: list[str] = []
    for query in broad_queries:
        query = clean(query)
        if not query:
            continue
        detail = detail_by_query.get(query, {})
        previous_context = [
            clean(value)
            for value in (detail.get("previous_query_plan_context") or [])
            if clean(value)
        ]
        previous_context_html = ""
        if previous_context:
            previous_context_html = (
                "<p><strong>Previous query-plan context:</strong> "
                + ", ".join(f"<code>{html.escape(value)}</code>" for value in previous_context)
                + "</p>"
            )
        evidence_examples = [
            clean(value)
            for value in (detail.get("evidence_examples") or [])
            if clean(value)
        ]
        evidence_html = ""
        if evidence_examples:
            evidence_html = (
                "<p><strong>Seed/gap evidence:</strong> "
                + "; ".join(html.escape(value) for value in evidence_examples)
                + "</p>"
            )
        broad_item_parts.append(
            "<li class='query-row'>"
            f"<code class='query-chip'>{html.escape(query)}</code> "
            "<details><summary>More details</summary>"
            f"<p><strong>Source:</strong> {html.escape(clean(detail.get('source')) or 'refined')}</p>"
            f"<p>{html.escape(clean(detail.get('reason')) or 'Refined broad discovery query.')}</p>"
            f"{evidence_html}"
            f"{previous_context_html}"
            "</details>"
            "</li>"
        )
    broad_items = "\n".join(broad_item_parts) or "<li class='muted'>No new non-duplicate broad discovery query was derived from the seed ledger.</li>"
    operational_items = "\n".join(f"<li><code>{html.escape(clean(query))}</code></li>" for query in operational_queries if clean(query))
    if not operational_items:
        operational_items = "<li class='muted'>No operational resource-audit terms were removed.</li>"
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Iteration {iteration:02d} Review - {html.escape(clean(item.get("subquestion_id")))}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.5; margin: 0; color: #1f2933; background: #f7f8fa; }}
    main {{ max-width: 1040px; margin: 0 auto; padding: 32px 20px 48px; }}
    h1, h2, h3 {{ line-height: 1.2; }}
    h1 {{ margin-bottom: 8px; font-size: 30px; }}
    h2 {{ margin-top: 32px; border-bottom: 1px solid #d7dde5; padding-bottom: 8px; }}
    code {{ background: #eef2f6; border: 1px solid #d7dde5; border-radius: 4px; padding: 2px 5px; }}
    .panel, .seed-card {{ background: #fff; border: 1px solid #d7dde5; border-radius: 8px; padding: 18px; }}
    .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 18px 0; }}
    .summary div {{ background: #fff; border: 1px solid #d7dde5; border-radius: 8px; padding: 14px; }}
    .label, .card-meta {{ color: #637083; font-size: 12px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; }}
    .seed-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; }}
    .muted {{ color: #637083; }}
    .query-list {{ padding-left: 1.5rem; }}
    .query-row {{ margin: 10px 0; }}
    .query-chip {{ display: inline-block; margin: 2px 8px 2px 0; }}
    details {{ display: inline-block; vertical-align: top; color: #0b5e9e; }}
    details p {{ color: #1f2933; margin: 8px 0 0; max-width: 760px; }}
    summary {{ cursor: pointer; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; }}
    th, td {{ border-bottom: 1px solid #d7dde5; padding: 10px; text-align: left; vertical-align: top; }}
    th {{ color: #637083; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }}
  </style>
</head>
<body>
<main>
  <p class="label">Query Iteration Review</p>
  <h1>{html.escape(subquestion)}</h1>
  <div class="summary">
    <div><div class="label">Subquestion</div>{html.escape(clean(item.get("subquestion_id")))}</div>
    <div><div class="label">Coverage Score</div>{html.escape(score or "not recorded")}</div>
    <div><div class="label">Coverage Status</div>{html.escape(status or "not recorded")}</div>
    <div><div class="label">Decision</div>{html.escape(decision or "not recorded")}</div>
  </div>
  <section class="panel">
    <h2>Reason To Iterate</h2>
    <p>{html.escape(reason or "Review the missing evidence and seed ledger before approving another query iteration.")}</p>
    <ul>
      {_html_list(item.get("missing_evidence_or_terms"), "No missing evidence terms recorded.")}
    </ul>
  </section>
  <section>
    <h2>Exact OpenAlex-Grounded Targets</h2>
    <div class="panel">
      <p class="muted">Exact resource, method, or paper-name targets are grounded in OpenAlex first and saved for evidence/manual follow-up, not auto-searched.</p>
      {_exact_target_html(exact_targets)}
    </div>
  </section>
  <section>
    <h2>Broad Discovery Queries</h2>
    <div class="panel">
      <p class="muted">These broad discovery queries will be searched in the next approved discovery round.</p>
      <p class="muted">Only explicit short queries from coverage/subagent/reading-note evidence are executable here. Previous query-plan phrases are shown only as context inside More details; exact repeats and word-order duplicates are excluded from this queue.</p>
      <ol class="query-list">{broad_items}</ol>
    </div>
  </section>
  <section>
    <h2>Manual Holds</h2>
    <div class="panel">
      <p class="muted">Manual holds are saved to manual.csv and are not auto-searched.</p>
      {_manual_hold_html(exact_targets)}
    </div>
  </section>
  <section>
    <h2>Removed Resource-Audit Terms</h2>
    <div class="panel"><ul>{operational_items}</ul></div>
  </section>
  <section>
    <h2>Seed Cards</h2>
    <div class="seed-grid">
      {_seed_cards(rows)}
    </div>
  </section>
</main>
</body>
</html>
"""
    path.write_text(document, encoding="utf-8")


def _write_markdown(path: Path, item: dict[str, Any], review: dict[str, Any], output_dir: Path) -> None:
    subquestion = clean(item.get("claim_subquestion") or item.get("subquestion_text") or item.get("subquestion_id"))
    exact_targets = review.get("exact_targets") if isinstance(review.get("exact_targets"), list) else []
    broad_queries = review.get("broad_queries") if isinstance(review.get("broad_queries"), list) else []
    lines = [
        "# Query Plan Amendment",
        "",
        f"- Subquestion: {subquestion}",
        f"- Subquestion ID: {clean(item.get('subquestion_id'))}",
        f"- Coverage status: {clean(item.get('coverage_stage_status') or item.get('coverage_decision')) or 'not recorded'}",
        f"- Iteration source: {output_dir}",
        "",
        "## Exact OpenAlex-Grounded Targets",
        "",
    ]
    if exact_targets:
        for target in exact_targets:
            route = clean(target.get("publisher_route")) or "manual_hold"
            title = clean(target.get("openalex_title")) or "No OpenAlex match"
            detail = clean(target.get("doi")) or clean(target.get("manual_reason"))
            lines.append(f"- `{clean(target.get('exact_query'))}` -> {route}; {title}; {detail}")
    else:
        lines.append("- No exact targets were derived.")
    lines.extend(["", "## Broad Discovery Queries", ""])
    if broad_queries:
        lines.extend(f"- `{query}`" for query in broad_queries)
    else:
        lines.append("- No broad discovery query was derived.")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def build_iteration_review(run_dir: Path, subquestion_id: str, iteration: int) -> Path:
    run_dir = run_dir.resolve()
    item = coverage_item(run_dir, subquestion_id)
    output_dir = run_dir / "loop-state" / subquestion_id / f"iteration-{iteration:02d}"
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = _annotate_rows(seed_ledger.build_seed_ledger_rows(article_dirs(item), subquestion_id))
    prior_queries = prior_plan_queries(run_dir, subquestion_id)
    remove_broad_queries = override_removed_broad_queries(output_dir / "query-overrides.json")
    rationale_review = load_query_rationale_review(output_dir)
    split = split_rationale_review_queries(
        rationale_review,
        prior_queries=prior_queries,
        remove_broad_queries=remove_broad_queries,
    )
    exact_targets = ground_exact_targets(split["exact_queries"])
    broad_queries = split["broad_queries"]
    broad_details = broad_query_details(broad_queries, prior_queries, rows, split.get("broad_query_sources", {}))
    subquestion_text = clean(item.get("claim_subquestion") or item.get("subquestion_text") or subquestion_id)
    iteration_source = str(output_dir)
    openalex_grounding = build_openalex_grounding(run_dir, exact_targets, broad_queries)
    subquestions: list[dict[str, Any]] = [
        {
            "subquestion_id": subquestion_id,
            "subquestion_text": subquestion_text,
            "query_family": clean(item.get("query_family")) or "coverage-gap-seeds",
            "query_mode": "broad_discovery",
            "queries": broad_queries,
            "broad_queries": broad_queries,
            "broad_query_details": broad_details,
            "exact_target_queries": split["exact_queries"],
            "exact_targets": exact_targets,
            "operational_queries_removed": split["operational_queries"],
            "iteration_source": iteration_source,
        }
    ]
    amendment = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "requires_user_approval": True,
        "approval_status": "iteration_review",
        "exploration_sources": [
            {"label": "OpenAlex", "url": "https://openalex.org", "note": "metadata grounding for exact iteration targets"},
        ],
        "openalex_grounding": openalex_grounding,
        "subquestions": subquestions,
    }

    (output_dir / "seed-ledger.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    write_target_exports(output_dir, exact_targets, split["operational_queries"])
    (output_dir / "query-plan-amendment.json").write_text(json.dumps(amendment, ensure_ascii=False, indent=2), encoding="utf-8")
    review = {
        "exact_targets": exact_targets,
        "broad_queries": broad_queries,
        "broad_query_details": broad_details,
        "operational_queries": split["operational_queries"],
        "query_rationale_review": str(output_dir / "query-rationale-review.json"),
    }
    _write_markdown(output_dir / "query-plan-amendment.md", item, review, output_dir)
    write_html(output_dir / "iteration-review.html", item, rows, review, iteration)
    return output_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--subquestion-id", required=True)
    parser.add_argument("--iteration", type=int, required=True)
    args = parser.parse_args()

    output_dir = build_iteration_review(args.run_dir, args.subquestion_id, args.iteration)
    print(f"iteration_review_dir={output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
