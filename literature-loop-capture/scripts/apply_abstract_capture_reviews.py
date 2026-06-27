#!/usr/bin/env python3
"""Create capture queues from validated full abstract-capture reviews."""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import validate_abstract_capture_review


STRUCTURED_PUBLISHERS = {"elsevier", "sciencedirect", "acs", "wiley", "springer"}
COMMON_FIELDS = [
    "group_id",
    "subquestion_id",
    "subquestion_slug",
    "subquestion_group_slug",
    "subquestion_group_title",
    "subquestion_text",
    "query_family",
    "publisher",
    "query_text",
    "decision",
    "rcs_0_to_10",
    "rcs_flag",
    "rcs_reasoning",
    "abstract_preview_status",
    "rank",
    "title",
    "href",
    "landing_url",
    "doi",
    "context",
    "page_url",
    "abstract",
    "abstract_source",
    "pdf_url",
    "source_key",
    "publisher_key",
    "agent_score",
    "agent_reason",
    "source_bucket",
]


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def safe_file_stem(value: str, fallback: str = "subquestion") -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", clean(value)).strip("._-")
    return stem or fallback


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


def item_identity(item: dict[str, Any]) -> str:
    return validate_abstract_capture_review.item_identity(item)


def preview_lookup(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        out[item_identity(row)] = row
    return out


def plan_lookup(run_dir: Path) -> dict[str, dict[str, Any]]:
    plan = read_json(run_dir / "query-plan-preview.json", {})
    subquestions = plan.get("subquestions") if isinstance(plan, dict) and isinstance(plan.get("subquestions"), list) else []
    out: dict[str, dict[str, Any]] = {}
    for item in subquestions:
        if not isinstance(item, dict):
            continue
        subquestion_id = clean(item.get("subquestion_id"))
        if subquestion_id:
            out[subquestion_id] = item
    return out


def review_paths(run_dir: Path, preview_rows: list[dict[str, str]]) -> list[Path]:
    ids = sorted({clean(row.get("subquestion_id")) for row in preview_rows if clean(row.get("subquestion_id"))})
    return [run_dir / "abstract-preview" / f"abstract-capture-review-full-{safe_file_stem(subquestion_id)}.json" for subquestion_id in ids]


def validate_reviews(paths: list[Path], preview_csv: Path) -> None:
    issues: list[str] = []
    for path in paths:
        if not path.exists():
            issues.append(f"missing review: {path}")
            continue
        for issue in validate_abstract_capture_review.validate_review(path, preview_csv):
            issues.append(f"{path}: {issue}")
    if issues:
        raise SystemExit("Invalid abstract-capture reviews:\n- " + "\n- ".join(issues))


def capture_rows_from_review(review_path: Path, lookup: dict[str, dict[str, str]], plans: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    review = read_json(review_path, {})
    subquestion_id = clean(review.get("subquestion_id"))
    plan = plans.get(subquestion_id, {})
    out: list[dict[str, Any]] = []
    for item in review.get("capture_articles") or []:
        if not isinstance(item, dict):
            continue
        publisher = clean(item.get("publisher")).lower()
        if publisher not in STRUCTURED_PUBLISHERS:
            continue
        candidate = lookup.get(item_identity(item), {})
        href = clean(item.get("href") or candidate.get("href") or candidate.get("landing_url"))
        title = clean(item.get("title") or candidate.get("title"))
        if not href or not title:
            continue
        out.append({
            "group_id": f"{subquestion_id}_{publisher}_abstract_capture",
            "subquestion_id": subquestion_id,
            "subquestion_slug": clean(plan.get("subquestion_slug")),
            "subquestion_group_slug": clean(plan.get("subquestion_group_slug")),
            "subquestion_group_title": clean(plan.get("subquestion_group_title")),
            "subquestion_text": clean(plan.get("subquestion_text") or candidate.get("subquestion_text")),
            "query_family": clean(plan.get("query_family") or candidate.get("query_family")),
            "publisher": publisher,
            "query_text": clean(candidate.get("query_text") or item.get("query_text")),
            "decision": "ready_to_capture",
            "rcs_0_to_10": item.get("rcs_0_to_10", ""),
            "rcs_flag": clean(item.get("rcs_flag")),
            "rcs_reasoning": clean(item.get("rcs_reasoning")),
            "abstract_preview_status": clean(item.get("status") or candidate.get("status")),
            "rank": clean(item.get("rank") or candidate.get("rank")),
            "title": title,
            "href": href,
            "landing_url": clean(item.get("landing_url") or candidate.get("landing_url") or href),
            "doi": clean(item.get("doi") or candidate.get("doi")),
            "context": clean(item.get("context") or candidate.get("context")),
            "page_url": clean(item.get("page_url") or candidate.get("page_url")),
            "abstract": clean(item.get("abstract") or candidate.get("abstract")),
            "abstract_source": clean(item.get("abstract_source") or candidate.get("abstract_source")),
            "pdf_url": clean(item.get("pdf_url") or candidate.get("pdf_url")),
            "source_key": clean(item.get("source_key") or candidate.get("source_key")),
            "publisher_key": clean(item.get("publisher_key") or candidate.get("publisher_key") or publisher),
            "agent_score": item.get("rcs_0_to_10", ""),
            "agent_reason": clean(item.get("rcs_reasoning")),
            "source_bucket": "capture_articles",
        })
    return out


def apply_reviews(run_dir: Path, output_dir: Path | None = None) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    preview_csv = run_dir / "abstract-preview" / "abstract-preview.csv"
    if not preview_csv.exists():
        raise SystemExit(f"missing abstract preview CSV: {preview_csv}")
    preview_rows = read_csv(preview_csv)
    paths = review_paths(run_dir, preview_rows)
    validate_reviews(paths, preview_csv)
    lookup = preview_lookup(preview_rows)
    plans = plan_lookup(run_dir)
    capture_queue: list[dict[str, Any]] = []
    for path in paths:
        capture_queue.extend(capture_rows_from_review(path, lookup, plans))
    output_dir = (output_dir or run_dir / "query-refinement" / "iteration-01" / "applied-decisions").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "capture-queue.csv", capture_queue, COMMON_FIELDS)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "abstract_capture_reviews": [str(path) for path in paths],
        "capture_queue": capture_queue,
    }
    (output_dir / "abstract-capture-review-status.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Abstract Capture Review Decisions",
        "",
        f"- Reviews validated: {len(paths)}",
        f"- Capture candidates: {len(capture_queue)}",
        "",
    ]
    for row in capture_queue:
        lines.append(f"- {row['publisher']} RCS={row['rcs_0_to_10']}: {row['title']} - {row['rcs_reasoning']}")
    (output_dir / "abstract-capture-review-status.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return {"output_dir": str(output_dir), "capture_queue": len(capture_queue), "reviews": len(paths)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    result = apply_reviews(args.run_dir, output_dir=args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
