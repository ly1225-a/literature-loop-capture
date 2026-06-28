#!/usr/bin/env python3
"""Continue discovery from a user-approved query-plan amendment."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent


def clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def latest_next_queries(run_dir: Path) -> Path:
    raise SystemExit(
        "Direct next-simple-queries.csv iteration is disabled. "
        "Use a user-approved query-plan-amendment.json from query_iteration_review.py."
    )


def question_text(run_dir: Path) -> str:
    question = load_json(run_dir / "question.json", {})
    if isinstance(question, dict):
        return clean(question.get("question"))
    return ""


def query_round_index(run_dir: Path) -> dict[str, dict[str, Any]]:
    rows = load_json(run_dir / "query-rounds.json", [])
    if not isinstance(rows, list):
        return {}
    return {
        clean(row.get("subquestion_id")): row
        for row in rows
        if isinstance(row, dict) and clean(row.get("subquestion_id"))
    }


def build_plan_amendment(run_dir: Path, rows: list[dict[str, str]], args: argparse.Namespace) -> Path:
    raise SystemExit(
        "Python amendment construction from next-simple-queries.csv is disabled. "
        "The responsible agent must write query-rationale-review.json, run query_iteration_review.py, "
        "and pass the reviewed query-plan-amendment.json with --approved-query-plan."
    )


def build_command(run_dir: Path, plan_path: Path, args: argparse.Namespace) -> list[str]:
    claim = args.claim or question_text(run_dir)
    command = [
        sys.executable,
        str(SCRIPT_DIR / "incremental_capture.py"),
        "--claim", claim,
        "--existing-run-dir", str(run_dir),
        "--approved-query-plan", str(plan_path),
        "--rounds", "1",
        "--max-queries", "1",
        "--max-pages", str(args.max_pages),
        "--max-results-per-page", str(args.max_results_per_page),
        "--year-start", str(args.year_start),
        "--year-end", str(args.year_end),
        "--discovery-only",
        "--discovery-backend", args.discovery_backend,
        "--no-query-refinement-packets",
    ]
    if args.opencli_session:
        command.extend(["--opencli-session", args.opencli_session])
    command.extend([
        "--manual-blocker-wait-ms", str(args.manual_blocker_wait_ms),
        "--abstract-expand-wait-ms", str(args.abstract_expand_wait_ms),
    ])
    return command


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--claim", default="")
    parser.add_argument("--iteration", type=int, default=2)
    parser.add_argument("--total-iterations", type=int, default=3)
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--max-results-per-page", type=int, default=20)
    parser.add_argument("--query-refinement-top-candidates", type=int, default=12)
    parser.add_argument("--query-refinement-picks-per-query", type=int, default=3)
    parser.add_argument("--year-start", type=int, required=True)
    parser.add_argument("--year-end", type=int, required=True)
    parser.add_argument("--discovery-backend", choices=["opencli"], default="opencli")
    parser.add_argument("--opencli-session", default="lit")
    parser.add_argument("--manual-blocker-wait-ms", type=int, default=12000)
    parser.add_argument("--abstract-expand-wait-ms", type=int, default=700)
    parser.add_argument("--approved-query-plan", type=Path, help="Use this exact reviewed query-plan amendment for continued discovery.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    if not args.approved_query_plan:
        raise SystemExit(
            "continued query iteration requires --approved-query-plan pointing to a user-reviewed "
            "query-plan-amendment.json from query_iteration_review.py. Direct next-simple-queries.csv "
            "amendment construction is disabled in the public skill."
        )
    plan_path = args.approved_query_plan.resolve()
    if not plan_path.exists():
        raise SystemExit(f"approved query plan not found: {plan_path}")
    command = build_command(run_dir, plan_path, args)
    output_dir = plan_path.parent
    (output_dir / "continued-discovery-commands.json").write_text(
        json.dumps([command], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if args.dry_run:
        print("planned_commands=1")
        print(f"query_plan_amendment={plan_path}")
        return 0
    completed = subprocess.run(command, cwd=run_dir.parent)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)
    print("continued_queries=1")
    print(f"query_plan_amendment={plan_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
