#!/usr/bin/env python3
"""Build publisher search URLs from a validated agent query plan."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import discovery_core as discovery  # noqa: E402
import query_plan_common as common  # noqa: E402
import validate_agent_query_plan  # noqa: E402


def query_texts(subquestion: dict[str, Any]) -> list[str]:
    return [query["query"] for query in subquestion.get("queries") or [] if common.clean(query.get("query"))]


def query_provenance(subquestion: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for query in subquestion.get("queries") or []:
        rows.append(
            {
                "query": query.get("query") or "",
                "anchor_type": query.get("anchor_type") or "",
                "evidence_source": query.get("evidence_source") or [],
                "expected_result_type": query.get("expected_result_type") or "",
                "non_redundancy_rationale": query.get("non_redundancy_rationale") or "",
                "agent_authored": True,
            }
        )
    return rows


def publisher_queries_for_query(
    query: str,
    *,
    year_start: int,
    year_end: int,
    sciencedirect_route: str,
    sciencedirect_article_types: str,
    include_springer: bool,
) -> dict[str, str]:
    publisher_queries = discovery.publisher_query_urls(
        query,
        year_start,
        year_end,
        sciencedirect_route,
        sciencedirect_article_types,
        include_springer,
    )
    return {
        key: value
        for key, value in publisher_queries.items()
        if key in common.SUPPORTED_PUBLISHERS
    }


def build_subquestion_payload(
    subquestion: dict[str, Any],
    index: int,
    *,
    year_start: int,
    year_end: int,
    sciencedirect_route: str,
    sciencedirect_article_types: str,
    include_springer: bool,
) -> dict[str, Any]:
    queries = query_texts(subquestion)
    first_query = queries[0] if queries else subquestion.get("query_family", "")
    publisher_query_sets = [
        {
            "query": query,
            "publisher_queries": publisher_queries_for_query(
                query,
                year_start=year_start,
                year_end=year_end,
                sciencedirect_route=sciencedirect_route,
                sciencedirect_article_types=sciencedirect_article_types,
                include_springer=include_springer,
            ),
        }
        for query in queries
    ]
    publisher_queries = publisher_query_sets[0]["publisher_queries"] if publisher_query_sets else (
        publisher_queries_for_query(
            first_query,
            year_start=year_start,
            year_end=year_end,
            sciencedirect_route=sciencedirect_route,
            sciencedirect_article_types=sciencedirect_article_types,
            include_springer=include_springer,
        )
        if first_query
        else {}
    )
    return {
        "subquestion_id": subquestion["subquestion_id"],
        "subquestion_slug": subquestion.get("subquestion_slug") or subquestion["subquestion_id"],
        "subquestion_group_slug": subquestion.get("subquestion_group_slug") or "agent-reviewed",
        "subquestion_group_title": subquestion.get("subquestion_group_title") or "Agent Reviewed",
        "round": index,
        "query_family": subquestion.get("query_family") or subquestion["subquestion_id"],
        "subquestion_text": subquestion.get("subquestion_text") or "",
        "concept_groups": subquestion.get("concept_groups") or [],
        "boolean_query": subquestion.get("boolean_query") or "",
        "publisher_queries": publisher_queries,
        "publisher_query_sets": publisher_query_sets,
        "source_targets": [],
        "publisher_targets": common.publisher_targets_for_queries(publisher_queries),
        "publisher_discovery_plan": {
            "search_limit_per_query": 20,
            "scrape_shortlist_limit": 5,
            "use_research_category": True,
            "use_research_index": False,
            "use_publisher_advanced_pages": True,
        },
        "queries": queries,
        "query_provenance": query_provenance(subquestion),
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Query Plan Preview",
        "",
        f"- Generated: {payload.get('generated_at') or ''}",
        f"- English big question: {payload.get('english_big_question') or ''}",
        f"- Agent plan: `{payload.get('agent_query_plan_path') or ''}`",
        f"- Validated plan: `{payload.get('validated_agent_query_plan_path') or ''}`",
        "- Grounding source: OpenAlex metadata only",
        "",
        "## Publisher Focus",
        "",
    ]
    focus = payload.get("publisher_focus") or {}
    counts = focus.get("counts") or {}
    for key in common.SUPPORTED_PUBLISHERS:
        lines.append(f"- `{key}`: {counts.get(key, 0)} OpenAlex metadata matches")
    lines.extend(["", "## Subquestions", ""])
    for subquestion in payload.get("subquestions") or []:
        lines.extend(
            [
                f"### {subquestion.get('subquestion_id')}: {subquestion.get('query_family')}",
                "",
                f"- Group: {subquestion.get('subquestion_group_title')} (`{subquestion.get('subquestion_group_slug')}`)",
                f"- Subquestion: {subquestion.get('subquestion_text')}",
                "- Agent-authored queries:",
            ]
        )
        provenance = {row.get("query"): row for row in subquestion.get("query_provenance") or []}
        for query in subquestion.get("queries") or []:
            row = provenance.get(query) or {}
            lines.append(f"  - `{query}`")
            lines.append(f"    - anchor: {row.get('anchor_type') or ''}")
            lines.append(f"    - expected result: {row.get('expected_result_type') or ''}")
            lines.append(f"    - non-redundancy: {row.get('non_redundancy_rationale') or ''}")
            evidence = ", ".join(str(value) for value in row.get("evidence_source") or [])
            lines.append(f"    - evidence: {evidence}")
        lines.append("- Publisher URLs by query:")
        for query_set in subquestion.get("publisher_query_sets") or []:
            lines.append(f"  - query: `{query_set.get('query') or ''}`")
            for publisher, url in sorted((query_set.get("publisher_queries") or {}).items()):
                lines.append(f"    - {publisher}: `{url}`")
        if not subquestion.get("publisher_query_sets"):
            lines.append("- Publisher URL seed:")
            for publisher, url in sorted((subquestion.get("publisher_queries") or {}).items()):
                lines.append(f"  - {publisher}: `{url}`")
        lines.append("")
    lines.extend(
        [
            "## Approval Gate",
            "",
            "Do not start OpenCLI publisher discovery, publisher full-text retrieval, or publisher capture until this agent-authored plan is approved.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def build_preview(
    run_dir: Path,
    validated_payload: dict[str, Any] | None = None,
    *,
    year_start: int,
    year_end: int,
    sciencedirect_route: str = "direct",
    sciencedirect_article_types: str = "FLA,REV",
    include_springer: bool = True,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    validated_payload = validated_payload or validate_agent_query_plan.validate_run_dir(run_dir)
    subquestions = [
        build_subquestion_payload(
            subquestion,
            index,
            year_start=year_start,
            year_end=year_end,
            sciencedirect_route=sciencedirect_route,
            sciencedirect_article_types=sciencedirect_article_types,
            include_springer=include_springer,
        )
        for index, subquestion in enumerate(validated_payload.get("subquestions") or [], start=1)
    ]
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "english_big_question": validated_payload["english_big_question"],
        "grounding_notes": validated_payload["grounding_notes"],
        "grounding_notes_path": "grounding-notes.md",
        "openalex_grounding_path": "openalex-grounding.md",
        "exploration_sources_path": "exploration-sources.csv",
        "agent_query_plan_path": "agent-query-plan.json",
        "validated_agent_query_plan_path": "agent-query-plan-validated.json",
        "exploration_sources": validated_payload.get("exploration_sources") or [],
        "openalex_grounding": validated_payload["openalex_grounding"],
        "publisher_focus": common.publisher_focus_from_openalex(validated_payload["openalex_grounding"]),
        "requires_user_approval": True,
        "requires_user_approval_before_capture": True,
        "discovery_backend": "opencli",
        "subquestions": subquestions,
    }
    (run_dir / "query-plan-preview.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "query-plan-preview.md").write_text(render_markdown(payload), encoding="utf-8")
    (run_dir / "query-rounds.json").write_text(
        json.dumps(
            [
                {
                    "subquestion_id": item["subquestion_id"],
                    "query_family": item["query_family"],
                    "subquestion_text": item["subquestion_text"],
                    "queries": item["queries"],
                }
                for item in subquestions
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--year-start", type=int, default=datetime.now().year - 4)
    parser.add_argument("--year-end", type=int, default=datetime.now().year)
    parser.add_argument("--sciencedirect-route", choices=["direct"], default="direct")
    parser.add_argument("--sciencedirect-article-types", default="FLA,REV")
    parser.add_argument("--no-springer", dest="include_springer", action="store_false")
    parser.set_defaults(include_springer=True)
    args = parser.parse_args()
    payload = build_preview(
        args.run_dir,
        year_start=args.year_start,
        year_end=args.year_end,
        sciencedirect_route=args.sciencedirect_route,
        sciencedirect_article_types=args.sciencedirect_article_types,
        include_springer=args.include_springer,
    )
    print(f"query_plan_json={(args.run_dir.resolve() / 'query-plan-preview.json')}")
    print(f"query_plan_md={(args.run_dir.resolve() / 'query-plan-preview.md')}")
    print(f"subquestions={len(payload['subquestions'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
