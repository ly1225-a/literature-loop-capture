#!/usr/bin/env python3
"""L2-assisted orchestrator for the publisher-authenticated literature capture workflow."""

from __future__ import annotations

import argparse
import csv
import html
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import subquestion_coverage_review
import validate_abstract_capture_review


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = SKILL_DIR.parent

DEFAULT_CLAIM = "What is the research landscape for the target topic?"
DEFAULT_REVIEW_OBJECTIVE = "construction-method route"
DEFAULT_YEAR_START = 2021
DEFAULT_YEAR_END = 2026
DEFAULT_SUBQUESTION_COUNT = 3
DEFAULT_MAX_QUERIES_PER_SUBQUESTION = 3
DEFAULT_ATTEMPT_CAP = 2
DEFAULT_ITERATION_BUDGET = 1
DEFAULT_REFERENCE_LIMIT_PER_SUBQUESTION = 5
DEFAULT_OPENCLI_DISCOVERY_SESSION = "lit"
DEFAULT_OPENCLI_PREVIEW_SESSION = "lit-preview"
DEFAULT_OPENCLI_CAPTURE_SESSION = "lit-capture"

STRUCTURED_PUBLISHERS = {"elsevier", "sciencedirect", "acs", "wiley", "springer"}
DEFAULT_CAPTURE_PUBLISHERS = {"elsevier", "acs", "wiley", "springer"}
PUBLISHER_ALIASES = {
    "elsevier": "elsevier",
    "science direct": "elsevier",
    "sciencedirect": "elsevier",
    "acs": "acs",
    "american chemical society": "acs",
    "wiley": "wiley",
    "springer": "springer",
    "springer nature": "springer",
    "nature": "springer",
    "ieee": "ieee",
}
TERMINAL_COVERAGE_DECISIONS = {"stop_with_gaps", "blocked"}
ITERATION_REVIEW_COVERAGE_STAGE_STATUSES = {"needs_iteration_review", "primary_pass_sufficient"}
COVERAGE_THRESHOLD = 4

STAGE_ORDER = [
    "doctor",
    "query_plan",
    "query_plan_approval",
    "publisher_discovery",
    "query_refinement_review",
    "abstract_preview",
    "apply_query_decisions",
    "publisher_capture",
    "reading_notes",
    "coverage_review",
    "coverage_gate",
    "query_iteration_plan",
    "query_iteration_plan_approval",
    "query_iteration",
    "reference_followup",
    "overview",
    "verify",
]

HUMAN_GATE_STAGES = {
    "query_plan_approval": "write_validate_build_and_approve_agent_query_plan",
    "query_iteration_plan_approval": "review_and_approve_query_iteration_amendment",
    "query_refinement_review": "complete search-page triage, then build clean abstract-capture packets and dispatch stateless scoring workers",
    "reading_notes": "write_or_review_reading_note_zh",
    "overview": "write_final_overview_md",
}


@dataclass
class LoopConfig:
    run_dir: Path
    claim: str = DEFAULT_CLAIM
    original_request: str = ""
    review_objective: str = DEFAULT_REVIEW_OBJECTIVE
    year_start: int = DEFAULT_YEAR_START
    year_end: int = DEFAULT_YEAR_END
    subquestion_count: int = DEFAULT_SUBQUESTION_COUNT
    max_queries_per_subquestion: int = DEFAULT_MAX_QUERIES_PER_SUBQUESTION
    structured_publishers: set[str] | None = None
    unsupported_publishers: set[str] | None = None
    attempt_cap: int = DEFAULT_ATTEMPT_CAP
    iteration_budget: int = DEFAULT_ITERATION_BUDGET
    reference_limit_per_subquestion: int = DEFAULT_REFERENCE_LIMIT_PER_SUBQUESTION
    opencli_discovery_session: str = DEFAULT_OPENCLI_DISCOVERY_SESSION
    opencli_preview_session: str = DEFAULT_OPENCLI_PREVIEW_SESSION
    opencli_capture_session: str = DEFAULT_OPENCLI_CAPTURE_SESSION


Runner = Callable[[list[str], Path], int]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def numeric(value: Any) -> float | None:
    text = clean(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def safe_slug(value: str, fallback: str = "literature-loop", limit: int = 48) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.lower()).strip("-._")
    return (slug[:limit].strip("-._") or fallback)


def safe_file_stem(value: str, fallback: str = "subquestion") -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", clean(value)).strip("._-")
    return stem or fallback


def canonical_publisher(value: str) -> str:
    key = clean(value).lower()
    return PUBLISHER_ALIASES.get(key, key)


def extract_publishers(request: str, explicit: str = "") -> tuple[set[str], set[str]]:
    haystack = f"{request}, {explicit}"
    structured: set[str] = set()
    unsupported: set[str] = set()
    for alias, canonical in PUBLISHER_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", haystack, flags=re.IGNORECASE):
            if canonical in DEFAULT_CAPTURE_PUBLISHERS:
                structured.add(canonical)
            else:
                unsupported.add(canonical)
    for token in re.split(r"[,;/|]+", explicit or ""):
        canonical = canonical_publisher(token)
        if not canonical:
            continue
        if canonical in DEFAULT_CAPTURE_PUBLISHERS:
            structured.add(canonical)
        else:
            unsupported.add(canonical)
    return structured or set(DEFAULT_CAPTURE_PUBLISHERS), unsupported


def normalize_claim_from_request(request: str, explicit_claim: str = "") -> str:
    explicit_claim = clean(explicit_claim)
    if explicit_claim:
        return explicit_claim
    text = clean(request)
    text = strip_year_filters(text)
    text = re.sub(r"\bpublishers?\b\s*[:：]?\s*[^.。;；]+", " ", text, flags=re.IGNORECASE)
    text = clean(text.strip(" ,;；.。"))
    text = re.sub(r"^(please\s+)?(review|study|research|investigate|analyze|analyse)\s+", "", text, flags=re.IGNORECASE)
    text = clean(text.strip(" ,;；.。\"'“”"))
    if not text:
        return DEFAULT_CLAIM
    text = text[0].upper() + text[1:]
    if not text.endswith("?"):
        text = text.rstrip(".") + "?"
    return text


YEAR_RANGE_PATTERN = re.compile(
    r"\b(?:publication\s+)?years?\s*[:：]?\s*(?:from\s+)?(20\d{2})\s*(?:[-–—]|to|through|until|and)\s*(20\d{2})\b"
    r"|\b(?:from|between)\s+(20\d{2})\s*(?:[-–—]|to|through|until|and)\s*(20\d{2})\b"
    r"|\b(20\d{2})\s*[-–—]\s*(20\d{2})\b",
    flags=re.IGNORECASE,
)


def extract_year_window_from_request(request: str) -> tuple[int, int] | None:
    match = YEAR_RANGE_PATTERN.search(clean(request))
    if not match:
        return None
    groups = [value for value in match.groups() if value]
    if len(groups) < 2:
        return None
    start, end = int(groups[0]), int(groups[1])
    if start > end:
        start, end = end, start
    return start, end


def strip_year_filters(text: str) -> str:
    text = YEAR_RANGE_PATTERN.sub(" ", clean(text))
    text = re.sub(r"\b(?:publication\s+)?years?\b\s*[:：]?", " ", text, flags=re.IGNORECASE)
    return clean(text)


def default_run_dir(root: Path = PROJECT_ROOT, claim: str = DEFAULT_CLAIM) -> Path:
    return root / "LiteratureCaptures" / f"{safe_slug(claim, 'literature-review')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def state_path(run_dir: Path) -> Path:
    return run_dir / "loop-state.json"


def run_log_path(run_dir: Path) -> Path:
    return run_dir / "loop-run-log.jsonl"


def state_md_path(run_dir: Path) -> Path:
    return run_dir / "STATE.md"


def query_plan_review_path(run_dir: Path) -> Path:
    return run_dir / "query-plan-review.json"


def query_plan_review_html_path(run_dir: Path) -> Path:
    return run_dir / "query-plan-review.html"


def status_for_path(path: Path, kind: str = "file") -> dict[str, Any]:
    if path.exists() and ((kind == "dir" and path.is_dir()) or (kind != "dir" and path.is_file())):
        return {"status": "ok", "path": str(path)}
    return {"status": "missing", "path": str(path)}


def import_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def doctor_report(
    *,
    project_root: Path = PROJECT_ROOT,
    env: dict[str, str] | None = None,
    import_checker: Callable[[str], bool] = import_available,
) -> dict[str, Any]:
    env = env if env is not None else dict(os.environ)
    required_packages: list[str] = []
    optional_packages = ["bs4", "yaml"]
    environment: dict[str, dict[str, Any]] = {
        "OPENALEX_API_KEY": {
            "status": "set" if env.get("OPENALEX_API_KEY") else "missing",
            "required": True,
            "value": "<redacted>" if env.get("OPENALEX_API_KEY") else "",
        }
    }
    opencli_status = "ok"
    opencli_error = ""
    if not shutil.which("opencli"):
        opencli_status = "missing"
        opencli_error = "opencli executable not found on PATH"
    else:
        completed = subprocess.run(
            ["opencli", "doctor"],
            cwd=project_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            opencli_status = "failed"
            opencli_error = (completed.stderr or completed.stdout or "").strip()[:500]
    output_root = project_root / "LiteratureCaptures"
    writable = True
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        probe = output_root / ".write-probe"
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink()
    except OSError:
        writable = False
    return {
        "generated_at": utc_now(),
        "project_root": str(project_root),
        "python": {
            "executable": sys.executable,
            "version": sys.version.split()[0],
            "required_packages": {name: "ok" if import_checker(name) else "missing" for name in required_packages},
            "optional_packages": {name: "ok" if import_checker(name) else "missing" for name in optional_packages},
        },
        "environment": environment,
        "browser": {
            "backend": "opencli",
            "opencli": {"status": opencli_status, "error": opencli_error},
            "opencli_profile": env.get("OPENCLI_PROFILE", "") or "default",
            "authentication": "Log in to publisher-authenticated through the Chrome/OpenCLI profile before discovery or capture.",
        },
        "output_root": {"status": "ok" if writable else "not_writable", "path": str(output_root)},
        "secrets_policy": "presence-only; values are never written",
    }


def stage_template(name: str, status: str = "pending") -> dict[str, Any]:
    return {
        "status": status,
        "attempts": 0,
        "updated_at": utc_now(),
        "required_action": HUMAN_GATE_STAGES.get(name, ""),
        "artifacts": [],
    }


def create_initial_state(config: LoopConfig, *, query_plan_ready: bool = True) -> dict[str, Any]:
    run_dir = config.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    stages = {name: stage_template(name) for name in STAGE_ORDER}
    stages["doctor"]["status"] = "complete"
    if query_plan_ready:
        stages["query_plan"]["status"] = "complete"
        stages["query_plan_approval"]["status"] = "human_gate"
    state = {
        "schema_version": 1,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "run_dir": str(run_dir),
        "level": "L2-assisted",
        "kill_switch": False,
        "config": {
            "original_request": config.original_request,
            "claim": config.claim,
            "review_objective": config.review_objective,
            "year_start": config.year_start,
            "year_end": config.year_end,
            "subquestion_count": config.subquestion_count,
            "max_queries_per_subquestion": config.max_queries_per_subquestion,
            "structured_publishers": sorted(config.structured_publishers or DEFAULT_CAPTURE_PUBLISHERS),
            "unsupported_publishers": sorted(config.unsupported_publishers or set()),
            "attempt_cap": config.attempt_cap,
            "iteration_budget": config.iteration_budget,
            "reference_limit_per_subquestion": config.reference_limit_per_subquestion,
            "opencli_discovery_session": config.opencli_discovery_session,
            "opencli_preview_session": config.opencli_preview_session,
            "opencli_capture_session": config.opencli_capture_session,
        },
        "stages": stages,
        "subquestions": [],
        "blockers": [],
        "human_gates": [
            {
                "stage": "query_plan_approval",
                "status": "waiting",
                "instruction": (
                    "Read OpenAlex grounding, author agent-query-plan.json, run "
                    "validate_agent_query_plan.py and build_publisher_urls.py, then approve query-plan-preview.md/json."
                ),
            }
        ],
        "budget": {
            "max_stage_attempts": config.attempt_cap,
            "max_query_iterations_per_subquestion": config.iteration_budget,
            "max_reference_captures_per_subquestion": config.reference_limit_per_subquestion,
        },
    }
    save_state(run_dir, state)
    append_run_log(run_dir, "state_initialized", "success", {"stage": "doctor"})
    return state


def load_state(run_dir: Path) -> dict[str, Any]:
    path = state_path(run_dir)
    if not path.exists():
        raise SystemExit(f"Missing loop state: {path}")
    return migrate_state(json.loads(path.read_text(encoding="utf-8")))


def migrate_state(state: dict[str, Any]) -> dict[str, Any]:
    stages = state.get("stages")
    config = state.get("config")
    if isinstance(config, dict):
        config.setdefault("original_request", "")
        config.setdefault("structured_publishers", sorted(DEFAULT_CAPTURE_PUBLISHERS))
        config.setdefault("unsupported_publishers", [])
    return state


def save_state(run_dir: Path, state: dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = utc_now()
    state_path(run_dir).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    write_state_md(run_dir, state)
    if not run_log_path(run_dir).exists():
        run_log_path(run_dir).write_text("", encoding="utf-8")


def append_run_log(run_dir: Path, event: str, outcome: str, extra: dict[str, Any] | None = None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": utc_now(),
        "event": event,
        "outcome": outcome,
        "tokens_estimate": 0,
    }
    if extra:
        payload.update(extra)
    with run_log_path(run_dir).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def write_state_md(run_dir: Path, state: dict[str, Any]) -> None:
    next_item = next_action(state)
    blockers = state.get("blockers") or []
    lines = [
        "# Loop State",
        "",
        f"- Updated: {state.get('updated_at', '')}",
        f"- Level: {state.get('level', '')}",
        f"- Claim: {state.get('config', {}).get('claim', '')}",
        f"- Next stage: `{next_item.get('stage', '')}`",
        f"- Next status: `{next_item.get('status', '')}`",
        f"- Required action: {next_item.get('required_action', '') or 'none'}",
        "",
        "## High Priority",
        "",
    ]
    if blockers:
        for blocker in blockers[-10:]:
            lines.append(f"- {blocker.get('stage', '')}: {blocker.get('reason', '')}")
    else:
        lines.append("- No active blockers.")
    lines.extend(["", "## Stage Status", ""])
    for name in STAGE_ORDER:
        stage = state.get("stages", {}).get(name, {})
        lines.append(f"- `{name}`: {stage.get('status', 'missing')} (attempts={stage.get('attempts', 0)})")
    lines.extend(["", f"Run log: `{run_log_path(run_dir).name}`"])
    state_md_path(run_dir).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def next_action(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("kill_switch"):
        return {"stage": "paused", "status": "paused", "required_action": "clear_kill_switch"}
    stages = state.get("stages", {})
    for name in STAGE_ORDER:
        stage = stages.get(name, {})
        status = stage.get("status", "pending")
        if status not in {"complete", "skipped"}:
            return {"stage": name, "status": status, "required_action": stage.get("required_action", "")}
    return {"stage": "complete", "status": "complete", "required_action": ""}


def coverage_is_terminal(item: dict[str, Any]) -> bool:
    decision = clean(item.get("coverage_decision")).lower()
    stage_status = subquestion_coverage_review.effective_coverage_stage_status(item)
    if stage_status in ITERATION_REVIEW_COVERAGE_STAGE_STATUSES:
        return False
    score = numeric(item.get("coverage_score_0_to_5") or item.get("evidence_sufficiency_0_to_5"))
    if stage_status == "final_sufficient":
        return decision == "sufficient" and score is not None and score >= COVERAGE_THRESHOLD
    if decision in TERMINAL_COVERAGE_DECISIONS:
        return True
    return decision == "sufficient" and score is not None and score >= COVERAGE_THRESHOLD


def coverage_review_subquestions(run_dir: Path) -> list[dict[str, Any]]:
    review = read_json(run_dir / "coverage-review" / "subquestion-coverage-review.json", {})
    items = review.get("subquestions") if isinstance(review, dict) else []
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def coverage_needs_scoring(item: dict[str, Any]) -> bool:
    stage_status = subquestion_coverage_review.effective_coverage_stage_status(item)
    return stage_status not in ITERATION_REVIEW_COVERAGE_STAGE_STATUSES and not coverage_is_terminal(item)


def coverage_gate_has_iteration_handoff(run_dir: Path) -> bool:
    return bool(next_iteration_review_subquestion_id(run_dir))


def next_coverage_subquestion_id(run_dir: Path) -> str:
    items = coverage_review_subquestions(run_dir)
    for item in items:
        if not coverage_needs_scoring(item):
            continue
        subquestion_id = clean(item.get("subquestion_id"))
        if subquestion_id:
            return subquestion_id
    return ""


def next_iteration_review_subquestion_id(run_dir: Path) -> str:
    items = coverage_review_subquestions(run_dir)
    for item in items:
        status = subquestion_coverage_review.effective_coverage_stage_status(item)
        if status not in ITERATION_REVIEW_COVERAGE_STAGE_STATUSES:
            continue
        subquestion_id = clean(item.get("subquestion_id"))
        if subquestion_id:
            return subquestion_id
    return ""


def iteration_number(path: Path) -> int:
    for part in reversed(path.parts):
        if part.startswith("iteration-"):
            suffix = part.removeprefix("iteration-")
            if suffix.isdigit():
                return int(suffix)
    return 0


def current_iteration_amendment(run_dir: Path) -> Path | None:
    subquestion_id = next_iteration_review_subquestion_id(run_dir)
    if subquestion_id:
        candidates = sorted(
            run_dir.glob(f"loop-state/{subquestion_id}/iteration-*/query-plan-amendment.json"),
            key=iteration_number,
            reverse=True,
        )
        return candidates[0] if candidates else None
    return latest_query_plan_amendment(run_dir)


def command_for_stage(run_dir: Path, state: dict[str, Any], stage: str) -> list[str]:
    cfg = state.get("config", {})
    py = sys.executable
    if stage == "publisher_discovery":
        command = [
            py,
            str(SCRIPT_DIR / "incremental_capture.py"),
            "--claim",
            cfg.get("claim") or DEFAULT_CLAIM,
            "--approved-query-plan",
            str(run_dir / "query-plan-preview.json"),
            "--rounds",
            str(cfg.get("subquestion_count") or DEFAULT_SUBQUESTION_COUNT),
            "--max-queries",
            str(cfg.get("max_queries_per_subquestion") or DEFAULT_MAX_QUERIES_PER_SUBQUESTION),
            "--max-pages",
            "1",
            "--max-results-per-page",
            "20",
            "--discovery-only",
            "--discovery-backend",
            "opencli",
            "--opencli-session",
            cfg.get("opencli_discovery_session") or DEFAULT_OPENCLI_DISCOVERY_SESSION,
            "--manual-blocker-wait-ms",
            "12000",
            "--abstract-expand-wait-ms",
            "700",
            "--write-query-refinement-packets",
            "--include-structured-publishers",
            "--existing-run-dir",
            str(run_dir),
            "--output-root",
            str(run_dir.parent),
        ]
        publishers = [p for p in (cfg.get("structured_publishers") or []) if p in DEFAULT_CAPTURE_PUBLISHERS]
        if len(publishers) == 1:
            command.extend(["--publisher", publishers[0]])
        return command
    if stage == "abstract_preview":
        command = [
            py,
            str(SCRIPT_DIR / "abstract_preview.py"),
            str(run_dir),
            "--opencli-session",
            cfg.get("opencli_preview_session") or DEFAULT_OPENCLI_PREVIEW_SESSION,
        ]
        abstract_queues = sorted(
            run_dir.glob("query-refinement/iteration-*/applied-decisions/abstract-preview-queue.csv"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if abstract_queues:
            command.extend(["--abstract-queue", str(abstract_queues[0])])
        return command
    if stage == "apply_query_decisions":
        if list((run_dir / "abstract-preview").glob("abstract-capture-review-full-*.json")):
            return [py, str(SCRIPT_DIR / "apply_abstract_capture_reviews.py"), str(run_dir)]
        return [py, str(SCRIPT_DIR / "apply_query_decisions.py"), str(run_dir)]
    if stage == "publisher_capture":
        return [
            py,
            str(SCRIPT_DIR / "capture_decision_queue.py"),
            str(run_dir),
            "--opencli-session",
            cfg.get("opencli_capture_session") or DEFAULT_OPENCLI_CAPTURE_SESSION,
        ]
    if stage == "coverage_review":
        return [py, str(SCRIPT_DIR / "subquestion_coverage_review.py"), str(run_dir)]
    if stage == "coverage_gate":
        subquestion_id = next_coverage_subquestion_id(run_dir)
        if not subquestion_id:
            return []
        return [
            py,
            str(SCRIPT_DIR / "run_subquestion_loop.py"),
            str(run_dir),
            "--subquestion-id",
            subquestion_id,
            "--iteration-budget",
            str(cfg.get("iteration_budget") or DEFAULT_ITERATION_BUDGET),
        ]
    if stage == "query_iteration_plan":
        subquestion_id = next_iteration_review_subquestion_id(run_dir)
        if not subquestion_id:
            return []
        return [
            py,
            str(SCRIPT_DIR / "query_iteration_review.py"),
            str(run_dir),
            "--subquestion-id",
            subquestion_id,
            "--iteration",
            str(next_query_iteration_index(run_dir)),
        ]
    if stage == "query_iteration":
        iteration_budget = int(cfg.get("iteration_budget") or DEFAULT_ITERATION_BUDGET)
        command = [
            py,
            str(SCRIPT_DIR / "continue_query_iteration.py"),
            str(run_dir),
            "--iteration",
            str(next_query_iteration_index(run_dir)),
            "--total-iterations",
            str(iteration_budget + 1),
            "--year-start",
            str(cfg.get("year_start") or DEFAULT_YEAR_START),
            "--year-end",
            str(cfg.get("year_end") or DEFAULT_YEAR_END),
            "--discovery-backend",
            "opencli",
            "--opencli-session",
            cfg.get("opencli_discovery_session") or DEFAULT_OPENCLI_DISCOVERY_SESSION,
            "--manual-blocker-wait-ms",
            "12000",
            "--abstract-expand-wait-ms",
            "700",
        ]
        amendment = current_iteration_amendment(run_dir)
        if amendment:
            command.extend(["--approved-query-plan", str(amendment)])
        else:
            return []
        return command
    if stage == "reference_followup":
        return [
            py,
            str(SCRIPT_DIR / "reference_followup.py"),
            str(run_dir),
            "--candidate-pool-size",
            "20",
            "--final-top-n",
            str(DEFAULT_REFERENCE_LIMIT_PER_SUBQUESTION),
        ]
    if stage == "verify":
        return [py, str(SCRIPT_DIR / "literature_loop.py"), "verify", str(run_dir)]
    return []


def default_runner(command: list[str], cwd: Path) -> int:
    return subprocess.run(command, cwd=cwd).returncode


def advance_once(run_dir: Path, *, runner: Runner = default_runner, dry_run: bool = False) -> dict[str, Any]:
    state = load_state(run_dir)
    action = next_action(state)
    stage_name = action["stage"]
    if stage_name in {"complete", "paused"}:
        return action
    stage = state["stages"][stage_name]
    if stage.get("status") == "human_gate" or stage_name in HUMAN_GATE_STAGES:
        append_run_log(run_dir, "waiting_for_human", "waiting", {"stage": stage_name})
        return {"status": "waiting_for_human", "stage": stage_name, "required_action": stage.get("required_action", "")}
    blocker_reason = preflight_blocker(run_dir, state, stage_name)
    if blocker_reason:
        return block_stage(run_dir, state, stage_name, blocker_reason)
    command = command_for_stage(run_dir, state, stage_name)
    if not command:
        if stage_name == "coverage_gate" and coverage_gate_has_iteration_handoff(run_dir):
            stage["status"] = "complete"
            stage["updated_at"] = utc_now()
            save_state(run_dir, state)
            append_run_log(run_dir, "stage_complete", "success", {"stage": stage_name, "command": command})
            return {"status": "complete", "stage": stage_name}
        stage["status"] = "human_gate"
        stage["required_action"] = "manual_stage_completion"
        save_state(run_dir, state)
        append_run_log(run_dir, "manual_gate", "waiting", {"stage": stage_name})
        return {"status": "waiting_for_human", "stage": stage_name, "required_action": stage["required_action"]}
    if dry_run:
        return {"status": "dry_run", "stage": stage_name, "command": command}
    exit_code = runner(command, PROJECT_ROOT)
    if exit_code == 0 and stage_name == "coverage_gate":
        remaining = next_coverage_subquestion_id(run_dir)
        if remaining:
            stage["status"] = "pending"
            stage["updated_at"] = utc_now()
            save_state(run_dir, state)
            append_run_log(run_dir, "coverage_gate_partial", "partial", {"stage": stage_name, "next_subquestion_id": remaining})
            return {"status": "partial", "stage": stage_name, "next_subquestion_id": remaining}
    if exit_code == 0:
        stage["status"] = "complete"
        stage["updated_at"] = utc_now()
        save_state(run_dir, state)
        append_run_log(run_dir, "stage_complete", "success", {"stage": stage_name, "command": command})
        return {"status": "complete", "stage": stage_name}
    stage["attempts"] = int(stage.get("attempts") or 0) + 1
    cap = int(state.get("config", {}).get("attempt_cap") or DEFAULT_ATTEMPT_CAP)
    if stage["attempts"] >= cap:
        result = block_stage(run_dir, state, stage_name, f"stage failed {stage['attempts']} times", exit_code)
        append_run_log(run_dir, "stage_blocked_command", "blocked", {"stage": stage_name, "command": command})
        return result
    stage["status"] = "pending"
    save_state(run_dir, state)
    append_run_log(run_dir, "stage_failed", "failed", {"stage": stage_name, "exit_code": exit_code, "command": command})
    return {"status": "failed", "stage": stage_name, "exit_code": exit_code}


def iter_text_files(run_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in run_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() in {".json", ".jsonl", ".md", ".csv", ".txt", ".yaml", ".yml"}:
            files.append(path)
    return files


def capture_queue_rows(run_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in run_dir.glob("query-refinement/iteration-*/applied-decisions/capture-queue.csv"):
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows.extend(dict(row) for row in csv.DictReader(handle))
    return rows


def valid_agent_text(path: Path, *, fallback: bool = False) -> tuple[bool, str]:
    if not path.exists():
        return False, "missing"
    text = path.read_text(encoding="utf-8", errors="replace")
    if not clean(text):
        return False, "empty"
    lower = text.lower()
    if fallback:
        if "main_agent_fallback" not in lower:
            return False, "fallback missing review_mode main_agent_fallback"
        if "fallback_reason" not in lower and "reason:" not in lower:
            return False, "fallback missing reason"
        return True, ""
    if "review_mode" not in lower:
        return False, "response missing review_mode"
    if "subagent" in lower or "agent_id" in lower or "main_agent_fallback" in lower:
        return True, ""
    return False, "response missing agent identity"


def agent_response_ok(root: Path, response_name: str, fallback_name: str) -> tuple[bool, str]:
    response_path = root / response_name
    fallback_path = root / fallback_name
    response_ok, response_reason = valid_agent_text(response_path)
    if response_ok:
        return True, ""
    fallback_ok, fallback_reason = valid_agent_text(fallback_path, fallback=True)
    if fallback_ok:
        return True, ""
    if response_path.exists():
        return False, f"{response_name} invalid: {response_reason}"
    if fallback_path.exists():
        return False, f"{fallback_name} invalid: {fallback_reason}"
    return False, f"missing {response_name} or {fallback_name}"


def recommendation_provenance_ok(root: Path) -> tuple[bool, str]:
    recommendations = read_json(root / "query-refinement-recommendations.json", {})
    if not isinstance(recommendations, dict):
        return False, "query-refinement-recommendations.json is invalid"
    mode = clean(recommendations.get("review_mode")).lower()
    if mode == "subagent" and clean(recommendations.get("agent_id")):
        return agent_response_ok(root, "subagent-response.md", "main-agent-fallback.md")
    if mode == "main_agent_fallback" and clean(recommendations.get("fallback_reason")):
        return agent_response_ok(root, "subagent-response.md", "main-agent-fallback.md")
    return False, "query-refinement recommendations missing agent provenance"


def agent_gate_issues(run_dir: Path) -> list[str]:
    issues: list[str] = []
    if (run_dir / "agent-query-plan-packet.md").exists():
        if not (
            (run_dir / "agent-query-plan.json").exists()
            and (run_dir / "agent-query-plan-validated.json").exists()
            and (run_dir / "query-plan-preview.json").exists()
        ):
            issues.append("agent gate missing query-plan response or validated preview for root query planning")
    for prompt in run_dir.glob("query-refinement/iteration-*/query-refinement-agent-brief.md"):
        root = prompt.parent
        ok, reason = recommendation_provenance_ok(root)
        if not ok:
            issues.append(f"agent gate invalid query-refinement response for {root.relative_to(run_dir)}: {reason}")
    for prompt in run_dir.glob("subquestions/*/*/subagent-prompt.md"):
        root = prompt.parent
        ok, reason = agent_response_ok(root, "subagent-response.md", "main-agent-fallback.md")
        if not ok:
            issues.append(f"agent gate invalid subquestion response for {root.relative_to(run_dir)}: {reason}")
    for prompt in run_dir.glob("subquestions/*/*/reference-subagent-prompt.md"):
        root = prompt.parent
        ok, reason = agent_response_ok(root, "reference-subagent-response.md", "reference-main-agent-fallback.md")
        if not ok:
            issues.append(f"agent gate invalid reference response for {root.relative_to(run_dir)}: {reason}")
    return issues


def non_structured_capture_issues(run_dir: Path) -> list[str]:
    issues: list[str] = []
    for row in capture_queue_rows(run_dir):
        publisher = clean(row.get("publisher") or row.get("publisher_key") or row.get("source_key")).lower()
        title = clean(row.get("title") or row.get("href") or "untitled")
        if not publisher:
            issues.append(f"missing publisher in capture queue: {title}")
        elif publisher not in STRUCTURED_PUBLISHERS:
            issues.append(f"non-structured publisher in capture queue: {publisher}")
    return issues


def abstract_capture_review_issues(run_dir: Path) -> list[str]:
    preview = run_dir / "abstract-preview" / "abstract-preview.csv"
    if not preview.exists():
        return []
    try:
        rows = validate_abstract_capture_review.read_csv(preview)
    except Exception as exc:
        return [f"abstract-preview.csv is not readable: {exc}"]
    subquestion_ids = sorted({clean(row.get("subquestion_id")) for row in rows if clean(row.get("subquestion_id"))})
    if not subquestion_ids:
        return ["abstract-preview.csv has no subquestion_id values for abstract-capture review"]
    issues: list[str] = []
    for subquestion_id in subquestion_ids:
        review = run_dir / "abstract-preview" / f"abstract-capture-review-full-{safe_file_stem(subquestion_id)}.json"
        if not review.exists():
            issues.append(f"missing full abstract-capture review for {subquestion_id}")
            continue
        for issue in validate_abstract_capture_review.validate_review(review, preview):
            issues.append(f"{review.relative_to(run_dir)}: {issue}")
    return issues


def query_iteration_indices(run_dir: Path) -> list[int]:
    indices: list[int] = []
    for path in (run_dir / "query-refinement").glob("iteration-*"):
        suffix = path.name.removeprefix("iteration-")
        if suffix.isdigit():
            indices.append(int(suffix))
    return sorted(indices)


def used_query_iteration_count(run_dir: Path) -> int:
    return len([index for index in query_iteration_indices(run_dir) if index > 1])


def next_query_iteration_index(run_dir: Path) -> int:
    indices = query_iteration_indices(run_dir)
    return max(indices, default=1) + 1


def preflight_blocker(run_dir: Path, state: dict[str, Any], stage_name: str) -> str:
    if stage_name == "publisher_discovery":
        reason = query_plan_artifact_blocker(run_dir)
        if reason:
            return reason
    if stage_name == "publisher_capture":
        issues = non_structured_capture_issues(run_dir)
        issues.extend(abstract_capture_review_issues(run_dir))
        if issues:
            return "; ".join(issues[:5])
    if stage_name == "query_iteration":
        budget = int(state.get("config", {}).get("iteration_budget") or DEFAULT_ITERATION_BUDGET)
        if used_query_iteration_count(run_dir) >= budget:
            return f"query iteration budget exhausted ({budget} per subquestion)"
    return ""


def query_plan_artifact_blocker(run_dir: Path) -> str:
    required = [
        ("agent-authored query plan", run_dir / "agent-query-plan.json"),
        ("validated agent query plan", run_dir / "agent-query-plan-validated.json"),
        ("query-plan-preview.json", run_dir / "query-plan-preview.json"),
        ("query-plan-preview.md", run_dir / "query-plan-preview.md"),
    ]
    missing = [label for label, path in required if not path.exists()]
    if missing:
        return (
            "missing "
            + ", ".join(missing)
            + "; build query-plan-preview.json from an agent-authored query plan before OpenCLI publisher discovery"
        )
    return ""


def latest_query_plan_amendment(run_dir: Path) -> Path | None:
    candidates = sorted(
        [
            *run_dir.glob("query-refinement/iteration-*/query-plan-amendment.json"),
            *run_dir.glob("loop-state/*/iteration-*/query-plan-amendment.json"),
        ],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def load_query_plan_review(run_dir: Path) -> dict[str, Any]:
    path = query_plan_review_path(run_dir)
    data = read_json(path, {})
    if not isinstance(data, dict):
        data = {}
    items = data.get("items") if isinstance(data.get("items"), list) else []
    return {
        "schema_version": 1,
        "updated_at": data.get("updated_at", ""),
        "items": [item for item in items if isinstance(item, dict)],
    }


def save_query_plan_review(run_dir: Path, review: dict[str, Any]) -> None:
    review["schema_version"] = 1
    review["updated_at"] = utc_now()
    query_plan_review_path(run_dir).write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")


def next_query_plan_review_id(review: dict[str, Any]) -> str:
    max_id = 0
    for item in review.get("items") or []:
        match = re.match(r"qpr-(\d+)$", clean(item.get("id")))
        if match:
            max_id = max(max_id, int(match.group(1)))
    return f"qpr-{max_id + 1:03d}"


def add_query_plan_review_item(run_dir: Path, *, note: str, target: str = "", severity: str = "note") -> dict[str, Any]:
    severity = clean(severity).lower() or "note"
    if severity not in {"note", "correction"}:
        raise SystemExit("query-plan review severity must be note or correction")
    text = clean(note)
    if not text:
        raise SystemExit("query-plan review note cannot be empty")
    review = load_query_plan_review(run_dir)
    item = {
        "id": next_query_plan_review_id(review),
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "target": clean(target) or "query-plan",
        "severity": severity,
        "status": "open",
        "note": text,
        "resolution": "",
    }
    review["items"].append(item)
    save_query_plan_review(run_dir, review)
    return item


def resolve_query_plan_review_item(run_dir: Path, item_id: str, resolution: str = "") -> dict[str, Any]:
    review = load_query_plan_review(run_dir)
    target = clean(item_id)
    for item in review.get("items") or []:
        if clean(item.get("id")) == target:
            item["status"] = "resolved"
            item["updated_at"] = utc_now()
            item["resolution"] = clean(resolution)
            save_query_plan_review(run_dir, review)
            return item
    raise SystemExit(f"Unknown query-plan review item: {item_id}")


def unresolved_query_plan_corrections(run_dir: Path) -> list[dict[str, Any]]:
    review = load_query_plan_review(run_dir)
    return [
        item for item in review.get("items") or []
        if clean(item.get("severity")).lower() == "correction" and clean(item.get("status")).lower() != "resolved"
    ]


def html_text(value: Any) -> str:
    return html.escape(clean(value))


def html_list(items: list[str], *, empty: str = "None recorded.") -> str:
    cleaned = [html_text(item) for item in items if clean(item)]
    if not cleaned:
        return f"<li class='muted'>{html.escape(empty)}</li>"
    return "\n".join(f"<li>{item}</li>" for item in cleaned)


def openalex_overview_html(preview: dict[str, Any]) -> str:
    grounding = preview.get("openalex_grounding") if isinstance(preview.get("openalex_grounding"), dict) else {}
    works = grounding.get("works") if isinstance(grounding.get("works"), list) else []
    focus = preview.get("publisher_focus") if isinstance(preview.get("publisher_focus"), dict) else {}
    counts = focus.get("counts") if isinstance(focus.get("counts"), dict) else {}
    concept_hints = grounding.get("concept_hints") if isinstance(grounding.get("concept_hints"), list) else []
    seed_blocks = []
    for work in works[:8]:
        if not isinstance(work, dict):
            continue
        meta = [
            clean(work.get("year")),
            clean(work.get("venue")),
            clean(work.get("publisher")),
            f"cited {clean(work.get('cited_by_count'))}" if clean(work.get("cited_by_count")) else "",
        ]
        seed_blocks.append(
            "<li><strong>{title}</strong><br><span class='muted'>{meta}</span></li>".format(
                title=html_text(work.get("title")),
                meta=html.escape(" | ".join(item for item in meta if item)),
            )
        )
    concept_blocks = []
    for hint in concept_hints[:4]:
        if not isinstance(hint, dict):
            continue
        terms = ", ".join(clean(term) for term in (hint.get("terms") or [])[:8] if clean(term))
        concept_blocks.append(
            "<li><strong>{label}</strong>: {terms}<br><span class='muted'>{purpose}</span></li>".format(
                label=html_text(hint.get("label")),
                terms=html.escape(terms),
                purpose=html_text(hint.get("purpose")),
            )
        )
    publisher_focus = "".join(
        "<span class='pill'>{publisher}: {count}</span>".format(
            publisher=html.escape(str(publisher)),
            count=html.escape(str(counts.get(publisher, 0))),
        )
        for publisher in ["elsevier", "acs", "wiley", "springer"]
    )
    probe_queries = [clean(item) for item in grounding.get("probe_queries") or [] if clean(item)]
    return """
    <section class="panel">
      <h2>Grounding Overview</h2>
      <div class="overview-grid">
        <div>
          <h3>OpenAlex Status</h3>
          <p><span class="status">{status}</span> <span class="muted">API key: {api_key}</span></p>
          <h3>Publisher Focus</h3>
          <p>{publisher_focus}</p>
        </div>
        <div>
          <h3>OpenAlex Grounding Probes</h3>
          <p class="muted">Not final publisher queries.</p>
          <ul>{probe_queries}</ul>
        </div>
      </div>
      <h3>Key OpenAlex Seeds</h3>
      <ol class="seed-list">{seed_blocks}</ol>
      <h3>Concept Blocks Used For Planning</h3>
      <ul>{concept_blocks}</ul>
      <p class="callout">Grounding comes from OpenAlex metadata only. If you reject a subquestion or query, the next revision must run a targeted OpenAlex re-grounding pass for that concern before updating the query plan.</p>
    </section>
    """.format(
        status=html_text(grounding.get("status") or "unknown"),
        api_key="present" if grounding.get("api_key_present") else "missing",
        publisher_focus=publisher_focus or "<span class='muted'>No publisher focus data.</span>",
        probe_queries=html_list(probe_queries[:10]),
        seed_blocks="\n".join(seed_blocks) or "<li class='muted'>No OpenAlex seed works recorded.</li>",
        concept_blocks="\n".join(concept_blocks) or "<li class='muted'>No concept hints recorded.</li>",
    )


def subquestions_review_html(preview: dict[str, Any]) -> str:
    subquestions = preview.get("subquestions") if isinstance(preview.get("subquestions"), list) else []
    blocks = []
    for item in subquestions:
        if not isinstance(item, dict):
            continue
        provenance = {clean(row.get("query")): row for row in item.get("query_provenance") or [] if isinstance(row, dict)}
        query_cards = []
        for query in item.get("queries") or []:
            query = clean(query)
            if not query:
                continue
            row = provenance.get(query) or {}
            evidence = [clean(value) for value in row.get("evidence_source") or [] if clean(value)]
            query_cards.append(
                """
                <div class="query-card">
                  <div><span class="query">{query}</span> <span class="pill">{anchor}</span></div>
                  <p><strong>Why this query:</strong> {why}</p>
                  <p><strong>Expected evidence:</strong> {expected}</p>
                  <p><strong>OpenAlex seed:</strong> {evidence}</p>
                </div>
                """.format(
                    query=html_text(query),
                    anchor=html_text(row.get("anchor_type")),
                    why=html_text(row.get("non_redundancy_rationale")),
                    expected=html_text(row.get("expected_result_type")),
                    evidence=html.escape("; ".join(evidence) or "No seed recorded."),
                )
            )
        concept_terms = []
        for group in item.get("concept_groups") or []:
            if not isinstance(group, dict):
                continue
            label = clean(group.get("label"))
            terms = ", ".join(clean(term) for term in (group.get("terms") or [])[:8] if clean(term))
            if label or terms:
                concept_terms.append(f"{label}: {terms}" if label else terms)
        blocks.append(
            """
            <article class="subquestion">
              <div class="subquestion-head">
                <span class="id">{id}</span>
                <span class="family">{family}</span>
              </div>
              <h3>{text}</h3>
              <p><strong>Planning rationale:</strong> {rationale}</p>
              <p><strong>Concept anchors:</strong> {concepts}</p>
              <div class="query-grid">{query_cards}</div>
            </article>
            """.format(
                id=html_text(item.get("subquestion_id")),
                family=html_text(item.get("query_family")),
                text=html_text(item.get("subquestion_text")),
                rationale=html_text(item.get("query_rationale") or "Review the query reasons below."),
                concepts=html.escape("; ".join(concept_terms) or "No concept anchors recorded."),
                query_cards="\n".join(query_cards) or "<p class='muted'>No queries recorded.</p>",
            )
        )
    return "\n".join(blocks) or "<p class='muted'>No subquestions found in query-plan-preview.json.</p>"


def amendment_review_html(amendment: dict[str, Any], amendment_path: Path | None) -> str:
    subquestions = amendment.get("subquestions") if isinstance(amendment.get("subquestions"), list) else []
    if not amendment_path:
        return "<p class='muted'>No query iteration amendment has been generated yet.</p>"
    rows = []
    for item in subquestions:
        if not isinstance(item, dict):
            continue
        queries = ", ".join(clean(query) for query in (item.get("queries") or []) if clean(query))
        rows.append(
            "<li><strong>{id}</strong>: {text}<br><span class='muted'>queries: {queries}</span></li>".format(
                id=html_text(item.get("subquestion_id")),
                text=html_text(item.get("subquestion_text")),
                queries=html.escape(queries),
            )
        )
    return "<ol>{}</ol>".format("\n".join(rows) or "<li class='muted'>No amendment subquestions available.</li>")


def render_query_plan_review_html(run_dir: Path) -> str:
    preview = read_json(run_dir / "query-plan-preview.json", {})
    amendment_path = current_iteration_amendment(run_dir) or latest_query_plan_amendment(run_dir)
    amendment = read_json(amendment_path, {}) if amendment_path else {}
    review = load_query_plan_review(run_dir)
    items = review.get("items") or []
    item_blocks = []
    for item in items:
        css = "correction" if clean(item.get("severity")) == "correction" else "note"
        item_blocks.append(
            "<li class='{css}'>"
            "<strong>{id}</strong> [{severity}/{status}] "
            "<span class='target'>{target}</span><br>"
            "{note}"
            "{resolution}"
            "</li>".format(
                css=css,
                id=html.escape(clean(item.get("id"))),
                severity=html.escape(clean(item.get("severity"))),
                status=html.escape(clean(item.get("status"))),
                target=html.escape(clean(item.get("target"))),
                note=html.escape(clean(item.get("note"))),
                resolution=(
                    "<br><em>Resolution: {}</em>".format(html.escape(clean(item.get("resolution"))))
                    if clean(item.get("resolution")) else ""
                ),
            )
        )
    return """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Query Plan Review</title>
  <style>
    :root {{ color-scheme: light; --border: #d0d7de; --muted: #57606a; --ink: #24292f; --soft: #f6f8fa; --accent: #0969da; --danger: #d1242f; }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; color: var(--ink); line-height: 1.45; background: #ffffff; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 28px 32px 48px; }}
    header {{ border-bottom: 1px solid var(--border); padding: 26px 32px; background: linear-gradient(180deg, #f6f8fa, #ffffff); }}
    header .inner {{ max-width: 1180px; margin: 0 auto; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; letter-spacing: 0; }}
    h2 {{ margin: 0 0 14px; font-size: 20px; }}
    h3 {{ margin: 14px 0 8px; font-size: 15px; }}
    p {{ margin: 8px 0; }}
    ul, ol {{ margin-top: 8px; padding-left: 22px; }}
    li {{ margin: 8px 0; }}
    .muted, .target {{ color: var(--muted); }}
    .panel, .subquestion {{ border: 1px solid var(--border); border-radius: 8px; padding: 18px; margin: 18px 0; background: #ffffff; }}
    .overview-grid {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr); gap: 18px; }}
    .seed-list {{ columns: 2; column-gap: 28px; }}
    .seed-list li {{ break-inside: avoid; }}
    .callout {{ border-left: 4px solid var(--accent); background: #ddf4ff; padding: 10px 12px; border-radius: 4px; }}
    .correction {{ border-left: 4px solid var(--danger); padding: 10px 12px; background: #ffebe9; border-radius: 4px; }}
    .note {{ border-left: 4px solid var(--accent); padding: 10px 12px; background: #ddf4ff; border-radius: 4px; }}
    .subquestion-head {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 6px; }}
    .id {{ font-weight: 700; }}
    .family, .pill, .status {{ display: inline-block; border: 1px solid var(--border); border-radius: 999px; padding: 2px 8px; font-size: 12px; background: var(--soft); color: var(--muted); margin: 2px 4px 2px 0; }}
    .status {{ color: #1a7f37; border-color: #a6d8b8; background: #dafbe1; }}
    .query-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; margin-top: 12px; }}
    .query-card {{ border: 1px solid var(--border); border-radius: 8px; padding: 12px; background: var(--soft); }}
    .query {{ font-weight: 700; }}
    code {{ background: var(--soft); border: 1px solid var(--border); border-radius: 4px; padding: 1px 4px; }}
    @media (max-width: 760px) {{
      main, header {{ padding-left: 18px; padding-right: 18px; }}
      .overview-grid {{ grid-template-columns: 1fr; }}
      .seed-list {{ columns: 1; }}
    }}
  </style>
</head>
<body>
  <header><div class="inner">
    <h1>Query Plan Review</h1>
    <p class="muted">Review the OpenAlex-grounded plan before OpenCLI publisher discovery. Publisher URLs are intentionally hidden here; they remain in the machine JSON for execution.</p>
  </div></header>
  <main>
    <section class="panel">
      <h2>How To Comment</h2>
      <p>If a subquestion, seed, or query is unsatisfactory, tell the agent the target and the correction. The agent must record it with <code>review-plan --severity correction</code>, run a targeted OpenAlex-only re-grounding pass for that concern, update <code>agent-query-plan.json</code>, validate, rebuild the preview, and refresh this page before approval.</p>
    </section>
    {overview}
    <section class="panel">
      <h2>Review Items</h2>
      <ol>{items}</ol>
    </section>
    <section>
      <h2>Subquestions And Queries</h2>
      {subquestions}
    </section>
    <section class="panel">
      <h2>Latest Query Iteration Amendment</h2>
      {amendment}
    </section>
  </main>
</body>
</html>
""".format(
        items="\n".join(item_blocks) or "<li>No review items yet.</li>",
        overview=openalex_overview_html(preview if isinstance(preview, dict) else {}),
        subquestions=subquestions_review_html(preview if isinstance(preview, dict) else {}),
        amendment=amendment_review_html(amendment if isinstance(amendment, dict) else {}, amendment_path),
    )


def write_query_plan_review_html(run_dir: Path) -> Path:
    path = query_plan_review_html_path(run_dir)
    path.write_text(render_query_plan_review_html(run_dir), encoding="utf-8")
    return path


def review_plan(
    run_dir: Path,
    *,
    note: str = "",
    target: str = "",
    severity: str = "note",
    resolve: str = "",
    resolution: str = "",
    open_browser: bool = False,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    if note:
        add_query_plan_review_item(run_dir, note=note, target=target, severity=severity)
    if resolve:
        resolve_query_plan_review_item(run_dir, resolve, resolution)
    html_path = write_query_plan_review_html(run_dir)
    open_corrections = unresolved_query_plan_corrections(run_dir)
    payload = {
        "review_json": str(query_plan_review_path(run_dir)),
        "review_html": str(html_path),
        "agent_browser_instruction": (
            "Do not open this file with the user's Google Chrome/OpenCLI profile. "
            "Serve the run directory on 127.0.0.1 and open query-plan-review.html "
            "in the agent in-app browser."
        ),
        "open_corrections": len(open_corrections),
        "open_correction_ids": [clean(item.get("id")) for item in open_corrections],
    }
    if open_browser:
        payload["open_ignored"] = "--open is disabled to avoid using the user's Google Chrome/OpenCLI profile."
    return payload


def block_stage(run_dir: Path, state: dict[str, Any], stage_name: str, reason: str, exit_code: int | None = None) -> dict[str, Any]:
    stage = state["stages"][stage_name]
    stage["status"] = "blocked"
    stage["updated_at"] = utc_now()
    blocker = {
        "stage": stage_name,
        "reason": reason,
        "updated_at": utc_now(),
    }
    if exit_code is not None:
        blocker["exit_code"] = exit_code
    state.setdefault("blockers", []).append(blocker)
    save_state(run_dir, state)
    append_run_log(run_dir, "stage_blocked", "blocked", blocker)
    result: dict[str, Any] = {"status": "blocked", "stage": stage_name, "reason": reason}
    if exit_code is not None:
        result["exit_code"] = exit_code
    return result


def duplicate_capture_issues(run_dir: Path) -> list[str]:
    summary = run_dir / "run-summary.json"
    if not summary.exists():
        return []
    try:
        rows = json.loads(summary.read_text(encoding="utf-8"))
    except Exception:
        return ["run-summary.json is not valid JSON"]
    if not isinstance(rows, list):
        return ["run-summary.json is not a list"]
    seen: set[str] = set()
    issues: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = clean(row.get("doi") or row.get("url") or row.get("article_dir") or row.get("title")).lower()
        if not key:
            continue
        if key in seen:
            issues.append(f"duplicate captured article: {key}")
        seen.add(key)
    return issues


def verify_run(run_dir: Path, *, secret_values: list[str] | None = None) -> dict[str, Any]:
    issues: list[str] = []
    run_dir = run_dir.resolve()
    if not state_path(run_dir).exists():
        issues.append("missing loop-state.json")
        return {"ok": False, "issues": issues}
    state = load_state(run_dir)
    if not run_log_path(run_dir).exists():
        issues.append("missing loop-run-log.jsonl")
    if not state_md_path(run_dir).exists():
        issues.append("missing STATE.md")
    for name in STAGE_ORDER:
        if name == "verify":
            continue
        status = state.get("stages", {}).get(name, {}).get("status")
        if status not in {"complete", "skipped"}:
            issues.append(f"stage not complete: {name} ({status})")
    if not (run_dir / "overview.md").exists():
        issues.append("missing overview.md")
    issues.extend(agent_gate_issues(run_dir))
    issues.extend(non_structured_capture_issues(run_dir))
    issues.extend(abstract_capture_review_issues(run_dir))
    secret_values = [value for value in (secret_values or []) if value]
    for path in iter_text_files(run_dir):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for secret in secret_values:
            if secret and secret in text:
                issues.append(f"secret value leaked in {path.relative_to(run_dir)}")
    issues.extend(duplicate_capture_issues(run_dir))
    return {"ok": not issues, "issues": issues, "run_dir": str(run_dir)}


def write_doctor(report_path: Path | None = None) -> dict[str, Any]:
    report = doctor_report()
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def run_plan(args: argparse.Namespace) -> int:
    run_dir = (args.run_dir or default_run_dir()).resolve()
    original_request = clean(getattr(args, "request", "") or "")
    request_file = getattr(args, "request_file", None)
    if request_file:
        original_request = clean(Path(request_file).read_text(encoding="utf-8"))
    claim = normalize_claim_from_request(original_request, getattr(args, "claim", ""))
    requested_year_window = extract_year_window_from_request(original_request)
    year_start = args.year_start
    year_end = args.year_end
    if requested_year_window and (args.year_start, args.year_end) == (DEFAULT_YEAR_START, DEFAULT_YEAR_END):
        year_start, year_end = requested_year_window
    structured_publishers, unsupported_publishers = extract_publishers(original_request, getattr(args, "publishers", ""))
    config = LoopConfig(
        run_dir=run_dir,
        claim=claim,
        original_request=original_request,
        year_start=year_start,
        year_end=year_end,
        subquestion_count=args.rounds,
        max_queries_per_subquestion=args.max_queries,
        structured_publishers=structured_publishers,
        unsupported_publishers=unsupported_publishers,
    )
    command = [
        sys.executable,
        str(SCRIPT_DIR / "openalex_grounding.py"),
        "--claim",
        claim,
        "--rounds",
        str(args.rounds),
        "--year-start",
        str(year_start),
        "--year-end",
        str(year_end),
        "--grounding-notes",
        args.grounding_notes,
        "--exploration-source",
        args.exploration_source,
        "--output-dir",
        str(run_dir),
    ]
    if args.dry_run:
        print(json.dumps({"run_dir": str(run_dir), "command": command}, ensure_ascii=False, indent=2))
        return 0
    exit_code = default_runner(command, PROJECT_ROOT)
    if exit_code == 0:
        state = create_initial_state(config)
        append_run_log(run_dir, "openalex_grounding_packet_written", "success", {"stage": "query_plan"})
    else:
        state = create_initial_state(config, query_plan_ready=False)
        state["stages"]["query_plan"]["status"] = "blocked"
        state.setdefault("blockers", []).append({"stage": "query_plan", "reason": "openalex_grounding.py failed", "exit_code": exit_code})
        append_run_log(run_dir, "openalex_grounding_failed", "blocked", {"stage": "query_plan", "exit_code": exit_code})
    save_state(run_dir, state)
    print(f"run_dir={run_dir}")
    return exit_code


def approve_stage(run_dir: Path, stage_name: str) -> None:
    state = load_state(run_dir)
    if stage_name not in state.get("stages", {}):
        raise SystemExit(f"Unknown stage: {stage_name}")
    if stage_name == "query_plan_approval":
        blocker = query_plan_artifact_blocker(run_dir)
        if blocker:
            raise SystemExit(blocker)
    if stage_name == "query_iteration_plan_approval":
        if not current_iteration_amendment(run_dir):
            raise SystemExit("missing query-plan-amendment.json; run query_iteration_plan before approval")
    if stage_name in {"query_plan_approval", "query_iteration_plan_approval"}:
        corrections = unresolved_query_plan_corrections(run_dir)
        if corrections:
            ids = ", ".join(clean(item.get("id")) for item in corrections[:5])
            raise SystemExit(f"unresolved query-plan correction(s): {ids}; resolve them with review-plan before approval")
    state["stages"][stage_name]["status"] = "complete"
    state["stages"][stage_name]["updated_at"] = utc_now()
    save_state(run_dir, state)
    append_run_log(run_dir, "human_gate_approved", "success", {"stage": stage_name})


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Check local runtime without exposing secrets.")
    doctor.add_argument("--json-out", type=Path)

    plan = sub.add_parser("plan", help="Create a gated query-plan run directory.")
    plan.add_argument("request", nargs="?", default="", help="Optional natural-language request containing topic, years, and publishers.")
    plan.add_argument("--request-file", type=Path, help="Read the natural-language request from a file.")
    plan.add_argument("--run-dir", type=Path)
    plan.add_argument("--claim", default="")
    plan.add_argument("--publishers", default="", help="Comma-separated requested publishers; unsupported publishers are recorded as manual_hold.")
    plan.add_argument("--rounds", type=int, default=DEFAULT_SUBQUESTION_COUNT)
    plan.add_argument("--max-queries", type=int, default=DEFAULT_MAX_QUERIES_PER_SUBQUESTION)
    plan.add_argument("--year-start", type=int, default=DEFAULT_YEAR_START)
    plan.add_argument("--year-end", type=int, default=DEFAULT_YEAR_END)
    plan.add_argument("--grounding-notes", default="Literature review validation run for the target topic.")
    plan.add_argument(
        "--exploration-source",
        default="OpenAlex|https://openalex.org|metadata grounding",
    )
    plan.add_argument("--dry-run", action="store_true")

    for name in ["run", "resume"]:
        cmd = sub.add_parser(name, help=f"Advance the next legal stage ({name}).")
        cmd.add_argument("run_dir", type=Path)
        cmd.add_argument("--dry-run", action="store_true")

    status = sub.add_parser("status", help="Print current loop state.")
    status.add_argument("run_dir", type=Path)

    review = sub.add_parser("review-plan", help="Write query-plan review HTML and record notes or corrections.")
    review.add_argument("run_dir", type=Path)
    review.add_argument("--note", default="", help="Add a query-plan review note or correction.")
    review.add_argument("--target", default="", help="Subquestion id, query family, or query-plan section this note targets.")
    review.add_argument("--severity", choices=["note", "correction"], default="note")
    review.add_argument("--resolve", default="", help="Resolve an existing query-plan review item id, e.g. qpr-001.")
    review.add_argument("--resolution", default="", help="Resolution text for --resolve.")
    review.add_argument(
        "--open",
        action="store_true",
        help="Deprecated no-op. Do not use the system browser; open the review via the agent in-app browser on localhost.",
    )

    approve = sub.add_parser("approve", help="Mark a human-gated stage complete.")
    approve.add_argument("run_dir", type=Path)
    approve.add_argument("stage")

    verify = sub.add_parser("verify", help="Verify loop artifacts and safety boundaries.")
    verify.add_argument("run_dir", type=Path)
    verify.add_argument(
        "--secret-env",
        action="append",
        default=[],
        help="Additional environment variable whose value must not appear in artifacts. OpenAlex is checked by default when set.",
    )

    args = parser.parse_args()
    if args.command == "doctor":
        print_json(write_doctor(args.json_out))
        return 0
    if args.command == "plan":
        return run_plan(args)
    if args.command in {"run", "resume"}:
        print_json(advance_once(args.run_dir, dry_run=args.dry_run))
        return 0
    if args.command == "status":
        state = load_state(args.run_dir)
        print_json({"next": next_action(state), "state": state})
        return 0
    if args.command == "review-plan":
        print_json(
            review_plan(
                args.run_dir,
                note=args.note,
                target=args.target,
                severity=args.severity,
                resolve=args.resolve,
                resolution=args.resolution,
                open_browser=args.open,
            )
        )
        return 0
    if args.command == "approve":
        approve_stage(args.run_dir, args.stage)
        print(f"approved_stage={args.stage}")
        return 0
    if args.command == "verify":
        secret_names = ["OPENALEX_API_KEY", *args.secret_env]
        secrets = [os.environ.get(name, "") for name in secret_names]
        result = verify_run(args.run_dir, secret_values=secrets)
        print_json(result)
        return 0 if result["ok"] else 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
