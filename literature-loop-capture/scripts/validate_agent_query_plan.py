#!/usr/bin/env python3
"""Validate an agent-authored query plan before publisher URL generation."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import discovery_core as discovery  # noqa: E402
import query_plan_common as common  # noqa: E402


STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "for",
    "from",
    "how",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"Could not read JSON from {path}: {exc}") from exc


def query_tokens(query: str) -> set[str]:
    tokens = {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]*", common.clean(query))
        if token.lower() not in STOPWORDS
    }
    return tokens


def query_overlap(left: str, right: str) -> float:
    left_tokens = query_tokens(left)
    right_tokens = query_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def normalize_query_item(item: Any, subquestion_id: str, query_index: int) -> dict[str, Any]:
    require(isinstance(item, dict), f"{subquestion_id} query {query_index} must be an object, not a bare string.")
    query = common.clean(item.get("query"))
    require(query, f"{subquestion_id} query {query_index} is missing query.")
    require(not common.contains_cjk(query), f"{subquestion_id} query must be English: {query}")
    require(discovery.is_simple_publisher_query(query), f"{subquestion_id} query is not publisher-friendly: {query}")
    for field in ["anchor_type", "expected_result_type", "non_redundancy_rationale"]:
        require(common.clean(item.get(field)), f"{subquestion_id} query `{query}` missing {field}.")
    evidence = item.get("evidence_source")
    require(isinstance(evidence, list) and any(common.clean(value) for value in evidence), f"{subquestion_id} query `{query}` missing evidence_source.")
    return {
        "query": query,
        "anchor_type": common.clean(item.get("anchor_type")).lower(),
        "evidence_source": [common.clean(value) for value in evidence if common.clean(value)],
        "expected_result_type": common.clean(item.get("expected_result_type")),
        "non_redundancy_rationale": common.clean(item.get("non_redundancy_rationale")),
        "allow_high_overlap": bool(item.get("allow_high_overlap")),
        "rcs_seed_reasoning": common.clean(item.get("rcs_seed_reasoning")),
    }


def normalize_subquestion(item: Any, index: int) -> dict[str, Any]:
    require(isinstance(item, dict), f"subquestion {index} must be an object.")
    subquestion_id = common.clean(item.get("subquestion_id") or item.get("id"))
    require(subquestion_id, f"subquestion {index} missing subquestion_id.")
    require(common.clean(item.get("subquestion_text") or item.get("claim_subquestion")), f"{subquestion_id} missing subquestion_text.")
    require(common.clean(item.get("query_family")), f"{subquestion_id} missing query_family.")
    queries = item.get("queries")
    require(isinstance(queries, list) and queries, f"{subquestion_id} must include at least one query object.")
    normalized_queries = [
        normalize_query_item(query_item, subquestion_id, query_index)
        for query_index, query_item in enumerate(queries, start=1)
    ]
    return {
        "subquestion_id": subquestion_id,
        "subquestion_slug": common.clean(item.get("subquestion_slug")) or subquestion_id,
        "subquestion_group_slug": common.clean(item.get("subquestion_group_slug")) or "agent-reviewed",
        "subquestion_group_title": common.clean(item.get("subquestion_group_title")) or "Agent Reviewed",
        "subquestion_text": common.clean(item.get("subquestion_text") or item.get("claim_subquestion")),
        "query_family": common.clean(item.get("query_family")),
        "concept_groups": item.get("concept_groups") if isinstance(item.get("concept_groups"), list) else [],
        "boolean_query": common.clean(item.get("boolean_query")),
        "query_rationale": common.clean(item.get("query_rationale")),
        "queries": normalized_queries,
    }


def enforce_query_diversity(subquestions: list[dict[str, Any]]) -> None:
    seen: dict[str, str] = {}
    flattened: list[tuple[str, dict[str, Any]]] = []
    for subquestion in subquestions:
        subquestion_id = subquestion["subquestion_id"]
        for query_item in subquestion["queries"]:
            query = query_item["query"]
            key = query.lower()
            if key in seen:
                raise SystemExit(f"duplicate query `{query}` in {subquestion_id}; first seen in {seen[key]}.")
            seen[key] = subquestion_id
            flattened.append((subquestion_id, query_item))
    for left_index, (left_subq, left_item) in enumerate(flattened):
        for right_subq, right_item in flattened[left_index + 1 :]:
            overlap = query_overlap(left_item["query"], right_item["query"])
            if overlap <= 0.55:
                continue
            if left_item.get("allow_high_overlap") and right_item.get("allow_high_overlap"):
                combined = left_item["non_redundancy_rationale"] + " " + right_item["non_redundancy_rationale"]
                if len(combined) >= 120 and left_item["anchor_type"] != right_item["anchor_type"]:
                    continue
            raise SystemExit(
                "query overlap too high between "
                f"{left_subq} `{left_item['query']}` and {right_subq} `{right_item['query']}` "
                f"(jaccard={overlap:.2f}). Rewrite around distinct concept anchors."
            )


def validate_payload(payload: dict[str, Any], grounding: dict[str, Any]) -> dict[str, Any]:
    require(isinstance(payload, dict), "agent query plan must be a JSON object.")
    common.validate_openalex_grounding(grounding)
    question = common.clean(payload.get("english_big_question"))
    require(question, "agent query plan missing english_big_question.")
    require(not common.contains_cjk(question), "agent query plan english_big_question must be English.")
    owner = common.clean(payload.get("agent_owner"))
    require(owner and owner.lower() not in {"script", "python", "auto"}, "agent query plan must name a real agent owner or main_agent_fallback.")
    grounding_notes = common.clean(payload.get("grounding_notes"))
    require(grounding_notes and "AGENT REQUIRED" not in grounding_notes, "agent query plan must include non-placeholder grounding_notes.")
    subquestions_raw = payload.get("subquestions")
    require(isinstance(subquestions_raw, list) and subquestions_raw, "agent query plan must include subquestions.")
    subquestions = [normalize_subquestion(item, index) for index, item in enumerate(subquestions_raw, start=1)]
    enforce_query_diversity(subquestions)
    exploration_sources = payload.get("exploration_sources")
    if not isinstance(exploration_sources, list):
        exploration_sources = []
    exploration_sources = common.ensure_required_exploration_sources(
        [source for source in exploration_sources if isinstance(source, dict)],
        grounding,
    )
    return {
        "schema_version": 1,
        "english_big_question": question,
        "agent_owner": owner,
        "grounding_notes": grounding_notes,
        "exploration_sources": exploration_sources,
        "openalex_grounding": grounding,
        "subquestions": subquestions,
    }


def validate_run_dir(
    run_dir: Path,
    *,
    plan_path: Path | None = None,
    grounding_path: Path | None = None,
    write: bool = True,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    plan_path = plan_path or run_dir / "agent-query-plan.json"
    grounding_path = grounding_path or run_dir / "openalex-grounding.json"
    require(plan_path.exists(), f"missing agent-authored query plan: {plan_path}")
    require(grounding_path.exists(), f"missing OpenAlex grounding artifact: {grounding_path}")
    payload = load_json(plan_path)
    grounding = load_json(grounding_path)
    normalized = validate_payload(payload, grounding)
    if write:
        (run_dir / "agent-query-plan-validated.json").write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return normalized


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--grounding", type=Path)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()
    normalized = validate_run_dir(
        args.run_dir,
        plan_path=args.plan,
        grounding_path=args.grounding,
        write=not args.no_write,
    )
    print(f"validated_subquestions={len(normalized['subquestions'])}")
    print(f"validated_plan={(args.run_dir.resolve() / 'agent-query-plan-validated.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
