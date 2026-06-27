#!/usr/bin/env python3
"""Validate that a subquestion worker produced real reading-note artifacts."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


REQUIRED_MARKERS = [
    "five cs",
    "图表检查",
    "figure table check",
    "worth_close_reading:",
    "worth_close_reading_score_0_to_5:",
    "对 subquestion coverage 的影响",
    "coverage impact",
    "high-value seed ledger",
    "reference pick",
    "selected reference",
    "gap list",
    "proposed next query",
]


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def article_dirs(subquestion_dir: Path, include_references: bool = False) -> list[Path]:
    roots = [subquestion_dir / "sources"]
    if include_references:
        roots.append(subquestion_dir / "references")
    out: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.glob("*/articles/*")):
            if path.is_dir() and (path / "metadata.json").exists():
                out.append(path)
    return out


def missing_markers(text: str) -> list[str]:
    lower = text.lower()
    return [marker for marker in REQUIRED_MARKERS if marker.lower() not in lower]


def worth_close_reading(text: str) -> bool:
    lower = text.lower()
    if re.search(r"worth_close_reading:\s*(true|yes|是|值得)", lower):
        return True
    score_match = re.search(r"worth_close_reading_score_0_to_5:\s*([0-5](?:\.\d+)?)", lower)
    if score_match:
        try:
            return float(score_match.group(1)) >= 4
        except ValueError:
            return False
    return False


def has_recommended_references(article_dir: Path) -> bool:
    for name in ["recommended-references.json", "recommended-references.csv", "recommended-references.md"]:
        path = article_dir / name
        if path.exists() and clean(read_text(path)):
            return True
    return False


def response_valid(path: Path) -> bool:
    text = read_text(path)
    if not clean(text):
        return False
    lower = text.lower()
    return "review_mode:" in lower and ("subagent" in lower or "main_agent_fallback" in lower) and "reviewed" in lower


def summary_valid(path: Path) -> bool:
    text = read_text(path)
    lower = text.lower()
    return bool(clean(text)) and "requires agent" not in lower and "codex/subagent should write" not in lower


def validate_subquestion(
    subquestion_dir: Path,
    *,
    include_references: bool = False,
    require_response: bool = False,
    require_summary: bool = False,
) -> dict[str, Any]:
    subquestion_dir = subquestion_dir.resolve()
    issues: list[dict[str, Any]] = []
    articles = article_dirs(subquestion_dir, include_references=include_references)
    if not articles:
        issues.append({"issue": "no_article_dirs", "path": str(subquestion_dir)})

    ready_count = 0
    close_read_count = 0
    recommended_reference_count = 0
    for article_dir in articles:
        note_path = article_dir / "reading-note-zh.md"
        if not note_path.exists():
            issues.append({"issue": "missing_reading_note", "path": str(note_path)})
            continue
        text = read_text(note_path)
        missing = missing_markers(text)
        if missing:
            issues.append({"issue": "missing_required_markers", "path": str(note_path), "missing": missing})
            continue
        ready_count += 1
        if worth_close_reading(text):
            close_read_count += 1
            if has_recommended_references(article_dir):
                recommended_reference_count += 1
            else:
                issues.append({"issue": "missing_recommended_references_for_close_read", "path": str(article_dir)})

    if require_response and not response_valid(subquestion_dir / "subagent-response.md"):
        issues.append({"issue": "invalid_or_empty_subagent_response", "path": str(subquestion_dir / "subagent-response.md")})
    if require_summary and not summary_valid(subquestion_dir / "subquestion-summary-zh.md"):
        issues.append({"issue": "invalid_or_placeholder_subquestion_summary", "path": str(subquestion_dir / "subquestion-summary-zh.md")})

    return {
        "status": "ok" if not issues else "error",
        "subquestion_dir": str(subquestion_dir),
        "article_count": len(articles),
        "ready_note_count": ready_count,
        "close_reading_article_count": close_read_count,
        "recommended_reference_article_count": recommended_reference_count,
        "issues": issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("subquestion_dir", type=Path)
    parser.add_argument("--include-references", action="store_true")
    parser.add_argument("--require-response", action="store_true")
    parser.add_argument("--require-summary", action="store_true")
    args = parser.parse_args()
    result = validate_subquestion(
        args.subquestion_dir,
        include_references=args.include_references,
        require_response=args.require_response,
        require_summary=args.require_summary,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
