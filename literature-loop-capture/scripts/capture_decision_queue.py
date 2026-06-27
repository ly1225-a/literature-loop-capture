#!/usr/bin/env python3
"""Capture article URLs approved by the literature-search agent via OpenCLI."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import capture_core as capture  # noqa: E402
import discovery_core as discovery  # noqa: E402
import incremental_capture as inc  # noqa: E402
import opencli_browser  # noqa: E402


STRUCTURED_PUBLISHERS = {"elsevier", "acs", "wiley", "springer"}
PUBLISHER_ORDER = ["elsevier", "acs", "wiley", "springer"]
PUBLISHER_HOME_URL = opencli_browser.PUBLISHER_HOME_URL


def clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


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


def latest_capture_queue(run_dir: Path) -> Path:
    candidates = sorted(
        run_dir.glob("query-refinement/iteration-*/applied-decisions/capture-queue.csv"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise SystemExit("No capture-queue.csv found. Run apply_query_decisions.py first.")
    return candidates[0]


def plan_for_row(row: dict[str, str], query_rounds: list[dict[str, Any]]) -> discovery.QueryPlan:
    subquestion_id = clean(row.get("subquestion_id"))
    row_subquestion_text = clean(row.get("subquestion_text"))
    row_query = clean(row.get("query_text"))
    if subquestion_id and clean(row.get("subquestion_slug")) and clean(row.get("subquestion_group_slug")) and row_subquestion_text and row_query:
        return discovery.QueryPlan(
            1,
            clean(row.get("query_family")) or "agent-selected",
            row_subquestion_text,
            [row_query],
            subquestion_id=subquestion_id,
            subquestion_slug=clean(row.get("subquestion_slug")),
            group_slug=clean(row.get("subquestion_group_slug")),
            group_title=clean(row.get("subquestion_group_title")) or "General",
        )
    for item in query_rounds:
        if clean(item.get("subquestion_id")) != subquestion_id:
            continue
        return discovery.QueryPlan(
            int(item.get("round") or 1),
            clean(item.get("query_family")) or "agent-selected",
            clean(item.get("claim_subquestion")) or clean(item.get("subquestion_text")) or "Agent-selected capture.",
            [row_query or clean((item.get("queries") or [""])[0])],
            subquestion_id=clean(item.get("subquestion_id")),
            subquestion_slug=clean(item.get("subquestion_slug")),
            group_slug=clean(item.get("subquestion_group_slug")) or "general",
            group_title=clean(item.get("subquestion_group_title")) or "General",
            concept_groups=item.get("concept_groups") if isinstance(item.get("concept_groups"), list) else [],
            boolean_query=clean(item.get("boolean_query")),
            publisher_queries=item.get("publisher_queries") if isinstance(item.get("publisher_queries"), dict) else {},
        )
    raise SystemExit(
        "Capture queue row is not traceable to a subquestion. "
        "Regenerate capture-queue.csv with apply_query_decisions.py."
    )


def default_capture_args(run_dir: Path, args: argparse.Namespace) -> argparse.Namespace:
    today = date.today()
    question = load_json(run_dir / "question.json", {})
    return argparse.Namespace(
        claim=clean(question.get("question") if isinstance(question, dict) else "") or args.claim,
        review_context=clean(question.get("review_context") if isinstance(question, dict) else "") or args.review_context,
        capture_depth=args.capture_depth,
        parent_article_dir="",
        parent_reference_index="",
        agent_owner=args.agent_owner,
        year_start=args.year_start or today.year - 4,
        year_end=args.year_end or today.year,
        min_chars=args.min_chars,
        write_snapshot_html=False,
    )


def row_key(row: dict[str, Any]) -> str:
    publisher = clean(row.get("publisher")).lower()
    identity = clean(row.get("doi") or row.get("url") or row.get("href") or row.get("title")).lower()
    return f"{publisher}:{identity}"


def existing_capture_keys(summary_rows: list[dict[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for row in summary_rows:
        if clean(row.get("status")) != "captured":
            continue
        publisher = clean(row.get("publisher")).lower()
        for value in [row.get("doi"), row.get("url"), row.get("title")]:
            identity = clean(value).lower()
            if publisher and identity:
                keys.add(f"{publisher}:{identity}")
    return keys


def queue_rows(path: Path, limit: int) -> list[dict[str, str]]:
    rows = [row for row in read_csv(path) if clean(row.get("href") or row.get("landing_url"))]
    rows = interleave_rows_by_publisher(rows)
    if limit:
        rows = rows[:limit]
    return rows


def interleave_rows_by_publisher(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    buckets: dict[str, list[dict[str, str]]] = {publisher: [] for publisher in PUBLISHER_ORDER}
    extras: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        publisher = clean(row.get("publisher")).lower()
        if publisher in buckets:
            buckets[publisher].append(row)
        else:
            extras.setdefault(publisher, []).append(row)

    ordered_publishers = PUBLISHER_ORDER + sorted(extras)
    out: list[dict[str, str]] = []
    while True:
        added = False
        for publisher in ordered_publishers:
            bucket = buckets.get(publisher) or extras.get(publisher) or []
            if bucket:
                out.append(bucket.pop(0))
                added = True
        if not added:
            break
    return out


def validate_rows(rows: list[dict[str, str]]) -> None:
    for row in rows:
        href = clean(row.get("href") or row.get("landing_url"))
        publisher = clean(row.get("publisher")).lower() or discovery.infer_publisher(href)
        if publisher not in STRUCTURED_PUBLISHERS:
            raise SystemExit(f"Unsupported structured capture publisher: {publisher or 'missing'}")
        try:
            discovery.ensure_allowed_article_url(href)
        except Exception as exc:
            raise SystemExit(
                "Structured full-text capture requires a supported direct publisher URL. "
                f"publisher={publisher or 'missing'} href={href or 'missing'}"
            ) from exc


def auth_error_from_exception(exc: Exception) -> str:
    detail = f"{type(exc).__name__}: {exc}"
    low = detail.lower()
    if "publisher_auth_required" in low or "login" in low or "sign in" in low or "authentication" in low:
        return detail[:500]
    if "opencli_navigation_not_ready" in low and "publisher" in low:
        return detail[:500]
    return ""


def preflight_publisher_home(session: str, args: argparse.Namespace, run_dir: Path) -> None:
    if getattr(args, "skip_publisher_preflight", False):
        return
    home_url = clean(getattr(args, "publisher_home_url", "")) or PUBLISHER_HOME_URL
    inc.append_log(run_dir, {"event": "opencli-publisher-preflight-started", "session": session, "url": home_url})
    snapshot, problem = opencli_browser.preflight_publisher_home(
        session,
        home_url=home_url,
        expected_hosts={"sciencedirect.com"},
        wait_ms=int(getattr(args, "publisher_auth_wait_ms", 3000) or 3000),
        timeout=90,
    )
    if problem:
        inc.append_log(run_dir, {"event": "opencli-publisher-preflight-blocked", "session": session, "problem": problem, "url": snapshot.get("url")})
        raise SystemExit(
            "OpenCLI publisher authentication is required before capture. "
            f"Problem: {problem}. Log in through the connected Chrome/OpenCLI window at "
            f"{home_url}, close or leave the window authenticated, then rerun capture."
        )
    inc.append_log(run_dir, {"event": "opencli-publisher-preflight-ok", "session": session, "url": snapshot.get("url")})


def extraction_problem(data: dict[str, Any], args: argparse.Namespace) -> str:
    fulltext = clean(data.get("fullText") or data.get("body") or "")
    if len(fulltext) < int(args.min_chars):
        return "insufficient_fulltext"
    if not clean(data.get("title") or data.get("documentTitle")):
        return "missing_title"
    if not clean(data.get("doi") or data.get("url")):
        return "missing_doi_or_stable_url"
    return ""


def write_capture_row(
    *,
    run_dir: Path,
    summary_rows: list[dict[str, Any]],
    queue_row: dict[str, str],
    publisher: str,
    plan: discovery.QueryPlan,
    data: dict[str, Any],
    capture_args: argparse.Namespace,
    status: str,
    skip_reason: str = "",
    opencli_session: str = "",
) -> None:
    if status == "captured":
        data = capture.normalize_article_data(data)
    result = {
        "title": clean(queue_row.get("title")) or clean(data.get("title")),
        "href": clean(queue_row.get("href") or queue_row.get("landing_url")) or clean(data.get("url")),
        "_page_rank": clean(queue_row.get("rank")) or "",
        "doi": clean(queue_row.get("doi")) or clean(data.get("doi")),
        "abstract": clean(queue_row.get("abstract")) or clean(data.get("abstract")),
        "year": clean(queue_row.get("year")) or clean(data.get("year")),
        "journal": clean(queue_row.get("journal")) or clean(data.get("journal")),
    }
    job = {"query": clean(queue_row.get("query_text")), "plan": plan}
    row = inc.row_base(result, publisher, capture_args, job, 1)
    if row.get("source_role") == "reference":
        parent_article_dir = clean(queue_row.get("parent_article_dir") or queue_row.get("source_article_dir"))
        parent_reference_index = clean(queue_row.get("parent_reference_index") or queue_row.get("source_reference_index"))
        row["parent_article_dir"] = parent_article_dir
        row["parent_reference_index"] = parent_reference_index
        row["reference_provenance"] = (
            f"{parent_article_dir}#ref-{parent_reference_index}"
            if parent_article_dir or parent_reference_index
            else ""
        )
    row["status"] = status
    row["skip_reason"] = skip_reason
    row["title"] = clean(data.get("title") or result["title"])
    row["doi"] = clean(data.get("doi") or result["doi"])
    row["url"] = clean(data.get("url") or result["href"])
    row["abstract"] = clean(data.get("abstract") or result["abstract"])[:1500]
    row["authors"] = "; ".join(data.get("authors") or []) if isinstance(data.get("authors"), list) else clean(data.get("authors"))
    row["year"] = clean(data.get("year") or result["year"])
    row["journal"] = clean(data.get("journal") or result["journal"])
    row["keywords"] = data.get("keywords") or ""
    row["fulltext_chars"] = len(data.get("fullText") or "")
    row["section_count"] = len(data.get("sectionBlocks") or [])
    row["figure_count"] = len(data.get("figures") or [])
    row["table_count"] = len(data.get("tables") or [])
    row["captured_at"] = datetime.now().isoformat(timespec="seconds")
    row["note_status"] = "pending_agent" if status == "captured" else ""
    if status == "captured":
        parent_dir = inc.article_parent_dir(run_dir, publisher, job, capture_args)
        article_dir = inc.next_numbered_dir(parent_dir, "ref" if row.get("source_role") == "reference" else "primary")
        metadata = {
            "working_article_id": article_dir.name,
            "final_name_hint": inc.article_dir_name(row["year"] or "unknown-year", row["title"] or "Untitled", row["doi"]),
            "publisher": publisher,
            "source_platform": "OpenCLI publisher",
            "source_bucket": row["source_bucket"],
            "source_role": row["source_role"],
            "subquestion_group_slug": row["subquestion_group_slug"],
            "subquestion_group_title": row["subquestion_group_title"],
            "subquestion_id": row["subquestion_id"],
            "subquestion_slug": row["subquestion_slug"],
            "subquestion_text": row["subquestion_text"],
            "claim": capture_args.claim,
            "review_context": capture_args.review_context or "",
            "query_round": row.get("query_round"),
            "query_family": row.get("query_family"),
            "query_text": row.get("query_text"),
            "discovery_rank": row.get("discovery_rank"),
            "capture_depth": row.get("capture_depth"),
            "agent_owner": row.get("agent_owner") or "",
            "parent_article_dir": row.get("parent_article_dir") or "",
            "parent_reference_index": row.get("parent_reference_index") or "",
            "reference_provenance": row.get("reference_provenance") or "",
            "captured_at": row["captured_at"],
        }
        capture.write_article_artifacts(article_dir, data, metadata=metadata)
        if opencli_session:
            capture.write_figure_exports_opencli(opencli_session, article_dir, data)
        inc.write_indexes(article_dir, data)
        inc.write_auto_extract_note(article_dir, data, capture_args)
        inc.write_agent_note_placeholder(article_dir, data, capture_args)
        row["article_dir"] = str(article_dir)
    summary_rows.append(row)
    inc.write_run_outputs(run_dir, summary_rows)


def opencli_extract_article(session: str, href: str, args: argparse.Namespace) -> dict[str, Any]:
    opencli_browser.open_url(session, href, timeout=90)
    opencli_browser.settle_article_page(
        session,
        initial_wait_ms=max(
            int(getattr(args, "article_open_wait_ms", 5000) or 5000),
            int(getattr(args, "article_settle_ms", 0) or getattr(args, "settle_ms", 0) or 0),
        ),
        scroll_rounds=int(getattr(args, "scroll_rounds", 0) or 0),
        scroll_wait_ms=int(getattr(args, "scroll_wait_ms", 1000) or 1000),
        final_wait_ms=int(getattr(args, "article_final_settle_ms", 0) or 0),
    )
    data = opencli_browser.eval_json(session, capture.article_extraction_script(), timeout=120)
    if not isinstance(data, dict):
        raise RuntimeError("opencli_article_extract_invalid_payload")
    return data


async def run(args: argparse.Namespace) -> Path:
    run_dir = args.run_dir.resolve()
    queue_path = (args.capture_queue or latest_capture_queue(run_dir)).resolve()
    rows = queue_rows(queue_path, args.limit)
    validate_rows(rows)
    query_rounds = load_json(run_dir / "query-rounds.json", [])
    if not isinstance(query_rounds, list):
        query_rounds = []
    summary_rows = load_json(run_dir / "run-summary.json", [])
    if not isinstance(summary_rows, list):
        summary_rows = []
    captured = existing_capture_keys(summary_rows)
    capture_args = default_capture_args(run_dir, args)
    session = clean(args.opencli_session) or "lit-capture"
    preflight_publisher_home(session, args, run_dir)
    try:
        for row in rows:
            href = clean(row.get("href") or row.get("landing_url"))
            publisher = clean(row.get("publisher")).lower() or discovery.infer_publisher(href)
            plan = plan_for_row(row, query_rounds)
            candidate_keys = {
                f"{publisher}:{clean(row.get('doi')).lower()}",
                f"{publisher}:{href.lower()}",
                f"{publisher}:{clean(row.get('title')).lower()}",
            }
            if captured.intersection(key for key in candidate_keys if not key.endswith(":")):
                inc.append_log(run_dir, {"event": "opencli-capture-duplicate-skipped", "publisher": publisher, "href": href})
                continue
            inc.append_log(run_dir, {"event": "opencli-capture-started", "publisher": publisher, "href": href, "title": clean(row.get("title"))})
            try:
                data = await asyncio.to_thread(opencli_extract_article, session, href, args)
                if row.get("title") and not clean(data.get("title")):
                    data["title"] = clean(row.get("title"))
                if row.get("doi") and not clean(data.get("doi")):
                    data["doi"] = clean(row.get("doi"))
                if row.get("abstract") and not clean(data.get("abstract")):
                    data["abstract"] = clean(row.get("abstract"))
                problem = extraction_problem(data, args)
                if problem:
                    write_capture_row(
                        run_dir=run_dir,
                        summary_rows=summary_rows,
                        queue_row=row,
                        publisher=publisher,
                        plan=plan,
                        data=data,
                        capture_args=capture_args,
                        status="skipped",
                        skip_reason=problem,
                    )
                    inc.append_log(run_dir, {"event": "opencli-capture-skipped", "publisher": publisher, "href": href, "reason": problem})
                    continue
                write_capture_row(
                    run_dir=run_dir,
                    summary_rows=summary_rows,
                    queue_row=row,
                    publisher=publisher,
                    plan=plan,
                    data=data,
                    capture_args=capture_args,
                    status="captured",
                    opencli_session=session,
                )
                captured.add(row_key(row))
                inc.append_log(run_dir, {"event": "opencli-capture-finished", "publisher": publisher, "href": href})
            except Exception as exc:
                auth_error = auth_error_from_exception(exc)
                data = {"title": clean(row.get("title")), "doi": clean(row.get("doi")), "url": href, "abstract": clean(row.get("abstract")), "fullText": ""}
                write_capture_row(
                    run_dir=run_dir,
                    summary_rows=summary_rows,
                    queue_row=row,
                    publisher=publisher,
                    plan=plan,
                    data=data,
                    capture_args=capture_args,
                    status="error",
                    skip_reason=f"{type(exc).__name__}: {str(exc)[:500]}",
                )
                inc.append_log(run_dir, {"event": "opencli-capture-error", "publisher": publisher, "href": href, "error": str(exc)[:500]})
                if auth_error:
                    inc.append_log(run_dir, {"event": "opencli-capture-auth-blocked", "publisher": publisher, "href": href, "error": auth_error})
                    raise SystemExit(
                        "OpenCLI publisher authentication expired during capture. "
                        "Log in through the connected Chrome/OpenCLI publisher window, then rerun capture; "
                        "already captured articles will be skipped."
                    )
            if args.article_delay_ms:
                await asyncio.sleep(args.article_delay_ms / 1000)
    finally:
        try:
            await asyncio.to_thread(opencli_browser.close, session, timeout=15)
        except Exception:
            pass
    return run_dir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--capture-queue", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--opencli-session", default="lit-capture")
    parser.add_argument("--claim", default="")
    parser.add_argument("--review-context", default="")
    parser.add_argument("--capture-depth", type=int, default=1)
    parser.add_argument("--agent-owner", default="literature-search-agent")
    parser.add_argument("--year-start", type=int, default=0)
    parser.add_argument("--year-end", type=int, default=0)
    parser.add_argument("--min-chars", type=int, default=2500)
    parser.add_argument("--settle-ms", type=int, default=5000)
    parser.add_argument("--article-open-wait-ms", type=int, default=5000)
    parser.add_argument("--article-settle-ms", type=int, default=3000)
    parser.add_argument("--article-final-settle-ms", type=int, default=3000)
    parser.add_argument("--scroll-rounds", type=int, default=8)
    parser.add_argument("--scroll-wait-ms", type=int, default=1000)
    parser.add_argument("--article-delay-ms", type=int, default=5000)
    parser.add_argument("--publisher-home-url", default=PUBLISHER_HOME_URL)
    parser.add_argument("--publisher-auth-wait-ms", type=int, default=3000)
    parser.add_argument("--skip-publisher-preflight", action="store_true")
    args = parser.parse_args()
    run_dir = asyncio.run(run(args))
    print(f"output_root={run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
