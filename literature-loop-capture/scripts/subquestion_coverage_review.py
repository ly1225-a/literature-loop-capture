#!/usr/bin/env python3
"""Create per-subquestion evidence coverage review packets."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def unique_nonempty(values: list[Any], limit: int = 30) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        item = clean(value)
        key = item.lower()
        if not item or key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def read_summary(run_dir: Path) -> list[dict[str, Any]]:
    json_path = run_dir / "run-summary.json"
    data = load_json(json_path, [])
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    csv_path = run_dir / "run-summary.csv"
    if csv_path.exists():
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    return []


def read_query_rounds(run_dir: Path) -> list[dict[str, Any]]:
    preview = load_json(run_dir / "query-plan-preview.json", {})
    if isinstance(preview, dict) and isinstance(preview.get("subquestions"), list):
        return [row for row in preview["subquestions"] if isinstance(row, dict)]
    data = load_json(run_dir / "query-rounds.json", [])
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


def numeric(value: Any) -> float | None:
    text = clean(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def read_rcs_groups(run_dir: Path) -> dict[str, list[dict[str, Any]]]:
    by_subquestion: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in run_dir.glob("query-refinement/iteration-*/query-refinement-recommendations.json"):
        data = load_json(path, {})
        groups = data.get("groups") if isinstance(data, dict) else []
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            key = subquestion_key(group)
            by_subquestion[key].append(group)
    return by_subquestion


def subquestion_key(row: dict[str, Any]) -> str:
    return clean(row.get("subquestion_id")) or clean(row.get("subquestion_slug")) or "unassigned"


REQUIRED_NOTE_FIELDS = {
    "five_cs": ["five cs", "five c", "5c", "5 cs"],
    "figure_table_check": ["图表检查", "figure", "table"],
    "reference_selection": ["引用选择", "reference pick", "selected reference", "recommended reference"],
    "high_value_seed": ["high-value seed", "high value seed", "高价值 seed", "高价值seed"],
    "gap_list": ["gap list", "remaining gap", "缺口", "不足"],
    "worth_close_reading": ["worth_close_reading:"],
    "worth_close_reading_score_0_to_5": ["worth_close_reading_score_0_to_5:"],
    "coverage_impact": ["对 subquestion coverage 的影响", "coverage impact", "subquestion coverage"],
}


def subquestion_reading_lens(query_family: str) -> list[str]:
    family = (query_family or "").lower()
    if "definition" in family or "landscape" in family:
        return [
            "terminology and definitions",
            "field boundaries and representative seed papers",
            "terms that should become later simple keyword queries",
        ]
    if "data" in family or "resource" in family:
        return [
            "databases, datasets, benchmarks, corpora, tools, and reusable resources",
            "schema, entity types, relation types, data sources, coverage, access, and update status",
            "resource names that should become seed-driven iteration queries or blockers",
        ]
    if "method" in family or "model" in family:
        return [
            "construction workflow, extraction, fusion, modeling, algorithms, and pipelines",
            "inputs, outputs, assumptions, baselines, implementation clues, and reproducibility limits",
            "method/model names that should become seed-driven iteration queries or blockers",
        ]
    if "evaluation" in family or "benchmark" in family:
        return [
            "metrics, validation design, benchmarks, baselines, comparisons, and ablations",
            "negative findings, failure cases, threats to validity, and missing evaluation evidence",
            "benchmark or metric names that should become seed-driven iteration queries or blockers",
        ]
    if "application" in family or "case" in family:
        return [
            "use cases, tasks, deployment contexts, users, constraints, and demonstrated effects",
            "whether applications are evidenced by data or only speculative",
            "application/task names that should become seed-driven iteration queries or blockers",
        ]
    if "limitation" in family or "gap" in family:
        return [
            "unresolved gaps, contradictions, missing data, missing methods, and risks",
            "evidence needed to close each gap",
            "gap terms that should become seed-driven iteration queries or blockers",
        ]
    return [
        "evidence directly tied to this subquestion",
        "reusable named resources, methods, datasets, metrics, references, and gaps",
        "high-value seeds that should become later simple keyword queries or blockers",
    ]


def quality_first_stop_decision(
    *,
    groups: list[dict[str, Any]],
    captured_article_count: int,
    seed_evidence_count: int,
    reference_evidence_count: int,
    gap_evidence_count: int,
    blocker_count: int,
) -> dict[str, Any]:
    scores = [score for score in (numeric(group.get("rcs_0_to_10")) for group in groups) if score is not None]
    high_rcs_count = len([score for score in scores if score >= 7])
    if captured_article_count == 0 and high_rcs_count:
        recommendation = "capture_high_rcs_candidates"
    elif captured_article_count == 0:
        recommendation = "improve_query_or_capture"
    elif blocker_count:
        recommendation = "resolve_blockers_or_stop_with_gaps"
    elif gap_evidence_count and not seed_evidence_count:
        recommendation = "iterate_from_gaps"
    else:
        recommendation = "ready_for_subagent_coverage_decision"
    return {
        "evaluated_candidate_count": len(groups),
        "high_rcs_candidate_count": high_rcs_count,
        "max_rcs_0_to_10": max(scores) if scores else "",
        "captured_article_count": captured_article_count,
        "seed_evidence_count": seed_evidence_count,
        "reference_evidence_count": reference_evidence_count,
        "gap_evidence_count": gap_evidence_count,
        "blocker_count": blocker_count,
        "recommendation": recommendation,
    }


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def note_validation(article_dir: str) -> dict[str, Any]:
    if not article_dir:
        return {"status": "missing_article_dir", "missing": list(REQUIRED_NOTE_FIELDS)}
    path = Path(article_dir)
    note_path = path / "reading-note-zh.md"
    if not path.exists():
        return {"status": "article_dir_missing", "missing": list(REQUIRED_NOTE_FIELDS)}
    if not note_path.exists():
        return {"status": "missing_reading_note", "missing": list(REQUIRED_NOTE_FIELDS)}
    text = read_text(note_path)
    lower = text.lower()
    missing = [
        field for field, needles in REQUIRED_NOTE_FIELDS.items()
        if not any(needle.lower() in lower for needle in needles)
    ]
    if missing:
        return {"status": "missing_required_fields", "missing": missing, "text": text}
    return {"status": "ready", "missing": [], "text": text}


def article_note_status(article_dir: str) -> str:
    validation = note_validation(article_dir)
    if validation["status"] == "ready":
        return "reading_note_ready"
    if validation["status"] in {"missing_required_fields", "missing_reading_note"}:
        return "reading_note_pending"
    return validation["status"]


def extract_lines(text: str, patterns: list[str], limit: int = 8) -> list[str]:
    rows: list[str] = []
    lower_patterns = [pattern.lower() for pattern in patterns]
    for raw_line in text.splitlines():
        line = clean(raw_line.strip("-* "))
        if not line:
            continue
        low = line.lower()
        if any(pattern in low for pattern in lower_patterns):
            rows.append(line[:500])
    return rows[:limit]


def extract_blocker_lines(text: str, limit: int = 8) -> list[str]:
    """Extract only active blockers, not ordinary research gaps.

    Reading notes include a required `gaps_blockers` seed-ledger field. Those
    entries describe evidence gaps that may drive iteration; they are not
    capture/auth/tool blockers and should not prevent a sufficient or
    stop-with-gaps coverage decision.
    """
    rows: list[str] = []
    for raw_line in text.splitlines():
        line = clean(raw_line.strip("-* "))
        if not line:
            continue
        low = line.lower()
        if any(marker in low for marker in ["gaps_blockers", "gap list", "high-value seed ledger", "typed seed ledger"]):
            continue
        if any(marker in low for marker in ["抓取失败", "capture failed", "auth blocker", "publisher blocker", "unresolved blocker", "blocked_capture"]):
            rows.append(line[:500])
    return rows[:limit]


def recommended_reference_evidence(article_dir: str) -> list[dict[str, Any]]:
    if not article_dir:
        return []
    path = Path(article_dir)
    evidence: list[dict[str, Any]] = []
    data = load_json(path / "recommended-references.json", [])
    if isinstance(data, list):
        for item in data[:10]:
            if isinstance(item, dict):
                evidence.append({
                    "title": clean(item.get("title")),
                    "doi": clean(item.get("doi")),
                    "reason": clean(item.get("reason") or item.get("rationale")),
                })
    if not evidence and (path / "recommended-references.md").exists():
        for line in read_text(path / "recommended-references.md").splitlines():
            line = clean(line.strip("-* "))
            if line:
                evidence.append({"title": line[:240], "doi": "", "reason": ""})
    return evidence[:10]


def article_evidence(row: dict[str, Any]) -> dict[str, Any]:
    article_dir = clean(row.get("article_dir"))
    validation = note_validation(article_dir)
    note_text = validation.get("text", "")
    references = recommended_reference_evidence(article_dir)
    seeds = extract_lines(note_text, ["high-value seed", "high value seed", "高价值 seed", "高价值seed"])
    gaps = extract_lines(note_text, ["gap list", "remaining gap", "缺口", "不足"])
    blockers = extract_blocker_lines(note_text)
    next_queries = extract_lines(note_text, ["next query", "后续 query", "后续查询", "proposed query"])
    return {
        "title": clean(row.get("title")),
        "publisher": clean(row.get("publisher")),
        "year": clean(row.get("year")),
        "doi": clean(row.get("doi")),
        "article_dir": article_dir,
        "note_status": "reading_note_ready" if validation["status"] == "ready" else "reading_note_pending",
        "note_validation_status": validation["status"],
        "missing_note_fields": validation["missing"],
        "abstract": clean(row.get("abstract"))[:700],
        "seed_evidence": seeds,
        "reference_evidence": references,
        "gap_evidence": gaps,
        "blockers": blockers,
        "proposed_next_queries": next_queries,
    }


TERMINAL_COVERAGE_DECISIONS = {"sufficient", "stop_with_gaps"}
TERMINAL_COVERAGE_STAGE_STATUSES = {"final_sufficient", "stop_with_gaps", "blocked"}
CORE_GAP_TERMS = {
    "benchmark",
    "blocker",
    "database",
    "dataset",
    "evaluation",
    "gap",
    "method",
    "metric",
    "model",
    "nutrition",
    "ontology",
    "resource",
    "schema",
    "validation",
    "workflow",
}
COVERAGE_REVIEW_FIELDS = [
    "review_mode",
    "agent_id",
    "reviewed_artifacts",
    "coverage_decision",
    "coverage_stage_status",
    "coverage_score_0_to_5",
    "coverage_rationale",
    "missing_evidence_or_terms",
    "next_simple_queries",
    "next_query_rationale",
    "next_action",
]


def existing_coverage_by_subquestion(run_dir: Path) -> dict[str, dict[str, Any]]:
    data = load_json(run_dir / "coverage-review" / "subquestion-coverage-review.json", {})
    rows = data.get("subquestions") if isinstance(data, dict) else []
    if not isinstance(rows, list):
        return {}
    return {
        clean(row.get("subquestion_id")): row
        for row in rows
        if isinstance(row, dict) and clean(row.get("subquestion_id"))
    }


def flatten_ledger_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        rows: list[str] = []
        for nested in value.values():
            rows.extend(flatten_ledger_values(nested))
        return rows
    if isinstance(value, list):
        rows = []
        for nested in value:
            rows.extend(flatten_ledger_values(nested))
        return rows
    text = clean(value)
    return [text] if text else []


def has_core_gap(item: dict[str, Any]) -> bool:
    values: list[str] = []
    for key in ["missing_evidence_or_terms", "gap_evidence", "blockers"]:
        values.extend(flatten_ledger_values(item.get(key)))
    ledger = item.get("typed_seed_ledger")
    if isinstance(ledger, dict):
        values.extend(flatten_ledger_values(ledger.get("gaps_blockers")))
        values.extend(flatten_ledger_values(ledger.get("proposed_next_queries")))
    haystack = " ".join(values).lower()
    return any(term in haystack for term in CORE_GAP_TERMS)


def has_seed_or_gap_ledger_evidence(item: dict[str, Any]) -> bool:
    ledger = item.get("typed_seed_ledger")
    if not isinstance(ledger, dict):
        return False
    for key in [
        "named_resources",
        "methods_models_workflows",
        "evaluation_terms_metrics",
        "cited_seed_papers",
        "gaps_blockers",
        "proposed_next_queries",
    ]:
        if flatten_ledger_values(ledger.get(key)):
            return True
    return False


def coverage_stage_status(item: dict[str, Any]) -> str:
    decision = clean(item.get("coverage_decision")).lower()
    if decision in {"", "pending", "pending_subagent_review"}:
        return "pending_subagent_review"
    if decision in {"blocked", "stop_with_gaps"}:
        return decision
    if decision == "iterate_query":
        return "needs_iteration_review"

    score = numeric(item.get("coverage_score_0_to_5") or item.get("evidence_sufficiency_0_to_5"))
    if decision == "sufficient":
        core_gap = has_core_gap(item)
        if score is not None and score >= 4.5 and not core_gap:
            return "final_sufficient"
        if score is not None and score >= 4.0:
            if core_gap or has_seed_or_gap_ledger_evidence(item):
                return "needs_iteration_review"
            return "primary_pass_sufficient"
        return "needs_iteration_review"

    return decision or "pending_subagent_review"


def has_explicit_blockers(item: dict[str, Any]) -> bool:
    for key in ["blockers", "capture_blockers", "unresolved_blockers"]:
        if flatten_ledger_values(item.get(key)):
            return True
    return False


def has_explicit_final_sufficient_override(item: dict[str, Any]) -> bool:
    if clean(item.get("coverage_stage_status")).lower() != "final_sufficient":
        return False
    if clean(item.get("coverage_decision")).lower() != "sufficient":
        return False
    score = numeric(item.get("coverage_score_0_to_5") or item.get("evidence_sufficiency_0_to_5"))
    if score is None or score < 4.0:
        return False
    if has_explicit_blockers(item):
        return False
    return bool(clean(item.get("final_sufficient_rationale") or item.get("coverage_rationale")))


def effective_coverage_stage_status(item: dict[str, Any]) -> str:
    if has_explicit_final_sufficient_override(item):
        return "final_sufficient"
    return coverage_stage_status(item)


def preserve_terminal_coverage_decision(
    item: dict[str, Any],
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    if not existing:
        return item
    stage_status = effective_coverage_stage_status(existing)
    if stage_status not in TERMINAL_COVERAGE_STAGE_STATUSES:
        return item
    old_count = int(existing.get("captured_article_count") or 0)
    new_count = int(item.get("captured_article_count") or 0)
    if old_count != new_count:
        return item
    merged = dict(item)
    for field in COVERAGE_REVIEW_FIELDS:
        if field in existing:
            merged[field] = existing[field]
    merged["coverage_stage_status"] = stage_status
    merged["coverage_preserved_from_previous_review"] = True
    return merged


def build_packet(run_dir: Path) -> dict[str, Any]:
    summary_rows = read_summary(run_dir)
    rounds = read_query_rounds(run_dir)
    rcs_groups = read_rcs_groups(run_dir)
    existing_coverage = existing_coverage_by_subquestion(run_dir)
    by_subquestion: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in summary_rows:
        if clean(row.get("status")) == "captured":
            by_subquestion[subquestion_key(row)].append(row)

    packets: list[dict[str, Any]] = []
    seen: set[str] = set()
    ordered_rounds = rounds or [{"subquestion_id": key} for key in sorted(by_subquestion)]
    for round_info in ordered_rounds:
        key = subquestion_key(round_info)
        if key in seen:
            continue
        seen.add(key)
        articles = [article_evidence(row) for row in by_subquestion.get(key, [])]
        seed_evidence = [seed for article in articles for seed in article.get("seed_evidence") or []]
        reference_evidence = [ref for article in articles for ref in article.get("reference_evidence") or []]
        gap_evidence = [gap for article in articles for gap in article.get("gap_evidence") or []]
        blockers = [blocker for article in articles for blocker in article.get("blockers") or []]
        proposed_next_queries = [query for article in articles for query in article.get("proposed_next_queries") or []]
        query_family = clean(round_info.get("query_family"))
        stop_decision = quality_first_stop_decision(
            groups=rcs_groups.get(key, []),
            captured_article_count=len(articles),
            seed_evidence_count=len(seed_evidence),
            reference_evidence_count=len(reference_evidence),
            gap_evidence_count=len(gap_evidence),
            blocker_count=len(blockers),
        )
        typed_seed_ledger = {
            "named_resources": extract_lines("\n".join(seed_evidence), ["database", "dataset", "benchmark", "resource", "tool", "ontology"]),
            "methods_models_workflows": extract_lines("\n".join(seed_evidence), ["method", "model", "workflow", "algorithm", "pipeline"]),
            "evaluation_terms_metrics": extract_lines("\n".join(seed_evidence + gap_evidence), ["metric", "validation", "evaluation", "benchmark", "baseline"]),
            "cited_seed_papers": [clean(ref.get("title")) for ref in reference_evidence if clean(ref.get("title"))][:10],
            "gaps_blockers": unique_nonempty(gap_evidence + blockers),
            "proposed_next_queries": unique_nonempty(proposed_next_queries),
        }
        item = {
            "subquestion_id": key,
            "subquestion_slug": clean(round_info.get("subquestion_slug")),
            "subquestion_group_slug": clean(round_info.get("subquestion_group_slug")) or "general",
            "subquestion_group_title": clean(round_info.get("subquestion_group_title")) or "General",
            "claim_subquestion": clean(round_info.get("claim_subquestion") or round_info.get("subquestion_text")),
            "query_family": query_family,
            "captured_article_count": len(articles),
            "articles": articles,
            "subquestion_reading_lens": subquestion_reading_lens(query_family),
            "seed_evidence": seed_evidence[:30],
            "typed_seed_ledger": typed_seed_ledger,
            "reference_evidence": reference_evidence[:30],
            "gap_evidence": gap_evidence[:30],
            "blockers": blockers[:30],
            "stop_decision": stop_decision,
            "coverage_decision": "pending_subagent_review",
            "coverage_stage_status": "pending_subagent_review",
            "coverage_score_0_to_5": "",
            "coverage_rationale": "",
            "missing_evidence_or_terms": [],
            "next_simple_queries": [],
            "next_query_rationale": "",
            "next_action": "",
        }
        packet_item = preserve_terminal_coverage_decision(item, existing_coverage.get(key))
        packet_item["coverage_stage_status"] = effective_coverage_stage_status(packet_item)
        packets.append(packet_item)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "instructions": (
            "For each subquestion, read captured article folders and reading-note-zh.md when present. "
            "Use the subquestion_reading_lens and typed_seed_ledger. "
            "Set coverage_decision to sufficient, iterate_query, or stop_with_gaps. "
            "If coverage_score_0_to_5 is below 4, choose iterate_query or stop_with_gaps. "
            "If iterating, fill next_simple_queries with short simple keyword queries and explain next_query_rationale."
        ),
        "subquestions": packets,
    }


def write_outputs(output_dir: Path, packet: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "subquestion-coverage-review.json").write_text(
        json.dumps(packet, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    rows: list[dict[str, Any]] = []
    for item in packet.get("subquestions") or []:
        rows.append({
            "subquestion_id": item.get("subquestion_id", ""),
            "subquestion_slug": item.get("subquestion_slug", ""),
            "subquestion_group_slug": item.get("subquestion_group_slug", ""),
            "query_family": item.get("query_family", ""),
            "captured_article_count": item.get("captured_article_count", 0),
            "coverage_decision": item.get("coverage_decision", ""),
            "coverage_stage_status": item.get("coverage_stage_status", ""),
            "coverage_score_0_to_5": item.get("coverage_score_0_to_5", item.get("evidence_sufficiency_0_to_5", "")),
            "next_action": item.get("next_action", ""),
        })
    with (output_dir / "subquestion-coverage-review.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "subquestion_id", "subquestion_slug", "subquestion_group_slug",
            "query_family", "captured_article_count", "coverage_decision",
            "coverage_stage_status", "coverage_score_0_to_5", "next_action",
        ])
        writer.writeheader()
        writer.writerows(rows)
    lines = [
        "# Subquestion Coverage Review",
        "",
        packet.get("instructions", ""),
        "",
    ]
    for item in packet.get("subquestions") or []:
        lines.extend([
            f"## {item.get('subquestion_id')}: {item.get('claim_subquestion') or item.get('query_family')}",
            "",
            f"- Captured articles: {item.get('captured_article_count')}",
            f"- Coverage decision: {item.get('coverage_decision')}",
            f"- Coverage stage status: {item.get('coverage_stage_status')}",
            "- subquestion_reading_lens:",
            *[f"  - {lens}" for lens in item.get("subquestion_reading_lens") or []],
            "- typed seed ledger: inspect `typed_seed_ledger` in JSON before deciding sufficiency.",
            "- Required subagent output: set `coverage_decision`, `coverage_score_0_to_5`, `coverage_rationale`, `missing_evidence_or_terms`, `next_simple_queries`, and `next_query_rationale` in the JSON.",
            "",
        ])
        for article in item.get("articles") or []:
            lines.append(f"- {article.get('year') or ''} | {article.get('publisher') or ''} | {article.get('title') or ''} | {article.get('note_status')}")
        lines.append("")
    (output_dir / "subquestion-coverage-review.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    output_dir = (args.output_dir or run_dir / "coverage-review").resolve()
    packet = build_packet(run_dir)
    write_outputs(output_dir, packet)
    print(f"coverage_review_dir={output_dir}")
    print(f"subquestions={len(packet.get('subquestions') or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
