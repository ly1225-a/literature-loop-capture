#!/usr/bin/env python3
"""Write OpenAlex grounding packets for agent-owned query planning."""

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


def agent_plan_template(claim: str, rounds: int) -> dict[str, Any]:
    return {
        "english_big_question": claim,
        "agent_owner": "main_agent_fallback or subagent id/name",
        "grounding_notes": "Agent-written synthesis of OpenAlex titles, abstracts, topics, venues, DOI/year/citation signals.",
        "exploration_sources": [
            {
                "label": "OpenAlex",
                "url": "https://openalex.org",
                "note": "metadata grounding with work-level records",
            }
        ],
        "subquestions": [
            {
                "subquestion_id": f"{index:02d}_agent_named_focus",
                "subquestion_group_slug": "agent-named-group",
                "subquestion_group_title": "Agent Named Group",
                "subquestion_text": "Agent-authored atomic subquestion grounded in OpenAlex evidence.",
                "query_family": "agent-authored-focus",
                "queries": [
                    {
                        "query": "publisher friendly phrase",
                        "anchor_type": "schema|method|resource|evaluation|application|gap|seed",
                        "evidence_source": ["OpenAlex: cited title/topic/keyword that justifies the query"],
                        "expected_result_type": "What this query should retrieve and why it is not a suffix variant.",
                        "non_redundancy_rationale": "Explain how this query probes a distinct concept axis from the other queries.",
                    }
                ],
            }
            for index in range(1, max(1, rounds) + 1)
        ],
    }


def render_agent_packet(claim: str, rounds: int, openalex_audit: dict[str, Any]) -> str:
    focus = common.publisher_focus_from_openalex(openalex_audit)
    lines = [
        "# Agent Query Plan Packet",
        "",
        f"- Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"- English big question: {claim}",
        f"- Required subquestions: {rounds}",
        "- Required output: `agent-query-plan.json`",
        "- Next command: `validate_agent_query_plan.py <run_dir>` then `build_publisher_urls.py <run_dir>`",
        "",
        "## Non-Negotiable Boundary",
        "",
        "Python has already written OpenAlex metadata. The agent must now author the query plan.",
        "Do not copy suffix variants such as `food flavor knowledge graph dataset/method/terminology` across subquestions.",
        "Each query needs a distinct concept anchor, evidence source, expected result type, and non-redundancy rationale.",
        "",
        "## Query Design Guidance",
        "",
        "- Use OpenAlex titles, abstracts, topics, keywords, venues, DOI/year/citation signals as grounding.",
        "- Prefer distinct concept axes: schema/ontology, flavor molecule resources, graph completion, evaluation, application, gaps.",
        "- Keep publisher queries simple enough for publisher search forms; put Boolean thinking in the rationale, not in the query text.",
        "- If two queries share most words, rewrite one around a different anchor or explicitly justify an exception.",
        "",
        "## Broad OpenAlex Probe Queries",
        "",
        "These are metadata-grounding probes for agent terminology discovery, not final publisher queries.",
        "",
    ]
    for query in openalex_audit.get("probe_queries") or []:
        lines.append(f"- `{query}`")
    lines.extend([
        "",
        "## Open Concept Hints",
        "",
        "Use these as optional terminology blocks while authoring the query plan; keep or discard them based on the metadata evidence.",
        "",
    ])
    for hint in openalex_audit.get("concept_hints") or []:
        terms = ", ".join(f"`{term}`" for term in hint.get("terms") or [])
        lines.extend([
            f"### {hint.get('label') or 'concept hint'}",
            "",
            f"- Purpose: {hint.get('purpose') or ''}",
            f"- Terms: {terms}",
            "",
        ])
    lines.extend([
        "## Supported Publisher Focus From OpenAlex",
        "",
    ])
    for key in common.SUPPORTED_PUBLISHERS:
        lines.append(f"- `{key}`: {focus['counts'].get(key, 0)} metadata matches")
    lines.extend(["", "## OpenAlex Works", ""])
    for index, work in enumerate(openalex_audit.get("works") or [], start=1):
        lines.extend(
            [
                f"### {index}. {work.get('title') or 'Untitled'}",
                "",
                f"- Year: {work.get('year') or ''}",
                f"- Venue: {work.get('venue') or ''}",
                f"- Publisher: {work.get('publisher') or work.get('host_organization_name') or ''}",
                f"- DOI: {work.get('doi') or ''}",
                f"- Cited by: {work.get('cited_by_count') or 0}",
                f"- Primary topic: {work.get('primary_topic') or ''}",
                f"- Topics: {', '.join(str(item) for item in work.get('topics') or [])}",
                f"- Keywords: {', '.join(str(item) for item in work.get('keywords') or [])}",
                "",
                work.get("abstract_excerpt") or "No abstract excerpt in OpenAlex metadata.",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--claim", required=True, help="English-normalized big question.")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--year-start", type=int, default=datetime.now().year - 4)
    parser.add_argument("--year-end", type=int, default=datetime.now().year)
    parser.add_argument("--grounding-notes", default="", help="Optional agent notes to include in grounding-notes.md.")
    parser.add_argument(
        "--exploration-source",
        action="append",
        default=[],
        help="Exploration source used for framing, as 'label|url|note'. Repeatable.",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--allow-non-english", action="store_true")
    args = parser.parse_args()

    if not args.allow_non_english and common.contains_cjk(args.claim):
        raise SystemExit(
            "openalex_grounding.py requires English claim text. The agent should translate the user question first."
        )

    openalex_audit = discovery.openalex_grounding_audit(args.claim, requested=True)
    common.validate_openalex_grounding(openalex_audit)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    exploration_sources = common.ensure_required_exploration_sources(
        [common.parse_exploration_source(value) for value in args.exploration_source],
        openalex_audit,
    )
    (args.output_dir / "openalex-grounding.json").write_text(
        json.dumps(openalex_audit, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (args.output_dir / "openalex-grounding.md").write_text(
        common.render_openalex_grounding(openalex_audit),
        encoding="utf-8",
    )
    (args.output_dir / "grounding-notes.md").write_text(
        common.render_grounding_notes(args.claim, args.grounding_notes, exploration_sources, openalex_audit),
        encoding="utf-8",
    )
    common.write_exploration_sources(args.output_dir, exploration_sources)
    (args.output_dir / "agent-query-plan-packet.md").write_text(
        render_agent_packet(args.claim, args.rounds, openalex_audit),
        encoding="utf-8",
    )
    (args.output_dir / "agent-query-plan-template.json").write_text(
        json.dumps(agent_plan_template(args.claim, args.rounds), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"openalex_grounding={(args.output_dir / 'openalex-grounding.json').resolve()}")
    print(f"agent_packet={(args.output_dir / 'agent-query-plan-packet.md').resolve()}")
    print(f"agent_template={(args.output_dir / 'agent-query-plan-template.json').resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
