#!/usr/bin/env python3
"""Validate a publisher-authenticated literature capture output directory."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any


REQUIRED_CAPTURE_FILES = [
    "metadata.json",
    "fulltext.json",
    "fulltext.md",
    "captured-fulltext.md",
    "structure.json",
    "figures/manifest.json",
    "figures/index.md",
    "tables/manifest.json",
    "tables/index.md",
]

REQUIRED_MINERU_FILES = [
    "metadata.json",
    "source.pdf",
    "mineru/fulltext.md",
    "fulltext.json",
    "fulltext.md",
    "captured-fulltext.md",
    "structure.json",
    "figures/manifest.json",
    "figures/index.md",
    "tables/manifest.json",
    "tables/index.md",
]


def read_summary(run_dir: Path) -> list[dict[str, Any]]:
    json_path = run_dir / "run-summary.json"
    if json_path.exists():
        data = json.loads(json_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    csv_path = run_dir / "run-summary.csv"
    if csv_path.exists():
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    return []


def validate_run(run_dir: Path) -> tuple[list[dict[str, Any]], int]:
    issues: list[dict[str, Any]] = []
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import literature_loop  # type: ignore

        for issue in literature_loop.agent_gate_issues(run_dir):
            issues.append({"level": "error", "path": str(run_dir), "issue": issue})
    except Exception as exc:
        issues.append({"level": "warning", "path": str(run_dir), "issue": f"agent_gate_check_unavailable:{type(exc).__name__}"})
    rows = read_summary(run_dir)
    if not rows:
        issues.append({"level": "error", "path": str(run_dir), "issue": "missing_or_empty_run_summary"})
        return issues, 1

    subquestions_root = run_dir / "subquestions"
    if not subquestions_root.exists():
        issues.append({"level": "error", "path": str(subquestions_root), "issue": "missing_subquestions_root"})
    else:
        subquestion_dirs = [p.parent for p in subquestions_root.rglob("subquestion.json")]
        if not subquestion_dirs:
            issues.append({"level": "error", "path": str(subquestions_root), "issue": "empty_subquestions_root"})
        for folder in subquestion_dirs:
            for rel in ["subquestion.json", "agent-brief.md", "reading-notes-index.csv"]:
                path = folder / rel
                if not path.exists():
                    issues.append({"level": "error", "path": str(path), "issue": "missing_subquestion_task_file"})
            if (folder / "reference-candidates.csv").exists() or (folder / "reference-candidates.json").exists():
                for rel in ["final-reference-selection.csv", "final-reference-selection.json", "reference-provenance.csv", "reference-provenance.json"]:
                    path = folder / rel
                    if not path.exists():
                        issues.append({"level": "error", "path": str(path), "issue": "missing_reference_followup_stage_file"})

    for row in rows:
        if row.get("status") != "captured":
            continue
        if not row.get("subquestion_id"):
            issues.append({"level": "warning", "path": str(run_dir), "issue": "captured_row_missing_subquestion_id", "title": row.get("title")})
        capture_depth = str(row.get("capture_depth") or "1")
        source_role = str(row.get("source_role") or ("reference" if capture_depth == "2" else "primary"))
        if source_role not in {"primary", "reference"}:
            issues.append({"level": "error", "path": str(run_dir), "issue": "invalid_source_role", "title": row.get("title"), "source_role": source_role})
        if capture_depth == "2" or source_role == "reference":
            if not row.get("parent_article_dir") or not row.get("parent_reference_index"):
                issues.append({"level": "error", "path": str(run_dir), "issue": "second_level_capture_missing_parent_reference", "title": row.get("title")})
        article_dir = Path(str(row.get("article_dir") or ""))
        if not article_dir.exists():
            issues.append({"level": "error", "path": str(article_dir), "issue": "missing_article_dir", "title": row.get("title")})
            continue
        if subquestions_root.exists():
            try:
                article_dir.resolve().relative_to(subquestions_root.resolve())
            except ValueError:
                issues.append({"level": "error", "path": str(article_dir), "issue": "article_dir_outside_subquestions", "title": row.get("title")})
            normalized = str(article_dir).replace("\\", "/")
            if source_role == "reference" and "/references/" not in normalized:
                issues.append({"level": "error", "path": str(article_dir), "issue": "reference_article_not_under_references", "title": row.get("title")})
            if source_role == "primary" and "/sources/" not in normalized:
                issues.append({"level": "error", "path": str(article_dir), "issue": "primary_article_not_under_sources", "title": row.get("title")})
        is_mineru_pdf = row.get("article_type") == "pdf-mineru"
        required_files = REQUIRED_MINERU_FILES if is_mineru_pdf else REQUIRED_CAPTURE_FILES
        for rel in required_files:
            path = article_dir / rel
            if not path.exists():
                issues.append({"level": "error", "path": str(path), "issue": "missing_required_file", "title": row.get("title")})
        if not (article_dir / "reading-note-zh.md").exists() and not (article_dir / "NOTE_REQUIRES_AGENT.md").exists():
            issues.append({"level": "warning", "path": str(article_dir), "issue": "missing_note_or_agent_placeholder", "title": row.get("title")})
        try:
            fulltext_path = article_dir / "mineru" / "fulltext.md" if is_mineru_pdf else article_dir / "captured-fulltext.md"
            fulltext = fulltext_path.read_text(encoding="utf-8")
            if len(fulltext) < 2500:
                issues.append({"level": "warning", "path": str(fulltext_path), "issue": "short_fulltext", "chars": len(fulltext)})
        except Exception as exc:
            issues.append({"level": "error", "path": str(fulltext_path), "issue": f"cannot_read_fulltext:{type(exc).__name__}"})

    status = 1 if any(issue.get("level") == "error" for issue in issues) else 0
    return issues, status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args()
    issues, status = validate_run(args.run_dir.resolve())
    if args.json:
        print(json.dumps({"status": "ok" if status == 0 else "error", "issues": issues}, ensure_ascii=False, indent=2))
    else:
        if not issues:
            print("ok")
        else:
            for issue in issues:
                print(f"{issue.get('level')}: {issue.get('issue')} - {issue.get('path')}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
