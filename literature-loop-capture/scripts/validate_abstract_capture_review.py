#!/usr/bin/env python3
"""Validate subagent abstract-capture review files before capture."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


REQUIRED_ITEM_FIELDS = {
    "rank",
    "title",
    "href",
    "doi",
    "publisher",
    "abstract_source",
    "status",
    "rcs_0_to_10",
    "rcs_reasoning",
}
ARTICLE_BUCKETS = ("capture_articles", "maybe_articles", "skip_articles")


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("review JSON must be an object")
    return data


def numeric_rcs(value: Any) -> float | None:
    try:
        score = float(str(value).strip())
    except Exception:
        return None
    if score < 0 or score > 10:
        return None
    return score


def item_identity(item: dict[str, Any]) -> str:
    doi = clean(item.get("doi")).lower()
    if doi:
        return "doi:" + doi
    href = clean(item.get("href")).lower()
    if href:
        return "href:" + href
    return "title:" + clean(item.get("title")).lower()


def expected_identities(rows: list[dict[str, str]], subquestion_id: str) -> set[str]:
    identities: set[str] = set()
    for row in rows:
        if clean(row.get("subquestion_id")) != subquestion_id:
            continue
        identities.add(item_identity(row))
    return identities


def validate_review(review_path: Path, abstract_preview_csv: Path) -> list[str]:
    review = load_json(review_path)
    rows = read_csv(abstract_preview_csv)
    issues: list[str] = []

    subquestion_id = clean(review.get("subquestion_id"))
    if not subquestion_id:
        issues.append("missing subquestion_id")
        return issues

    expected = expected_identities(rows, subquestion_id)
    if not expected:
        issues.append(f"no abstract-preview rows found for subquestion_id={subquestion_id}")

    if clean(review.get("review_mode")) != "subagent":
        issues.append("review_mode must be subagent for formal abstract-capture review")
    if clean(review.get("review_phase")) != "abstract_capture_review":
        issues.append("review_phase must be abstract_capture_review")
    if not clean(review.get("agent_id")) or clean(review.get("agent_id")) in {"main", "main-agent"}:
        issues.append("agent_id must identify the reviewing subagent")
    if "fallback" in clean(review.get("review_mode")).lower() or clean(review.get("fallback_reason")):
        issues.append("main_agent_fallback is not valid for this subagent review file")

    reviewed_count = review.get("reviewed_count")
    try:
        reviewed_count_int = int(str(reviewed_count))
    except Exception:
        reviewed_count_int = -1
    if reviewed_count_int != len(expected):
        issues.append(f"reviewed_count {reviewed_count!r} does not match expected {len(expected)}")

    seen: set[str] = set()
    reason_counts: Counter[str] = Counter()
    for bucket in ARTICLE_BUCKETS:
        articles = review.get(bucket)
        if not isinstance(articles, list):
            issues.append(f"{bucket} must be a list")
            continue
        for index, item in enumerate(articles, start=1):
            if not isinstance(item, dict):
                issues.append(f"{bucket}[{index}] must be an object")
                continue
            missing = sorted(field for field in REQUIRED_ITEM_FIELDS if field not in item)
            if missing:
                issues.append(f"{bucket}[{index}] missing fields: {', '.join(missing)}")
            identity = item_identity(item)
            if not identity or identity == "title:":
                issues.append(f"{bucket}[{index}] has no stable identity")
            if identity in seen:
                issues.append(f"duplicate reviewed item: {identity}")
            seen.add(identity)
            score = numeric_rcs(item.get("rcs_0_to_10"))
            if score is None:
                issues.append(f"{bucket}[{index}] has invalid rcs_0_to_10")
            elif bucket == "capture_articles" and score < 7:
                issues.append(f"{bucket}[{index}] has capture score below 7")
            reasoning = clean(item.get("rcs_reasoning"))
            if len(reasoning) < 25:
                issues.append(f"{bucket}[{index}] rcs_reasoning is too short")
            reason_counts[reasoning.lower()] += 1

    missing = expected - seen
    extra = seen - expected
    if missing:
        issues.append(f"review does not cover {len(missing)} expected abstract-preview rows")
    if extra:
        issues.append(f"review contains {len(extra)} rows not present in abstract-preview for {subquestion_id}")
    repeated_reasons = [reason for reason, count in reason_counts.items() if reason and count >= 4]
    if repeated_reasons:
        issues.append("rcs_reasoning appears generic/repeated across four or more rows")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("review_json", type=Path)
    parser.add_argument("--abstract-preview", type=Path, required=True)
    args = parser.parse_args()

    issues = validate_review(args.review_json.resolve(), args.abstract_preview.resolve())
    if issues:
        print("INVALID")
        for issue in issues:
            print(f"- {issue}")
        return 1
    print("valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
