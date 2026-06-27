#!/usr/bin/env python3
"""Build clean per-subquestion abstract-capture review packets."""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


ARTICLE_BUCKETS = ("capture_articles", "maybe_articles", "skip_articles")
ROW_FIELDS = [
    "row_id",
    "rank",
    "title",
    "href",
    "doi",
    "publisher",
    "year",
    "journal",
    "query_text",
    "query_family",
    "context",
    "abstract",
    "abstract_source",
    "status",
]


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def safe_name(value: Any, fallback: str = "subquestion") -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", clean(value)).strip("._-")
    return text or fallback


def read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def subquestion_map(query_plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    subquestions = query_plan.get("subquestions") if isinstance(query_plan.get("subquestions"), list) else []
    for item in subquestions:
        if not isinstance(item, dict):
            continue
        subquestion_id = clean(item.get("subquestion_id"))
        if subquestion_id:
            out[subquestion_id] = item
    return out


def concept_summary(item: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for group in item.get("concept_groups") or []:
        if not isinstance(group, dict):
            continue
        label = clean(group.get("label") or group.get("name"))
        terms = ", ".join(clean(term) for term in (group.get("terms") or []) if clean(term))
        if label or terms:
            lines.append(f"{label}: {terms}" if label else terms)
    return lines


def query_basis(item: dict[str, Any]) -> dict[str, Any]:
    provenance = []
    for row in item.get("query_provenance") or []:
        if not isinstance(row, dict):
            continue
        provenance.append({
            "query": clean(row.get("query")),
            "why": clean(row.get("non_redundancy_rationale")),
            "expected_evidence": clean(row.get("expected_result_type")),
            "openalex_seed": [clean(value) for value in (row.get("evidence_source") or []) if clean(value)],
        })
    return {
        "subquestion_id": clean(item.get("subquestion_id")),
        "subquestion_text": clean(item.get("subquestion_text")),
        "query_family": clean(item.get("query_family")),
        "reading_lens": clean(item.get("subquestion_reading_lens") or item.get("reading_lens")),
        "planning_rationale": clean(item.get("query_rationale")),
        "concept_anchors": concept_summary(item),
        "approved_queries": [clean(query) for query in (item.get("queries") or []) if clean(query)],
        "query_provenance": provenance,
    }


def normalize_row(row: dict[str, Any], index: int) -> dict[str, str]:
    rank = clean(row.get("rank") or row.get("unique_rank") or index)
    title = clean(row.get("title") or row.get("page_title"))
    href = clean(row.get("href") or row.get("landing_url") or row.get("url"))
    row_id = clean(row.get("row_id")) or f"{rank}|{href or title}"
    return {
        "row_id": row_id,
        "rank": rank,
        "title": title,
        "href": href,
        "doi": clean(row.get("doi")),
        "publisher": clean(row.get("publisher") or row.get("publisher_key") or row.get("source_key")),
        "year": clean(row.get("year")),
        "journal": clean(row.get("journal")),
        "query_text": clean(row.get("query_text") or row.get("query")),
        "query_family": clean(row.get("query_family")),
        "context": clean(row.get("context") or row.get("search_description"))[:1400],
        "abstract": clean(row.get("abstract"))[:4000],
        "abstract_source": clean(row.get("abstract_source")),
        "status": clean(row.get("status")),
    }


def row_identity(row: dict[str, Any]) -> str:
    doi = clean(row.get("doi")).lower()
    if doi:
        return "doi:" + doi
    href = clean(row.get("href")).lower()
    if href:
        return "href:" + href
    return "title:" + clean(row.get("title")).lower()


def dedupe_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    duplicates = 0
    for row in rows:
        identity = row_identity(row)
        if identity in seen:
            duplicates += 1
            continue
        seen.add(identity)
        out.append(row)
    return out, duplicates


def empty_review_template(subquestion_id: str, expected_count: int) -> dict[str, Any]:
    return {
        "review_mode": "subagent",
        "agent_id": "",
        "review_phase": "abstract_capture_review",
        "subquestion_id": subquestion_id,
        "reviewed_count": expected_count,
        "capture_articles": [],
        "maybe_articles": [],
        "skip_articles": [],
        "notes": "",
    }


def render_prompt(packet: dict[str, Any], output_json: Path, output_md: Path) -> str:
    basis = packet["query_plan_basis"]
    lines = [
        "# Abstract Capture Review Worker Prompt",
        "",
        "You are a scoring worker for one atomic literature-review subquestion. Use only this packet.",
        "Do not run browser commands, discovery, abstract preview, capture, verification, or delegation. Do not inspect the surrounding run folder.",
        "",
        "## Task",
        "",
        "- Score every candidate row in `input-json` exactly once.",
        "- Use the subquestion and query-plan basis below as the scoring lens.",
        "- Put each row into exactly one bucket: `capture_articles`, `maybe_articles`, or `skip_articles`.",
        "- There is no target article count. Capture every supported-publisher row with RCS >= 7 and specific abstract/title evidence.",
        "- Use `maybe_articles` for RCS 5-6 or rows needing better evidence.",
        "- Use `skip_articles` for RCS <= 4, unsupported evidence, empty/invalid abstracts, or off-topic rows.",
        "- Each scored row must include rank, title, href, doi, publisher, abstract_source, status, rcs_0_to_10, and rcs_reasoning.",
        "- `rcs_reasoning` must be row-specific and explain the relation to this subquestion.",
        "",
        "## RCS Rubric",
        "",
        "- 8-10: foundational or seminal evidence for this subquestion.",
        "- 6-7: highly relevant; capture only when abstract evidence is direct and score is at least 7.",
        "- 4-5: partial evidence; keep as maybe at 5 or skip/iterate around 4.",
        "- 2-3: tangential evidence.",
        "- 0-1: off-topic, invalid, or no usable preview evidence.",
        "",
        "## Query Plan Basis",
        "",
        f"- Subquestion ID: {basis.get('subquestion_id', '')}",
        f"- Subquestion: {basis.get('subquestion_text', '')}",
        f"- Query family: {basis.get('query_family', '')}",
        f"- Reading lens: {basis.get('reading_lens', '')}",
        f"- Planning rationale: {basis.get('planning_rationale', '')}",
        f"- Concept anchors: {'; '.join(basis.get('concept_anchors') or [])}",
        f"- Approved queries: {', '.join(basis.get('approved_queries') or [])}",
        "",
        "## Input And Output",
        "",
        f"- input-json: {packet['input_json']}",
        f"- expected_count: {packet['expected_count']}",
        f"- output-json: {output_json}",
        f"- output-md: {output_md}",
        "",
        "Write the JSON first, then a short Markdown explanation listing capture/maybe/skip counts and any uncertainty.",
    ]
    return "\n".join(lines).rstrip() + "\n"


def build_packets(run_dir: Path, abstract_preview: Path | None = None, output_dir: Path | None = None, subquestion_id: str = "") -> dict[str, Any]:
    run_dir = run_dir.resolve()
    abstract_preview = (abstract_preview or run_dir / "abstract-preview" / "abstract-preview.csv").resolve()
    query_plan_path = run_dir / "query-plan-preview.json"
    query_plan = read_json(query_plan_path, {})
    if not isinstance(query_plan, dict):
        raise SystemExit(f"invalid query plan JSON: {query_plan_path}")
    rows = read_csv(abstract_preview)
    plan_by_id = subquestion_map(query_plan)
    grouped: dict[str, list[dict[str, str]]] = {}
    for index, row in enumerate(rows, start=1):
        sqid = clean(row.get("subquestion_id"))
        if subquestion_id and sqid != subquestion_id:
            continue
        if not sqid:
            continue
        grouped.setdefault(sqid, []).append(normalize_row(row, index))
    if not grouped:
        raise SystemExit("no abstract-preview rows matched")
    output_dir = (output_dir or abstract_preview.parent / "abstract-capture-review-packets").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "abstract_preview": str(abstract_preview),
        "query_plan_preview": str(query_plan_path),
        "packets": [],
    }
    for sqid, sq_rows in sorted(grouped.items()):
        sq_rows, duplicate_count = dedupe_rows(sq_rows)
        basis = query_basis(plan_by_id.get(sqid, {"subquestion_id": sqid}))
        base = safe_name(sqid)
        packet_json = output_dir / f"abstract-capture-review-input-{base}.json"
        packet_csv = output_dir / f"abstract-capture-review-input-{base}.csv"
        template_json = output_dir / f"abstract-capture-review-template-{base}.json"
        prompt_md = output_dir / f"abstract-capture-review-worker-prompt-{base}.md"
        output_json = abstract_preview.parent / f"abstract-capture-review-full-{base}.json"
        output_md = abstract_preview.parent / f"abstract-capture-review-full-{base}.md"
        packet = {
            "schema_version": 1,
            "generated_at": manifest["generated_at"],
            "review_phase": "abstract_capture_review",
            "subquestion_id": sqid,
            "expected_count": len(sq_rows),
            "deduped_duplicate_count": duplicate_count,
            "query_plan_basis": basis,
            "rows": sq_rows,
            "input_json": str(packet_json),
            "output_json": str(output_json),
            "output_md": str(output_md),
        }
        packet_json.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
        write_csv(packet_csv, sq_rows, ROW_FIELDS)
        template_json.write_text(
            json.dumps(empty_review_template(sqid, len(sq_rows)), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        prompt_md.write_text(render_prompt(packet, output_json, output_md), encoding="utf-8")
        manifest["packets"].append({
            "subquestion_id": sqid,
            "expected_count": len(sq_rows),
            "deduped_duplicate_count": duplicate_count,
            "input_json": str(packet_json),
            "input_csv": str(packet_csv),
            "template_json": str(template_json),
            "prompt_md": str(prompt_md),
            "output_json": str(output_json),
            "output_md": str(output_md),
        })
    (output_dir / "abstract-capture-review-packets-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--abstract-preview", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--subquestion-id", default="")
    args = parser.parse_args()
    manifest = build_packets(
        args.run_dir,
        abstract_preview=args.abstract_preview,
        output_dir=args.output_dir,
        subquestion_id=args.subquestion_id,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
