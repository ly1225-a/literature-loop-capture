#!/usr/bin/env python3
"""Continue discovery from reviewer-proposed next-simple-queries.csv rows."""

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
    candidates = sorted(
        run_dir.glob("query-refinement/iteration-*/applied-decisions/next-simple-queries.csv"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise SystemExit("No next-simple-queries.csv found. Run apply_query_decisions.py first.")
    return candidates[0]


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
    output_dir = run_dir / "query-refinement" / f"iteration-{args.iteration:02d}"
    path = output_dir / "query-plan-amendment.json"
    if args.use_existing_amendment and path.exists():
        return path
    rounds = query_round_index(run_dir)
    claim = args.claim or question_text(run_dir)
    if not claim:
        raise SystemExit("Missing claim. Pass --claim or keep question.json in the run directory.")
    subquestions: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        next_query = clean(row.get("next_query"))
        subquestion_id = clean(row.get("subquestion_id"))
        if not next_query or not subquestion_id:
            continue
        key = (subquestion_id, next_query.lower())
        if key in seen:
            continue
        seen.add(key)
        round_info = rounds.get(subquestion_id, {})
        source_key = clean(row.get("source_key") or row.get("publisher")).lower()
        source_targets = []
        publisher_targets = []
        if source_key:
            target = {
                "key": source_key,
                "enabled": True,
                "priority": 1,
                "activation_reason": clean(row.get("reason")) or "query iteration from coverage gap",
            }
            if source_key in {"pubmed", "arxiv"}:
                target["kind"] = "bibliographic_index" if source_key == "pubmed" else "preprint_repository"
                source_targets.append(target)
            else:
                target["kind"] = "publisher_platform"
                publisher_targets.append(target)
        subquestions.append({
            "subquestion_id": subquestion_id,
            "subquestion_slug": clean(row.get("subquestion_slug")) or clean(round_info.get("subquestion_slug")) or subquestion_id,
            "subquestion_group_slug": clean(row.get("subquestion_group_slug")) or clean(round_info.get("subquestion_group_slug")) or "general",
            "subquestion_group_title": clean(row.get("subquestion_group_title")) or clean(round_info.get("subquestion_group_title")) or "General",
            "subquestion_text": clean(row.get("subquestion_text")) or clean(round_info.get("claim_subquestion")) or clean(round_info.get("subquestion_text")),
            "query_family": clean(row.get("query_family")) or clean(round_info.get("query_family")) or "agent-iterated",
            "queries": [next_query],
            "source_targets": source_targets,
            "publisher_targets": publisher_targets,
            "publisher_discovery_plan": {
                "search_limit_per_query": args.max_results_per_page,
                "scrape_shortlist_limit": 5,
                "use_research_category": True,
                "use_research_index": False,
                "use_publisher_advanced_pages": True,
            },
        })
    if not subquestions:
        raise SystemExit("No valid next queries to run.")
    output_dir.mkdir(parents=True, exist_ok=True)
    base_plan = load_json(run_dir / "query-plan-preview.json", {})
    plan = {
        "english_big_question": claim,
        "grounding_notes": clean(base_plan.get("grounding_notes")) or "OpenAlex delta grounding from reading-note gaps.",
        "exploration_sources": base_plan.get("exploration_sources") or [
            {"label": "OpenAlex", "url": "https://openalex.org", "note": "metadata grounding"},
        ],
        "openalex_grounding": base_plan.get("openalex_grounding") or {"api_key_present": True, "status": "ok", "terms": ["delta grounding"]},
        "requires_user_approval": True,
        "approval_status": "iteration_amendment",
        "discovery_backend": args.discovery_backend,
        "subquestions": subquestions,
    }
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


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
    parser.add_argument("--next-queries", type=Path)
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
    parser.add_argument("--use-existing-amendment", action="store_true", help="Use an existing query-plan-amendment.json instead of rebuilding it.")
    parser.add_argument("--approved-query-plan", type=Path, help="Use this exact reviewed query-plan amendment for continued discovery.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    if args.approved_query_plan:
        plan_path = args.approved_query_plan.resolve()
        if not plan_path.exists():
            raise SystemExit(f"approved query plan not found: {plan_path}")
    else:
        next_queries_path = (args.next_queries or latest_next_queries(run_dir)).resolve()
        rows = read_csv(next_queries_path)
        plan_path = build_plan_amendment(run_dir, rows, args)
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
