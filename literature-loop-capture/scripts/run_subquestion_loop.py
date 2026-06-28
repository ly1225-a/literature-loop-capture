"""Gate a subquestion literature loop until coverage is sufficient or explicit stop."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import discovery_core as discovery
import subquestion_coverage_review


DEFAULT_COVERAGE_THRESHOLD = 4
DEFAULT_ITERATION_BUDGET = 3
SUCCESS_HANDOFF_STATUSES = {
    "ready_for_query_iteration",
    "needs_iteration_review",
    "primary_pass_sufficient",
}


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def numeric_score(value: Any) -> float | None:
    text = clean(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def latest_iteration_id(run_dir: Path, subquestion_id: str = "") -> int:
    ids = [1]
    root = run_dir / "loop-state" / subquestion_id if subquestion_id else run_dir / "loop-state"
    if root.exists():
        for child in root.glob("iteration-*"):
            suffix = child.name.removeprefix("iteration-")
            if suffix.isdigit():
                iteration_id = int(suffix)
                if iteration_id == 1 or full_iteration_artifact_path(run_dir, subquestion_id, iteration_id).exists():
                    ids.append(iteration_id)
    return max(ids)


def coverage_review_path(run_dir: Path) -> Path:
    return run_dir / "coverage-review" / "subquestion-coverage-review.json"


def find_subquestion(items: list[dict[str, Any]], subquestion_id: str) -> dict[str, Any]:
    for item in items:
        if clean(item.get("subquestion_id")) == subquestion_id:
            return item
    return {}


def read_coverage_item(run_dir: Path, subquestion_id: str) -> dict[str, Any]:
    data = load_json(coverage_review_path(run_dir), {})
    items = data.get("subquestions") if isinstance(data, dict) else []
    if not isinstance(items, list):
        return {}
    return find_subquestion([item for item in items if isinstance(item, dict)], subquestion_id)


def packet_item(run_dir: Path, subquestion_id: str) -> dict[str, Any]:
    packet = subquestion_coverage_review.build_packet(run_dir)
    items = packet.get("subquestions") if isinstance(packet, dict) else []
    if not isinstance(items, list):
        return {}
    return find_subquestion([item for item in items if isinstance(item, dict)], subquestion_id)


def normalize_next_queries(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    queries: list[str] = []
    for item in value:
        if isinstance(item, str):
            query = clean(item)
        elif isinstance(item, dict):
            query = clean(item.get("next_query") or item.get("query"))
        else:
            query = ""
        if query and discovery.is_simple_publisher_query(query):
            queries.append(query)
    return discovery.unique_ordered(queries)


def loop_state_dir(run_dir: Path, subquestion_id: str, iteration_id: int) -> Path:
    return run_dir / "loop-state" / subquestion_id / f"iteration-{iteration_id:02d}"


def openalex_audit_path(run_dir: Path, subquestion_id: str, iteration_id: int) -> Path:
    return loop_state_dir(run_dir, subquestion_id, iteration_id) / "openalex-grounding.json"


def full_iteration_artifact_path(run_dir: Path, subquestion_id: str, iteration_id: int) -> Path:
    return loop_state_dir(run_dir, subquestion_id, iteration_id) / "full-loop-artifact.json"


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


def subagent_response_exists(run_dir: Path, subquestion_id: str) -> bool:
    for folder in run_dir.glob(f"subquestions/*/{subquestion_id}"):
        if valid_agent_text(folder / "subagent-response.md"):
            return True
        if valid_agent_text(folder / "main-agent-fallback.md", fallback=True):
            return True
    return False


def coverage_blockers(packet: dict[str, Any], coverage: dict[str, Any]) -> list[Any]:
    blockers: list[Any] = []
    for source in [packet, coverage]:
        for key in ["blockers", "capture_blockers", "unresolved_blockers"]:
            value = source.get(key)
            if isinstance(value, list):
                blockers.extend(item for item in value if clean(item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)))
            elif clean(value):
                blockers.append(value)
    return blockers


def coverage_evidence_missing(packet: dict[str, Any], coverage: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for key in ["seed_evidence", "reference_evidence", "gap_evidence"]:
        value = coverage.get(key)
        if value is None:
            value = packet.get(key)
        if not isinstance(value, list) or not value:
            missing.append(key)
    return missing


def write_openalex_grounding_audit(
    run_dir: Path,
    subquestion_id: str,
    iteration_id: int,
    claim: str,
    subquestion_text: str,
    terms: list[str],
    api_key_present: bool | None = None,
    status: str = "recorded",
    error: str = "",
) -> Path:
    path = openalex_audit_path(run_dir, subquestion_id, iteration_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "subquestion_id": subquestion_id,
        "iteration_id": iteration_id,
        "claim": claim,
        "subquestion_text": subquestion_text,
        "openalex_grounding_requested": True,
        "api_key_present": bool(os.environ.get("OPENALEX_API_KEY")) if api_key_present is None else bool(api_key_present),
        "api_key_value_exposed": False,
        "status": status,
        "terms": discovery.unique_ordered([clean(term) for term in terms if clean(term)]),
        "error": error,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def refresh_openalex_grounding_audit(
    run_dir: Path,
    subquestion_id: str,
    iteration_id: int,
    claim: str,
    subquestion_text: str,
) -> Path:
    query_text = " ".join(part for part in [claim, subquestion_text] if clean(part))
    try:
        terms = discovery.openalex_grounding_terms(query_text)
        status = "ok" if terms else "no_terms_returned"
        return write_openalex_grounding_audit(
            run_dir,
            subquestion_id,
            iteration_id,
            claim,
            subquestion_text,
            terms,
            status=status,
        )
    except Exception as exc:  # defensive audit; do not expose secrets
        return write_openalex_grounding_audit(
            run_dir,
            subquestion_id,
            iteration_id,
            claim,
            subquestion_text,
            [],
            status="error",
            error=exc.__class__.__name__,
        )


def question_text(run_dir: Path) -> str:
    data = load_json(run_dir / "question.json", {})
    if isinstance(data, dict):
        return clean(data.get("question") or data.get("claim"))
    return ""


def evaluate_loop_state(
    run_dir: Path,
    subquestion_id: str,
    iteration_budget: int = DEFAULT_ITERATION_BUDGET,
    coverage_threshold: int = DEFAULT_COVERAGE_THRESHOLD,
    require_openalex_audit: bool = True,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    iteration_id = latest_iteration_id(run_dir, subquestion_id)
    packet = packet_item(run_dir, subquestion_id)
    if not packet:
        return {
            "terminal": False,
            "status": "subquestion_not_found",
            "required_action": "check_query_rounds_or_run_summary",
            "subquestion_id": subquestion_id,
            "iteration_id": iteration_id,
        }

    pending_notes = [
        article for article in packet.get("articles") or []
        if clean(article.get("note_status")) != "reading_note_ready"
    ]
    if pending_notes:
        return {
            "terminal": False,
            "status": "needs_reading_notes",
            "required_action": "write_reading_note_zh",
            "subquestion_id": subquestion_id,
            "iteration_id": iteration_id,
            "pending_article_count": len(pending_notes),
        }

    coverage = read_coverage_item(run_dir, subquestion_id)
    if not coverage:
        return {
            "terminal": False,
            "status": "needs_coverage_review_packet",
            "required_action": "run_subquestion_coverage_review",
            "subquestion_id": subquestion_id,
            "iteration_id": iteration_id,
        }

    decision = clean(coverage.get("coverage_decision"))
    score = numeric_score(coverage.get("coverage_score_0_to_5") or coverage.get("evidence_sufficiency_0_to_5"))
    stage_status = subquestion_coverage_review.effective_coverage_stage_status(coverage)
    blockers = coverage_blockers(packet, coverage)
    missing_evidence = coverage_evidence_missing(packet, coverage)
    base = {
        "subquestion_id": subquestion_id,
        "iteration_id": iteration_id,
        "coverage_decision": decision,
        "coverage_stage_status": stage_status,
        "coverage_score_0_to_5": score,
        "coverage_threshold": coverage_threshold,
        "iteration_budget": iteration_budget,
    }
    if decision in {"pending_subagent_review", ""} or score is None:
        return {
            **base,
            "terminal": False,
            "status": "needs_coverage_scoring",
            "required_action": "fill_coverage_review",
        }
    if decision == "sufficient" and score < coverage_threshold:
        return {
            **base,
            "terminal": False,
            "status": "needs_iteration_or_stop",
            "required_action": "set_iterate_query_or_stop_with_gaps",
        }
    if decision in {"sufficient", "stop_with_gaps"} and not subagent_response_exists(run_dir, subquestion_id):
        return {
            **base,
            "terminal": False,
            "status": "needs_subagent_response",
            "required_action": "write_subagent_response",
        }
    if blockers and decision in {"sufficient", "stop_with_gaps"}:
        return {
            **base,
            "terminal": False,
            "status": "unresolved_blockers",
            "required_action": "resolve_or_record_blocker",
            "blockers": blockers,
        }
    if stage_status == "needs_iteration_review":
        return {
            **base,
            "terminal": False,
            "status": "needs_iteration_review",
            "required_action": "build_and_review_query_iteration_plan",
        }
    if stage_status == "primary_pass_sufficient":
        return {
            **base,
            "terminal": False,
            "status": "primary_pass_sufficient",
            "required_action": "promote_to_final_sufficient_or_iteration_review",
        }
    if missing_evidence and decision in {"sufficient", "iterate_query"}:
        return {
            **base,
            "terminal": False,
            "status": "needs_coverage_evidence",
            "required_action": "add_seed_reference_gap_evidence",
            "missing_evidence": missing_evidence,
        }
    if decision in {"stop_with_gaps", "blocked"}:
        return {
            **base,
            "terminal": True,
            "status": decision,
            "required_action": "",
        }
    if decision == "sufficient" and score >= coverage_threshold:
        return {
            **base,
            "terminal": True,
            "status": "sufficient",
            "required_action": "",
        }
    if decision != "iterate_query" and score < coverage_threshold:
        return {
            **base,
            "terminal": False,
            "status": "needs_iteration_or_stop",
            "required_action": "set_iterate_query_or_stop_with_gaps",
        }
    if iteration_id >= iteration_budget:
        return {
            **base,
            "terminal": False,
            "status": "budget_exhausted_needs_stop_record",
            "required_action": "record_stop_with_gaps_or_blocker",
        }
    next_queries = normalize_next_queries(coverage.get("next_simple_queries"))
    if not next_queries:
        return {
            **base,
            "terminal": False,
            "status": "needs_next_queries",
            "required_action": "add_next_simple_queries",
        }
    audit_path = openalex_audit_path(run_dir, subquestion_id, iteration_id)
    if require_openalex_audit and not audit_path.exists():
        return {
            **base,
            "terminal": False,
            "status": "needs_openalex_grounding_audit",
            "required_action": "write_openalex_grounding_audit",
            "next_queries": next_queries,
            "openalex_audit_path": str(audit_path),
        }
    next_iteration_id = iteration_id + 1
    next_dir = loop_state_dir(run_dir, subquestion_id, next_iteration_id)
    if next_dir.exists() and not full_iteration_artifact_path(run_dir, subquestion_id, next_iteration_id).exists():
        return {
            **base,
            "terminal": False,
            "status": "needs_full_iteration_artifact",
            "required_action": "complete_next_iteration_loop",
            "next_iteration_id": next_iteration_id,
            "next_iteration_artifact": str(full_iteration_artifact_path(run_dir, subquestion_id, next_iteration_id)),
        }
    return {
        **base,
        "terminal": False,
        "status": "ready_for_query_iteration",
        "required_action": "run_continue_query_iteration",
        "next_iteration_id": next_iteration_id,
        "next_queries": next_queries,
        "openalex_audit_path": str(audit_path),
    }


def write_next_queries_csv(run_dir: Path, subquestion_id: str, output_path: Path | None = None) -> Path:
    raise SystemExit(
        "Direct next_simple_queries export is disabled. Write query-rationale-review.json, "
        "run query_iteration_review.py, and continue from the approved query-plan-amendment.json."
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--subquestion-id", required=True)
    parser.add_argument("--iteration-budget", type=int, default=DEFAULT_ITERATION_BUDGET)
    parser.add_argument("--coverage-threshold", type=int, default=DEFAULT_COVERAGE_THRESHOLD)
    parser.add_argument("--no-openalex-audit-required", dest="require_openalex_audit", action="store_false")
    parser.add_argument("--refresh-openalex-audit", action="store_true")
    parser.add_argument("--state-json", type=Path)
    parser.set_defaults(require_openalex_audit=True)
    args = parser.parse_args()

    if args.refresh_openalex_audit:
        packet = packet_item(args.run_dir, args.subquestion_id)
        refresh_openalex_grounding_audit(
            args.run_dir,
            args.subquestion_id,
            latest_iteration_id(args.run_dir, args.subquestion_id),
            question_text(args.run_dir),
            clean(packet.get("claim_subquestion") or packet.get("subquestion_text")),
        )

    state = evaluate_loop_state(
        args.run_dir,
        args.subquestion_id,
        iteration_budget=args.iteration_budget,
        coverage_threshold=args.coverage_threshold,
        require_openalex_audit=args.require_openalex_audit,
    )
    state_json = json.dumps(state, ensure_ascii=False, indent=2)
    print(state_json)
    if args.state_json:
        args.state_json.parent.mkdir(parents=True, exist_ok=True)
        args.state_json.write_text(state_json + "\n", encoding="utf-8")
    return 0 if state.get("terminal") or state.get("status") in SUCCESS_HANDOFF_STATUSES else 2


if __name__ == "__main__":
    raise SystemExit(main())
