"""Shared helpers for agent-owned query planning artifacts."""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import source_registry


SUPPORTED_PUBLISHERS = ["elsevier", "acs", "wiley", "springer"]


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def parse_exploration_source(value: str) -> dict[str, str]:
    parts = [part.strip() for part in (value or "").split("|", 2)]
    while len(parts) < 3:
        parts.append("")
    return {"label": parts[0], "url": parts[1], "note": parts[2]}


def ensure_required_exploration_sources(
    sources: list[dict[str, str]],
    openalex_audit: dict[str, Any],
) -> list[dict[str, str]]:
    normalized = list(sources)
    source_text = " ".join(
        " ".join(str(source.get(key) or "") for key in ["label", "url", "note"])
        for source in normalized
    ).lower()
    if "openalex" not in source_text:
        status = openalex_audit.get("status") or "unknown"
        api_state = "api_key_present=true" if openalex_audit.get("api_key_present") else "api_key_present=false"
        terms = ", ".join(str(term) for term in (openalex_audit.get("terms") or [])[:8]) or "none"
        normalized.append(
            {
                "label": "OpenAlex",
                "url": "https://openalex.org",
                "note": f"metadata grounding status={status}; {api_state}; terms={terms}",
            }
        )
    return normalized


def write_exploration_sources(output_dir: Path, sources: list[dict[str, str]]) -> None:
    (output_dir / "exploration-sources.json").write_text(
        json.dumps(sources, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with (output_dir / "exploration-sources.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["label", "url", "note"], extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sources)


def validate_openalex_grounding(openalex_audit: dict[str, Any]) -> None:
    if not openalex_audit.get("requested"):
        raise SystemExit("OpenAlex grounding is mandatory for this workflow.")
    if not openalex_audit.get("api_key_present"):
        raise SystemExit("OpenAlex grounding requires OPENALEX_API_KEY in the environment.")
    if openalex_audit.get("status") != "ok" or not openalex_audit.get("terms"):
        raise SystemExit("OpenAlex grounding must return non-empty metadata terms before query planning.")
    if not openalex_audit.get("works"):
        raise SystemExit("OpenAlex grounding must return work-level metadata before query planning.")


def supported_publisher_for_openalex_work(work: dict[str, Any]) -> str:
    fields = [
        "publisher",
        "venue",
        "source_display_name",
        "source_name",
        "host_organization_name",
        "landing_page_url",
    ]
    haystack = " ".join(str(work.get(key) or "") for key in fields).lower()
    if "elsevier" in haystack or "sciencedirect" in haystack:
        return "elsevier"
    if "american chemical society" in haystack or "pubs.acs" in haystack or re.search(r"\bacs\b", haystack):
        return "acs"
    if "wiley" in haystack:
        return "wiley"
    if "springer" in haystack:
        return "springer"
    return ""


def publisher_focus_from_openalex(openalex_audit: dict[str, Any]) -> dict[str, Any]:
    counts = {key: 0 for key in SUPPORTED_PUBLISHERS}
    evidence: dict[str, list[dict[str, Any]]] = {key: [] for key in SUPPORTED_PUBLISHERS}
    unsupported_work_count = 0
    for work in openalex_audit.get("works") or []:
        if not isinstance(work, dict):
            continue
        key = supported_publisher_for_openalex_work(work)
        if not key:
            unsupported_work_count += 1
            continue
        counts[key] += 1
        if len(evidence[key]) < 8:
            evidence[key].append(
                {
                    "title": work.get("title") or "",
                    "year": work.get("year") or "",
                    "venue": work.get("venue") or work.get("source_display_name") or "",
                    "publisher": work.get("publisher") or work.get("host_organization_name") or "",
                    "doi": work.get("doi") or "",
                    "cited_by_count": work.get("cited_by_count") or 0,
                }
            )
    return {
        "source": "openalex",
        "supported_publishers": SUPPORTED_PUBLISHERS,
        "counts": counts,
        "evidence": evidence,
        "unsupported_work_count": unsupported_work_count,
    }


def render_openalex_grounding(openalex_audit: dict[str, Any]) -> str:
    lines = [
        "# OpenAlex Grounding Evidence",
        "",
        f"- Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"- Status: `{openalex_audit.get('status') or ''}`",
        f"- API key present: `{str(bool(openalex_audit.get('api_key_present'))).lower()}`",
        f"- Terms: {', '.join(openalex_audit.get('terms') or []) or 'none'}",
        "",
    ]
    hints = openalex_audit.get("concept_hints") or []
    if hints:
        lines.extend([
            "## Claim Vocabulary Hints",
            "",
            "These terms are extracted from the current claim only. They are orientation hints, not a fixed subquestion template.",
            "",
        ])
        for hint in hints:
            terms = ", ".join(f"`{term}`" for term in hint.get("terms") or [])
            lines.extend([
                f"### {hint.get('label') or 'claim vocabulary'}",
                "",
                f"- Purpose: {hint.get('purpose') or ''}",
                f"- Terms: {terms}",
                "",
            ])
    for index, work in enumerate(openalex_audit.get("works") or [], start=1):
        lines.extend(
            [
                f"## {index}. {work.get('title') or 'Untitled'}",
                "",
                f"- Query: `{work.get('query') or ''}`",
                f"- Year: {work.get('year') or ''}",
                f"- Venue: {work.get('venue') or ''}",
                f"- Publisher: {work.get('publisher') or work.get('host_organization_name') or ''}",
                f"- DOI: {work.get('doi') or ''}",
                f"- Cited by: {work.get('cited_by_count') or 0}",
                f"- Authors: {', '.join(str(item) for item in work.get('authors') or [])}",
                f"- Primary topic: {work.get('primary_topic') or ''}",
                f"- Topics: {', '.join(str(item) for item in work.get('topics') or [])}",
                f"- Keywords: {', '.join(str(item) for item in work.get('keywords') or [])}",
                "",
                "### Abstract Excerpt",
                work.get("abstract_excerpt") or "No abstract excerpt in OpenAlex metadata.",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_grounding_notes(
    claim: str,
    grounding_notes: str,
    exploration_sources: list[dict[str, str]],
    openalex_audit: dict[str, Any],
) -> str:
    lines = [
        "# Grounding Notes",
        "",
        f"- Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"- English big question: {claim}",
        f"- OpenAlex status: {openalex_audit.get('status') or 'unknown'}",
        f"- OpenAlex API key present: {str(bool(openalex_audit.get('api_key_present'))).lower()}",
        f"- OpenAlex metadata terms: {', '.join(openalex_audit.get('terms') or []) or 'none'}",
        "",
        "## Agent Broad Exploration",
        "",
    ]
    if grounding_notes:
        lines.append(grounding_notes.strip())
    else:
        lines.extend(
            [
                "AGENT REQUIRED: summarize baseline knowledge and OpenAlex metadata here before authoring the query plan.",
                "Do not treat this script-generated placeholder as sufficient exploration evidence.",
            ]
        )
    lines.extend(["", "## Exploration Sources", ""])
    for source in exploration_sources:
        label = source.get("label") or "source"
        url = source.get("url") or ""
        note = source.get("note") or ""
        lines.append(f"- {label}: {url} - {note}".rstrip(" -"))
    lines.extend(["", "## OpenAlex Metadata Seen By Agent", ""])
    works = openalex_audit.get("works") or []
    if not works:
        lines.append("No OpenAlex work-level metadata was available; do not proceed to discovery for broad questions.")
    for index, work in enumerate(works[:10], start=1):
        lines.extend(
            [
                f"### {index}. {work.get('title') or 'Untitled'}",
                "",
                f"- Year: {work.get('year') or ''}",
                f"- Venue: {work.get('venue') or ''}",
                f"- Publisher: {work.get('publisher') or work.get('host_organization_name') or ''}",
                f"- DOI: {work.get('doi') or ''}",
                f"- Primary topic: {work.get('primary_topic') or ''}",
                f"- Topics: {', '.join(str(item) for item in work.get('topics') or [])}",
                f"- Keywords: {', '.join(str(item) for item in work.get('keywords') or [])}",
                "",
                work.get("abstract_excerpt") or "No abstract excerpt in OpenAlex metadata.",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def publisher_targets_for_queries(publisher_queries: dict[str, str]) -> list[dict[str, Any]]:
    targets = []
    for priority, key in enumerate([key for key in SUPPORTED_PUBLISHERS if key in publisher_queries], start=1):
        adapter = source_registry.get_adapter(key)
        if not adapter:
            continue
        targets.append(
            {
                "key": key,
                "kind": adapter.kind,
                "enabled": True,
                "priority": priority,
                "activation_reason": "agent-authored query validated for supported structured publisher route",
                "domains": adapter.domains,
            }
        )
    return targets
