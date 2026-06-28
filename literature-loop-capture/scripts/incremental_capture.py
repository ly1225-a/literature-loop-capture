#!/usr/bin/env python3
"""Article-first publisher-authenticated literature discovery and capture."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
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
import opencli_browser  # noqa: E402
import query_refinement  # noqa: E402


RUN_SUMMARY_FIELDS = [
    "subquestion_group_slug",
    "subquestion_group_title",
    "subquestion_id",
    "subquestion_slug",
    "subquestion_text",
    "publisher",
    "source_platform",
    "source_bucket",
    "source_role",
    "query_round",
    "query_family",
    "query_text",
    "page",
    "discovery_rank",
    "capture_depth",
    "parent_article_dir",
    "parent_reference_index",
    "agent_owner",
    "status",
    "skip_reason",
    "title",
    "authors",
    "year",
    "journal",
    "volume",
    "issue",
    "pages",
    "doi",
    "url",
    "article_type",
    "abstract",
    "keywords",
    "cited_by_count",
    "openalex_id",
    "crossref_type",
    "fulltext_chars",
    "section_count",
    "figure_count",
    "table_count",
    "note_status",
    "worth_close_reading",
    "agent_score",
    "agent_assessment_status",
    "reference_provenance",
    "article_dir",
    "captured_at",
    "review_context_fit",
]

DISCOVERY_BACKENDS = {"opencli"}


def safe_name(text: str, limit: int = 55) -> str:
    text = re.sub(r"[\\/:*?\"<>|]+", "_", text or "")
    text = re.sub(r"\s+", " ", text).strip(" ._-")
    text = text.replace(" ", "_")
    return (text[:limit].strip(" ._-") or "untitled")


def doi_token(doi: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", doi or "").strip("_")[:60]


def article_dir_name(year: str, title: str, doi: str) -> str:
    # Kept for old manifests only. New captures use numeric article folder
    # names and keep title/DOI in metadata.
    base = f"{year}_{safe_name(title, 36)}"
    return base[:48].strip(" ._-")


def unique_dir(root: Path, name: str) -> Path:
    candidate = root / name
    if not candidate.exists():
        return candidate
    for i in range(2, 1000):
        candidate = root / f"{name}_{i}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Too many duplicate folders for {name}")


def next_numbered_dir(root: Path, prefix: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    used: set[int] = set()
    pattern = re.compile(rf"^{re.escape(prefix)}_(\d+)$")
    for child in root.iterdir():
        if not child.is_dir():
            continue
        match = pattern.match(child.name)
        if match:
            used.add(int(match.group(1)))
    index = 1
    while index in used:
        index += 1
    return root / f"{prefix}_{index:03d}"


def source_bucket_for(publisher: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", publisher or "unknown").strip("_").lower()
    return value or "unknown"


def plan_subquestion_id(plan: Any) -> str:
    value = getattr(plan, "subquestion_id", "") or f"{int(getattr(plan, 'round_index', 1) or 1):02d}_{safe_name(getattr(plan, 'query_family', '') or 'subquestion', 14)}"
    return safe_name(value, 55)


def plan_subquestion_slug(plan: Any) -> str:
    value = getattr(plan, "subquestion_slug", "") or safe_name(getattr(plan, "query_family", "") or getattr(plan, "claim_subquestion", "") or "subquestion", 18)
    return safe_name(value, 40)


def plan_group_slug(plan: Any) -> str:
    return safe_name(getattr(plan, "group_slug", "") or "general", 40)


def plan_group_title(plan: Any) -> str:
    return getattr(plan, "group_title", "") or plan_group_slug(plan).replace("-", " ").title()


def subquestion_dir(output_root: Path, plan: Any) -> Path:
    return output_root / "subquestions" / plan_group_slug(plan) / plan_subquestion_id(plan)


def plan_concept_groups(plan: Any) -> list[dict[str, Any]]:
    groups = getattr(plan, "concept_groups", None)
    return groups if isinstance(groups, list) else []


def evidence_guidance(query_family: str) -> dict[str, str]:
    family = (query_family or "").lower()
    if "definition" in family or "landscape" in family:
        return {
            "include": "reviews, surveys, definitions, taxonomies, field-framing papers",
            "exclude": "single narrow applications with no reusable framing",
            "evidence": "definitions, scope boundaries, named concepts, representative review claims",
            "reference": "prefer seminal reviews, taxonomies, or papers repeatedly used to frame the field",
        }
    if "data" in family or "resource" in family:
        return {
            "include": "datasets, databases, benchmarks, corpora, ontologies, tools, reusable resources",
            "exclude": "papers that only mention a resource without describing or evaluating it",
            "evidence": "resource name, schema/entities, coverage, access/licensing, update status, benchmark use",
            "reference": "prefer primary resource papers, benchmark papers, and reusable dataset/tool descriptions",
        }
    if "method" in family or "model" in family:
        return {
            "include": "methods, models, algorithms, pipelines, construction or analysis workflows",
            "exclude": "pure application papers without enough method detail",
            "evidence": "inputs, outputs, model/workflow steps, baselines, implementation clues, reproducibility",
            "reference": "prefer method papers that explain the core technique or establish a reusable workflow",
        }
    if "application" in family or "case" in family:
        return {
            "include": "applied studies, deployments, case studies, decision-support workflows",
            "exclude": "papers with speculative applications but no demonstrated use case",
            "evidence": "task, user/scenario, data, outcome metrics, deployment constraints, practical value",
            "reference": "prefer application papers with clear task formulation and transferable evidence",
        }
    if "evaluation" in family or "benchmark" in family or "limitation" in family or "gap" in family:
        return {
            "include": "evaluations, benchmarks, comparisons, limitation analyses, perspectives, failure cases",
            "exclude": "papers making strong claims without comparable evidence",
            "evidence": "metrics, baselines, datasets, negative findings, threats to validity, open gaps",
            "reference": "prefer benchmark, comparison, and limitation papers that explain why evidence is weak or strong",
        }
    return {
        "include": "papers with direct evidence for this subquestion",
        "exclude": "papers only weakly matching the query terms",
        "evidence": "problem, method/data, evidence, limitations, and relation to the subquestion",
        "reference": "prefer references that are central to the subquestion and cited in a meaningful context",
    }


def subquestion_reading_lens(query_family: str) -> list[str]:
    family = (query_family or "").lower()
    if "definition" in family or "landscape" in family:
        return [
            "extract terminology, definitions, field boundaries, and competing taxonomies",
            "identify representative papers, seed reviews, and field-framing resources",
            "record which terms should become later simple keyword queries",
        ]
    if "data" in family or "resource" in family:
        return [
            "extract databases, datasets, benchmarks, corpora, tools, and reusable resources",
            "record schema, entity types, relation types, data sources, coverage, access, and update status",
            "turn high-value resource names into seed-driven iteration queries or explicit blockers",
        ]
    if "method" in family or "model" in family:
        return [
            "extract construction workflows, extraction/fusion/modeling steps, algorithms, and pipelines",
            "record inputs, outputs, assumptions, implementation clues, baselines, and reproducibility limits",
            "turn method/model names into seed-driven iteration queries or explicit blockers",
        ]
    if "evaluation" in family or "benchmark" in family:
        return [
            "extract metrics, validation design, benchmarks, baselines, comparisons, and ablations",
            "record negative findings, failure cases, threats to validity, and missing evaluation evidence",
            "turn benchmark or metric names into seed-driven iteration queries or explicit blockers",
        ]
    if "application" in family or "case" in family:
        return [
            "extract use cases, tasks, deployment contexts, users, constraints, and demonstrated effects",
            "record which applications are evidenced by data versus speculative discussion",
            "turn application/task names into seed-driven iteration queries or explicit blockers",
        ]
    if "limitation" in family or "gap" in family:
        return [
            "extract unresolved gaps, contradictions, missing data, missing methods, and risks",
            "record what evidence would close each gap and which terms should be searched next",
            "turn gap terms into seed-driven iteration queries or explicit blockers",
        ]
    return [
        "extract evidence directly tied to this subquestion, not a generic paper summary",
        "record reusable named resources, methods, datasets, metrics, references, and gaps",
        "turn high-value seeds into later simple keyword queries or explicit blockers",
    ]


def source_role_for_depth(capture_depth: int) -> str:
    return "reference" if int(capture_depth or 1) >= 2 else "primary"


def source_role_for_args(args: argparse.Namespace) -> str:
    return source_role_for_depth(int(args.capture_depth))


def article_parent_dir(output_root: Path, publisher: str, job: dict[str, Any], args: argparse.Namespace) -> Path:
    plan = job.get("plan")
    role_root = "references" if source_role_for_args(args) == "reference" else "sources"
    return subquestion_dir(output_root, plan) / role_root / source_bucket_for(publisher) / "articles"


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def enforce_capture_gate(args: argparse.Namespace) -> None:
    if args.discovery_only or getattr(args, "allow_first_n_capture", False):
        return
    if args.existing_run_dir or int(args.capture_depth) >= 2:
        return
    raise SystemExit(
        "Direct first-N capture is disabled for new root runs. Run --discovery-only "
        "--write-query-refinement-packets first, have the search subagent write "
        "query-refinement-recommendations.json, then capture via capture_decision_queue.py. "
        "Use --allow-first-n-capture only for intentional bounded debugging."
    )


def enforce_publisher_browser_config(args: argparse.Namespace) -> None:
    return None


def should_enforce_publisher_browser_config(args: argparse.Namespace) -> bool:
    return False


def validate_discovery_backend_gate(args: argparse.Namespace) -> None:
    backend = getattr(args, "discovery_backend", "opencli")
    if backend not in DISCOVERY_BACKENDS:
        raise SystemExit("Discovery backend must be opencli.")
    if not getattr(args, "approved_query_plan", None):
        if getattr(args, "search_url", None):
            return
        raise SystemExit("OpenCLI discovery requires --approved-query-plan or --search-url.")
    if not getattr(args, "discovery_only", False):
        raise SystemExit("OpenCLI discovery is discovery-only. Use capture_decision_queue.py after agent approval.")


def enforce_visible_publisher(args: argparse.Namespace) -> None:
    return None


def validate_approved_query_plan(args: argparse.Namespace) -> None:
    if args.existing_run_dir or int(args.capture_depth) >= 2:
        return
    plan_path = getattr(args, "approved_query_plan", None)
    if not plan_path:
        raise SystemExit(
            "New root discovery requires --approved-query-plan pointing to the user-approved "
            "query-plan-preview.json built from an agent-authored query plan."
        )
    try:
        payload = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"Could not read approved query plan: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("Approved query plan must be a JSON object.")
    if clean(payload.get("english_big_question")) != clean(args.claim):
        raise SystemExit("Approved query plan question does not match --claim.")
    grounding = clean(payload.get("grounding_notes"))
    if not grounding or "AGENT REQUIRED" in grounding:
        raise SystemExit("Approved query plan must include non-placeholder grounding notes.")
    sources = payload.get("exploration_sources") or []
    if not isinstance(sources, list):
        raise SystemExit("Approved query plan must include exploration_sources.")
    source_text = " ".join(
        " ".join(str(source.get(key) or "") for key in ["label", "url", "note"])
        for source in sources if isinstance(source, dict)
    ).lower()
    if "openalex" not in source_text or not any(term in source_text for term in ["metadata", "grounding"]):
        raise SystemExit("Approved query plan must record OpenAlex metadata grounding.")
    openalex_audit = payload.get("openalex_grounding")
    if not isinstance(openalex_audit, dict):
        raise SystemExit("Approved query plan must include OpenAlex grounding audit.")
    if not openalex_audit.get("api_key_present"):
        raise SystemExit("Approved query plan OpenAlex audit must record api_key_present=true.")
    if openalex_audit.get("status") != "ok" or not openalex_audit.get("terms"):
        raise SystemExit("Approved query plan OpenAlex audit must include extracted metadata terms.")
    if not openalex_audit.get("works"):
        raise SystemExit("Approved query plan OpenAlex audit must include work-level metadata.")
    subquestions = payload.get("subquestions") or []
    if not isinstance(subquestions, list) or not subquestions:
        raise SystemExit("Approved query plan must include subquestions.")
    for subquestion in subquestions:
        if not isinstance(subquestion, dict):
            raise SystemExit("Approved query plan subquestions must be JSON objects.")
        for query in subquestion.get("queries") or []:
            if not discovery.is_simple_publisher_query(str(query)):
                raise SystemExit(f"Approved query plan contains non-simple publisher query: {query}")


def enforce_subquestion_append_gate(args: argparse.Namespace) -> None:
    if getattr(args, "subquestion_id", "") and not getattr(args, "existing_run_dir", None):
        raise SystemExit(
            "--subquestion-id is an append/iteration control and requires --existing-run-dir. "
            "Use --approved-query-plan for new root discovery with multiple subquestions."
        )


def approved_query_plan_jobs(
    plan_path: Path,
    max_queries: int,
    year_start: int,
    year_end: int,
    sciencedirect_route: str,
    sciencedirect_article_types: str,
    include_springer: bool,
) -> tuple[list[dict[str, Any]], list[discovery.QueryPlan]]:
    payload = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    plans: list[discovery.QueryPlan] = []
    jobs: list[dict[str, Any]] = []
    for index, item in enumerate(payload.get("subquestions") or [], start=1):
        if not isinstance(item, dict):
            continue
        query_mode = clean(item.get("query_mode"))
        if query_mode and query_mode != "broad_discovery":
            continue
        publisher_routes = {
            clean(item.get("publisher_route")).lower(),
            *[
                clean(target.get("key")).lower()
                for target in (item.get("publisher_targets") or [])
                if isinstance(target, dict)
            ],
        }
        publisher_routes = {route for route in publisher_routes if route}
        queries = [
            clean(query)
            for query in item.get("queries") or []
            if clean(query)
        ][: max_queries or None]
        plan = discovery.QueryPlan(
            int(item.get("round") or index),
            clean(item.get("query_family")) or clean(item.get("subquestion_slug")) or f"subquestion-{index:02d}",
            clean(item.get("subquestion_text") or item.get("claim_subquestion")) or "Approved subquestion.",
            queries,
            subquestion_id=clean(item.get("subquestion_id") or item.get("id")),
            subquestion_slug=clean(item.get("subquestion_slug")),
            group_slug=clean(item.get("subquestion_group_slug")) or "approved",
            group_title=clean(item.get("subquestion_group_title")) or "Approved",
            concept_groups=item.get("concept_groups") if isinstance(item.get("concept_groups"), list) else [],
            boolean_query=clean(item.get("boolean_query")),
            publisher_queries=item.get("publisher_queries") if isinstance(item.get("publisher_queries"), dict) else {},
        )
        discovery.assign_subquestion_ids([plan])
        plans.append(plan)
        for query in queries:
            for url in discovery.search_urls_for_query(
                query,
                year_start,
                year_end,
                sciencedirect_route,
                sciencedirect_article_types,
                include_springer,
            ):
                if publisher_routes and discovery.infer_publisher(url) not in publisher_routes:
                    continue
                jobs.append({"url": url, "query": query, "plan": plan})
        if not queries:
            query = clean(plan.boolean_query) or plan.query_family
            for _publisher, url in (plan.publisher_queries or {}).items():
                if clean(url):
                    jobs.append({"url": clean(url), "query": query, "plan": plan})
            if not plan.publisher_queries and publisher_routes and clean(query):
                for url in discovery.search_urls_for_query(
                    query,
                    year_start,
                    year_end,
                    sciencedirect_route,
                    sciencedirect_article_types,
                    include_springer,
                ):
                    if discovery.infer_publisher(url) in publisher_routes:
                        jobs.append({"url": url, "query": query, "plan": plan})
    return jobs, plans


def article_result_key(result: dict[str, str], publisher: str) -> str:
    href = result.get("href") or ""
    low = href.lower()
    if publisher == "elsevier":
        match = re.search(r"/science/article/pii/([^/?#]+)", low)
        if match:
            return f"elsevier:{match.group(1)}"
    if publisher in {"acs", "wiley"}:
        match = re.search(r"/doi/(?:abs/|full/|epdf/|pdf/)?(10\.\d{4,9}/[^?#]+)", low)
        if match:
            return f"{publisher}:{match.group(1).rstrip('/')}"
    if publisher == "springer":
        match = re.search(r"/(?:article|chapter|protocol)/(10\.\d{4,9}/[^?#]+)", low)
        if match:
            return f"springer:{match.group(1).rstrip('/')}"
    title = clean(result.get("title") or "").lower()
    return f"{publisher}:{title or low}"


def result_year(result: dict[str, str]) -> int | None:
    year = result.get("year") or discovery.extract_year(
        " ".join([
            result.get("title") or "",
            result.get("context") or "",
            result.get("href") or "",
        ])
    )
    if re.fullmatch(r"\d{4}", str(year or "")):
        return int(str(year))
    return None


def year_in_range(year: int | str | None, year_start: int, year_end: int) -> bool:
    if year is None or year == "":
        return True
    try:
        value = int(str(year))
    except Exception:
        return True
    return int(year_start) <= value <= int(year_end)


def select_page_articles(
    results: list[dict[str, str]],
    publisher: str,
    limit: int,
    year_start: int | None = None,
    year_end: int | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    candidates: list[dict[str, str]] = []
    audit: list[dict[str, Any]] = []
    index_by_key: dict[str, int] = {}
    weak_titles = {"abstract", "full text", "html", "pdf", "article"}

    for raw_rank, raw in enumerate(results, start=1):
        result = dict(raw)
        key = article_result_key(result, publisher)
        title = clean(result.get("title") or "")
        href = result.get("href") or ""
        context = result.get("context") or ""
        abstract = clean(result.get("abstract") or "")
        abstract_source = clean(result.get("abstract_source") or "")
        detected_year = result_year(result)
        if year_start is not None and year_end is not None and detected_year is not None and not year_in_range(detected_year, year_start, year_end):
            audit.append({
                "stage": "year-filtered",
                "raw_rank": raw_rank,
                "unique_rank": "",
                "selected": False,
                "dedupe_key": key,
                "title": title,
                "href": href,
                "doi": result.get("doi") or "",
                "duplicate_of_raw_rank": "",
                "duplicate_status": "",
                "duplicate_of": "",
                "screening_priority": "",
                "context": context[:500],
                "abstract": abstract[:1500],
                "abstract_source": abstract_source,
                "year": detected_year,
            })
            continue
        if key in index_by_key:
            existing_index = index_by_key[key]
            existing = candidates[existing_index]
            existing_title = clean(existing.get("title") or "")
            if existing_title.lower() in weak_titles and title.lower() not in weak_titles:
                existing["title"] = title
            duplicate_ranks = existing.setdefault("_duplicate_raw_ranks", [])
            if isinstance(duplicate_ranks, list):
                duplicate_ranks.append(raw_rank)
            audit.append({
                "stage": "duplicate",
                "raw_rank": raw_rank,
                "unique_rank": existing_index + 1,
                "selected": False,
                "dedupe_key": key,
                "title": title,
                "href": href,
                "doi": result.get("doi") or "",
                "duplicate_of_raw_rank": existing.get("_raw_rank", ""),
                "duplicate_status": "",
                "duplicate_of": "",
                "screening_priority": "",
                "context": context[:500],
                "abstract": abstract[:1500],
                "abstract_source": abstract_source,
            })
            continue
        result["_dedupe_key"] = key
        result["_raw_rank"] = raw_rank
        result["_duplicate_raw_ranks"] = []
        index_by_key[key] = len(candidates)
        candidates.append(result)

    selected_limit = limit if limit and limit > 0 else len(candidates)
    for unique_rank, result in enumerate(candidates, start=1):
        selected = unique_rank <= selected_limit
        result["_page_rank"] = unique_rank
        result["_selected"] = selected
        audit.append({
            "stage": "candidate",
            "raw_rank": result.get("_raw_rank", ""),
            "unique_rank": unique_rank,
            "selected": selected,
            "dedupe_key": result.get("_dedupe_key", ""),
            "title": clean(result.get("title") or ""),
            "href": result.get("href") or "",
            "doi": result.get("doi") or "",
            "duplicate_of_raw_rank": "",
            "duplicate_status": "",
            "duplicate_of": "",
            "screening_priority": "",
            "context": (result.get("context") or "")[:500],
            "abstract": clean(result.get("abstract") or "")[:1500],
            "abstract_source": clean(result.get("abstract_source") or ""),
        })
    return candidates[:selected_limit], audit


def publisher_blocker_audit_row(
    job: dict[str, Any],
    *,
    publisher: str,
    page_num: int,
    page_url: str,
    reason: str,
) -> dict[str, Any]:
    plan = job.get("plan")
    return {
        "subquestion_id": plan_subquestion_id(plan),
        "subquestion_group_slug": plan_group_slug(plan),
        "subquestion_group_title": plan_group_title(plan),
        "subquestion_slug": plan_subquestion_slug(plan),
        "subquestion_text": getattr(plan, "claim_subquestion", ""),
        "query_round": getattr(plan, "round_index", ""),
        "query_family": getattr(plan, "query_family", "manual-search-url"),
        "query_text": job.get("query") or "",
        "publisher": publisher,
        "source_bucket": source_bucket_for(publisher),
        "page": page_num,
        "page_url": page_url,
        "stage": "publisher-blocker",
        "status": "blocked",
        "blocker_reason": reason,
        "raw_rank": "",
        "unique_rank": "",
        "selected": False,
        "dedupe_key": "",
        "duplicate_of_raw_rank": "",
        "title": "",
        "href": "",
        "context": "",
        "abstract": "",
        "abstract_source": "",
    }


async def wait_for_load_best_effort(page, timeout_ms: int) -> None:
    try:
        await page.wait_for_load_state("load", timeout=timeout_ms)
    except Exception:
        pass


TRANSIENT_CAPTURE_ERROR_PATTERNS = (
    "execution context was destroyed",
    "most likely because of a navigation",
    "page was closed",
    "target closed",
)


def is_transient_capture_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(pattern in message for pattern in TRANSIENT_CAPTURE_ERROR_PATTERNS)


async def evaluate_with_transient_retry(page, script: str, *args, retries: int = 2, settle_ms: int = 1000):
    for attempt in range(retries + 1):
        try:
            return await page.evaluate(script, *args)
        except Exception as exc:
            if attempt >= retries or not is_transient_capture_error(exc):
                raise
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=settle_ms)
            except Exception:
                pass
            await page.wait_for_timeout(settle_ms)
    raise RuntimeError("unreachable evaluate retry state")


async def extract_page_with_retry(page, retries: int = 2, settle_ms: int = 1000) -> dict[str, Any]:
    for attempt in range(retries + 1):
        try:
            return await capture.extract_page(page)
        except Exception as exc:
            if attempt >= retries or not is_transient_capture_error(exc):
                raise
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=settle_ms)
            except Exception:
                pass
            await page.wait_for_timeout(settle_ms)
    raise RuntimeError("unreachable capture retry state")


def elsevier_problem_page_reason(data: dict[str, Any]) -> str:
    title = clean(data.get("title") or data.get("documentTitle") or "")
    fulltext = clean(data.get("fullText") or data.get("body") or "")
    haystack = f"{title} {fulltext}"
    if "there was a problem providing the content you requested" not in haystack.lower():
        return ""
    match = re.search(r"Reference\s+number:\s*([A-Za-z0-9_-]+)", haystack, flags=re.IGNORECASE)
    if match:
        return f"elsevier_problem_page:{match.group(1)}"
    return "elsevier_problem_page"


def publisher_problem_from_state(state: dict[str, Any], page_url: str) -> str:
    current_url = str(state.get("url") or "")
    title = str(state.get("title") or "")
    text = str(state.get("text") or "")
    anchors = int(state.get("anchors") or 0)
    haystack = f"{current_url} {title} {text}".lower()
    if any(term in haystack for term in ["are you a robot", "captcha", "cloudflare", "turnstile", "请验证", "真人"]):
        return "blocked_robot"
    if current_url in {"about:blank", ""} or current_url.startswith(("chrome://newtab", "edge://newtab")):
        return "publisher_blank_page"
    allowed_direct = [
        "sciencedirect.com/search",
        "sciencedirect.com/science/article",
        "pubs.acs.org/action/dosearch",
        "pubs.acs.org/doi/",
        "onlinelibrary.wiley.com/action/dosearch",
        "onlinelibrary.wiley.com/doi/",
        "link.springer.com/search",
        "link.springer.com/article/",
        "link.springer.com/chapter/",
        "link.springer.com/protocol/",
    ]
    if not any(token in current_url.lower() for token in allowed_direct):
        return f"publisher_page_unavailable:{current_url[:220]}"
    auth_terms = [
        "authentication",
        "sign in", "login", "log in", "session expired", "please authenticate",
    ]
    if any(term in haystack for term in auth_terms) and anchors < 8:
        return "publisher_auth_required"
    if len(text) < 20 and anchors == 0:
        return "publisher_blank_or_unloaded_page"
    return ""


def is_publisher_auth_blocker(reason: str) -> bool:
    return reason == "publisher_auth_required" or reason.startswith("publisher_page_unavailable:")


OPENCLI_EXTRACT_SCRIPT_TEMPLATE = r"""(() => {
  const searchUrl = __SEARCH_URL__;
  const publisher = __PUBLISHER__;
  const out = [];
  const seen = new Set();
  const abs = href => {
    try { return new URL(href, location.href).toString(); } catch { return ""; }
  };
  const clean = text => String(text || '').replace(/\s+/g, ' ').trim();
  const abstractSource = () => {
    if (publisher === 'elsevier' || publisher === 'sciencedirect') return 'sciencedirect_search_result_abstract';
    if (publisher === 'acs') return 'acs_search_result_abstract';
    if (publisher === 'wiley') return 'wiley_search_result_abstract';
    if (publisher === 'springer') return 'springer_search_result_snippet';
    return 'publisher_search_page';
  };
  const articleAllowed = href => {
    const low = href.toLowerCase();
    if (['/pdf', 'pdfft', '/epdf', 'download', 'pdfdownload', 'viewpdf'].some(token => low.includes(token))) return false;
    const path = (() => { try { return new URL(href).pathname.toLowerCase(); } catch { return ''; } })();
    if (publisher === 'elsevier') return /\/science\/article\/pii\/[^/]+\/?$/.test(path) && low.includes('sciencedirect.com');
    if (publisher === 'acs') return low.includes('/doi/') && !low.includes('/doi/abs/') && !low.includes('/doi/full/') && !low.includes('/action/') && !low.includes('/toc/');
    if (publisher === 'wiley') return low.includes('/doi/') && !low.includes('/doi/book/') && !low.includes('/action/') && !low.includes('/toc/');
    if (publisher === 'springer') return (low.includes('/article/10.') || low.includes('/chapter/10.') || low.includes('/protocol/10.')) && !low.includes('/search');
    return false;
  };
  const titleFromBox = (a, box) => {
    const candidates = [
      a.innerText,
      a.getAttribute('title'),
      box.querySelector('h1,h2,h3,h4,[data-test=title],[class*="title" i]')?.innerText,
    ].map(clean).filter(Boolean);
    return candidates.sort((a, b) => b.length - a.length)[0] || '';
  };
  const abstractFromBox = (box, title, boxText) => {
    const candidates = [];
    const selectors = [
      '[class*="abstract" i]',
      '[id*="abstract" i]',
      '[data-testid*="abstract" i]',
      '[data-test=description]',
      '[aria-label*="abstract" i]',
      'section',
      'p',
      'div'
    ];
    const push = value => {
      const text = clean(value)
        .replace(/^Abstract\s*/i, '')
        .replace(/^Graphical Abstract\s*/i, '');
      if (text.length < 60) return;
      const low = text.toLowerCase();
      const titleLow = clean(title).toLowerCase();
      if (titleLow && low === titleLow) return;
      if (/^(view pdf|download|export|figures?|extracts?|graphical abstract|abstract)$/i.test(text)) return;
      if (/\b(view pdf|download selected|set search alert|cookie settings)\b/i.test(text)) return;
      candidates.push(text);
    };
    for (const selector of selectors) {
      let nodes = [];
      try { nodes = Array.from(box.querySelectorAll(selector)); } catch { nodes = []; }
      for (const node of nodes.slice(0, 80)) {
        push(node.innerText || node.textContent || '');
      }
    }
    if (!candidates.length) {
      const match = clean(boxText).match(/(?:^|\b)Abstract\s*(?:Graphical Abstract\s*)?(?:Extracts\s*)?(?:Figures\s*)?(?:Export\s*)?(.{80,2500})/i);
      if (match) push(match[1]);
    }
    candidates.sort((a, b) => b.length - a.length);
    return (candidates[0] || '').slice(0, 2500);
  };
  for (const a of Array.from(document.querySelectorAll('a[href]'))) {
    const href = abs(a.getAttribute('href') || '');
    if (!articleAllowed(href) || seen.has(href)) continue;
    const box = a.closest('li, article, .result, .search-result, .issue-item, .card, .publication, .item, [data-test=search-result-item]') || a.parentElement || a;
    const boxText = clean(box.innerText || '');
    const title = titleFromBox(a, box);
    if (!title || title.length < 8) continue;
    if (/\b(pdf|download)\b/i.test(title)) continue;
    const abstract = abstractFromBox(box, title, boxText);
    const published = clean(
      box.querySelector('[data-test=published], time, [class*="date" i], [class*="published" i]')?.innerText ||
      box.querySelector('[data-test=published], time, [class*="date" i], [class*="published" i]')?.getAttribute('datetime') ||
      ''
    );
    const yearMatch = (published || boxText).match(/\b(20[12]\d)\b/);
    const venue = clean(box.querySelector('[data-test=parent], [class*="journal" i], [class*="publication" i]')?.innerText || '');
    seen.add(href);
    out.push({
      title,
      href,
      context: boxText.slice(0, 1400),
      abstract,
      abstract_source: abstract ? abstractSource() : '',
      year: yearMatch ? yearMatch[1] : '',
      journal: venue,
      searchUrl
    });
  }
  return {
    state: {
      url: location.href,
      title: document.title,
      text: clean(document.body?.innerText || '').slice(0, 5000),
      anchors: document.querySelectorAll('a[href]').length
    },
    results: out
  };
})()"""


def run_opencli_command(parts: list[str], *, timeout: int = 90) -> str:
    return opencli_browser.run_opencli_command(parts, timeout=timeout)


def opencli_extract_search_page(session: str, page_url: str, search_url: str, publisher: str, wait_s: int) -> dict[str, Any]:
    opencli_browser.open_url(session, page_url, timeout=90)
    opencli_browser.wait_time(session, wait_s)
    script = OPENCLI_EXTRACT_SCRIPT_TEMPLATE.replace("__SEARCH_URL__", json.dumps(search_url)).replace("__PUBLISHER__", json.dumps(publisher))
    data = opencli_browser.eval_json(session, script, timeout=90)
    if not isinstance(data, dict):
        raise RuntimeError("opencli_invalid_payload")
    return data


async def run_opencli_discovery(
    args: argparse.Namespace,
    output_root: Path,
    search_jobs: list[dict[str, Any]],
    discovery_rows: list[dict[str, Any]],
    previous_rows: list[dict[str, Any]],
) -> None:
    session = clean(getattr(args, "opencli_session", "") or "lit")
    wait_s = max(0, int(round(float(getattr(args, "settle_ms", 3000) or 3000) / 1000)))
    try:
        if not getattr(args, "skip_publisher_preflight", False):
            home_url = clean(getattr(args, "publisher_home_url", "")) or opencli_browser.PUBLISHER_HOME_URL
            append_log(output_root, {"event": "opencli-publisher-preflight-started", "session": session, "url": home_url})
            snapshot, problem = await asyncio.to_thread(
                opencli_browser.preflight_publisher_home,
                session,
                home_url=home_url,
                expected_hosts={"sciencedirect.com"},
                wait_ms=int(getattr(args, "publisher_auth_wait_ms", 3000) or 3000),
                timeout=90,
            )
            if problem:
                append_log(output_root, {"event": "opencli-publisher-preflight-blocked", "session": session, "problem": problem, "url": snapshot.get("url")})
                raise SystemExit(
                    "OpenCLI publisher authentication is required before discovery. "
                    f"Problem: {problem}. Log in through the connected Chrome/OpenCLI window at "
                    f"{home_url}, then rerun discovery."
                )
            append_log(output_root, {"event": "opencli-publisher-preflight-ok", "session": session, "url": snapshot.get("url")})
        for job in search_jobs:
            search_url = job["url"]
            discovery.ensure_allowed_search_url(search_url)
            publisher = discovery.infer_publisher(search_url)
            for page_num in range(1, args.max_pages + 1):
                page_url = discovery.page_url_for(search_url, publisher, page_num)
                append_log(output_root, {"event": "opencli-search-page-started", "publisher": publisher, "page_url": page_url, "session": session})
                try:
                    payload = await asyncio.to_thread(opencli_extract_search_page, session, page_url, search_url, publisher, wait_s)
                    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
                    problem = publisher_problem_from_state(state, page_url)
                    if problem:
                        discovery_rows.append(publisher_blocker_audit_row(
                            job,
                            publisher=publisher,
                            page_num=page_num,
                            page_url=page_url,
                            reason=problem,
                        ))
                        write_discovery_outputs(output_root, discovery_rows)
                        append_log(output_root, {
                            "event": "opencli-search-page-blocked",
                            "publisher": publisher,
                            "page_url": page_url,
                            "reason": problem,
                            "final_url": state.get("url", ""),
                        })
                        break
                    raw_results = payload.get("results") if isinstance(payload.get("results"), list) else []
                except Exception as exc:
                    reason = f"{type(exc).__name__}: {str(exc)[:500]}"
                    discovery_rows.append(publisher_blocker_audit_row(
                        job,
                        publisher=publisher,
                        page_num=page_num,
                        page_url=page_url,
                        reason=reason,
                    ))
                    write_discovery_outputs(output_root, discovery_rows)
                    append_log(output_root, {
                        "event": "opencli-search-page-error",
                        "publisher": publisher,
                        "page_url": page_url,
                        "reason": reason,
                    })
                    break
                selected, page_audit = select_page_articles(raw_results, publisher, args.max_results_per_page, args.year_start, args.year_end)
                dedup_history = previous_rows + discovery_rows
                selected = discovery.mark_duplicate_candidates(selected, dedup_history)
                marked_candidates = iter(discovery.mark_duplicate_candidates(
                    [row for row in page_audit if row.get("stage") == "candidate"],
                    dedup_history,
                ))
                plan = job.get("plan")
                for audit_row in page_audit:
                    if audit_row.get("stage") == "candidate":
                        audit_row.update(next(marked_candidates))
                    audit_row.update({
                        "subquestion_id": plan_subquestion_id(plan),
                        "subquestion_group_slug": plan_group_slug(plan),
                        "subquestion_group_title": plan_group_title(plan),
                        "subquestion_slug": plan_subquestion_slug(plan),
                        "subquestion_text": getattr(plan, "claim_subquestion", ""),
                        "query_round": getattr(plan, "round_index", ""),
                        "query_family": getattr(plan, "query_family", "manual-search-url"),
                        "query_text": job.get("query") or "",
                        "publisher": publisher,
                        "source_bucket": source_bucket_for(publisher),
                        "page": page_num,
                        "page_url": page_url,
                    })
                discovery_rows.extend(page_audit)
                write_discovery_outputs(output_root, discovery_rows)
                append_log(output_root, {
                    "event": "opencli-search-page-discovered",
                    "publisher": publisher,
                    "page": page_num,
                    "raw_results": len(raw_results),
                    "selected": len(selected),
                })
    finally:
        try:
            await asyncio.to_thread(opencli_browser.close, session, timeout=15)
        except Exception:
            pass


async def publisher_page_state(page) -> dict[str, Any]:
    return await page.evaluate(
        """() => {
          const text = (document.body && document.body.innerText || '').replace(/\\s+/g, ' ').trim();
          return {
            url: location.href,
            title: document.title || '',
            text: text.slice(0, 1200),
            anchors: document.querySelectorAll('a[href]').length
          };
        }"""
    )


async def publisher_page_problem(page, page_url: str) -> str:
    return publisher_problem_from_state(await publisher_page_state(page), page_url)


async def page_scroll_signature(page) -> dict[str, Any]:
    return await evaluate_with_transient_retry(
        page,
        """() => {
          const root = document.scrollingElement || document.documentElement || document.body;
          const q = sel => document.querySelectorAll(sel).length;
          return {
            y: Math.round(window.scrollY || root.scrollTop || 0),
            height: Math.round(root.scrollHeight || document.body.scrollHeight || 0),
            viewport: Math.round(window.innerHeight || root.clientHeight || 0),
            sections: q('section, article section, h2, h3'),
            figures: q('figure, .figure, [class*="figure" i]'),
            tables: q('table, .article-table-content, .c-article-table, .Table'),
            images: q('img[src], picture source[srcset]')
          };
        }"""
    )


def scroll_signature_key(sig: dict[str, Any]) -> tuple[Any, ...]:
    at_bottom = sig.get("y", 0) + sig.get("viewport", 0) >= sig.get("height", 0) - 12
    return (
        sig.get("height", 0),
        at_bottom,
        sig.get("sections", 0),
        sig.get("figures", 0),
        sig.get("tables", 0),
        sig.get("images", 0),
    )


async def smart_scroll_page(page, args: argparse.Namespace) -> dict[str, Any]:
    await wait_for_load_best_effort(page, args.load_timeout_ms)
    await page.wait_for_timeout(args.article_settle_ms)
    stable_seen = 0
    previous_key: tuple[Any, ...] | None = None
    last_sig: dict[str, Any] = {}
    rounds = 0
    while rounds < args.scroll_rounds:
        rounds += 1
        sig = await page_scroll_signature(page)
        step = max(args.smart_scroll_step, int((sig.get("viewport") or 900) * 1.8))
        await evaluate_with_transient_retry(
            page,
            """step => {
              const root = document.scrollingElement || document.documentElement || document.body;
              root.scrollTo({ top: (window.scrollY || root.scrollTop || 0) + step, behavior: 'auto' });
            }""",
            step,
            settle_ms=args.scroll_wait_ms,
        )
        await page.wait_for_timeout(args.scroll_wait_ms)
        sig = await page_scroll_signature(page)
        key = scroll_signature_key(sig)
        if key == previous_key:
            stable_seen += 1
        else:
            stable_seen = 0
            previous_key = key
        last_sig = sig
        if rounds >= args.smart_scroll_min_rounds and bool(key[1]) and stable_seen >= args.smart_scroll_stable_rounds:
            break
    await page.wait_for_timeout(args.article_final_settle_ms)
    last_sig["rounds"] = rounds
    last_sig["stable_seen"] = stable_seen
    return last_sig


def write_discovery_outputs(output_root: Path, discovery_rows: list[dict[str, Any]]) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "discovery-audit.json").write_text(json.dumps(discovery_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = [
        "subquestion_group_slug", "subquestion_group_title",
        "subquestion_id", "subquestion_slug", "subquestion_text",
        "query_round", "query_family", "query_text", "publisher", "source_bucket", "page", "stage",
        "status", "blocker_reason",
        "raw_rank", "unique_rank", "selected", "dedupe_key", "duplicate_of_raw_rank",
        "duplicate_status", "duplicate_of", "screening_priority",
        "title", "href", "doi", "context", "abstract", "abstract_source", "page_url",
    ]
    with (output_root / "discovery-audit.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(discovery_rows)


def load_json_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def load_csv_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except Exception:
        return []


def history_rows(rows: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [{**row, "_history_kind": kind} for row in rows]


def summary_history_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        kind = "captured" if clean(row.get("status")).lower() == "captured" else "seen"
        out.append({**row, "_history_kind": kind})
    return out


def load_existing_dedup_rows(output_root: Path, summary_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary = list(summary_rows)
    if not summary:
        summary.extend(load_json_rows(output_root / "run-summary.json"))
    if not summary:
        summary.extend(load_csv_rows(output_root / "run-summary.csv"))
    discovery_audit = load_json_rows(output_root / "discovery-audit.json") or load_csv_rows(output_root / "discovery-audit.csv")
    return summary_history_rows(summary) + history_rows(discovery_audit, "seen")


def write_run_outputs(output_root: Path, summary_rows: list[dict[str, Any]]) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "run-summary.json").write_text(json.dumps(summary_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output_root / "run-summary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RUN_SUMMARY_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(summary_rows)
    lines = [
        "# publisher-authenticated Literature Capture Summary",
        "",
        f"- Updated: {datetime.now().isoformat(timespec='seconds')}",
        f"- Captured: {sum(1 for row in summary_rows if row.get('status') == 'captured')}",
        f"- Skipped: {sum(1 for row in summary_rows if row.get('status') == 'skipped')}",
        f"- Errors: {sum(1 for row in summary_rows if row.get('status') == 'error')}",
        "",
        "| Status | Note | Publisher | Rank | Year | Title | DOI | Figures | Tables |",
        "|---|---|---|---:|---:|---|---|---:|---:|",
    ]
    for row in summary_rows:
        title = str(row.get("title") or "").replace("|", "\\|")[:120]
        lines.append(
            f"| {row.get('status', '')} | {row.get('note_status', '')} | {row.get('publisher', '')} | {row.get('discovery_rank', '')} | {row.get('year', '')} | {title} | {row.get('doi', '')} | {row.get('figure_count', '')} | {row.get('table_count', '')} |"
        )
    (output_root / "run-summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_reading_notes_indexes(output_root, summary_rows)


def write_question_artifacts(output_root: Path, args: argparse.Namespace, plans: list[Any]) -> None:
    existing_payload: dict[str, Any] = {}
    question_path = output_root / "question.json"
    if args.existing_run_dir and question_path.exists():
        try:
            loaded = json.loads(question_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing_payload = loaded
        except Exception:
            existing_payload = {}
    new_subquestions = [
        {
            "subquestion_id": plan_subquestion_id(plan),
            "subquestion_slug": plan_subquestion_slug(plan),
            "subquestion_group_slug": plan_group_slug(plan),
            "subquestion_group_title": plan_group_title(plan),
            "round": getattr(plan, "round_index", ""),
            "query_family": getattr(plan, "query_family", ""),
            "subquestion_text": getattr(plan, "claim_subquestion", ""),
            "concept_groups": plan_concept_groups(plan),
            "boolean_query": getattr(plan, "boolean_query", ""),
            "publisher_queries": getattr(plan, "publisher_queries", None) or {},
            "queries": getattr(plan, "queries", []),
        }
        for plan in plans
    ]
    if existing_payload:
        existing_subquestions = existing_payload.get("subquestions") or []
        if not isinstance(existing_subquestions, list):
            existing_subquestions = []
        seen = {
            json.dumps(
                {
                    "subquestion_id": item.get("subquestion_id") if isinstance(item, dict) else "",
                    "query_family": item.get("query_family") if isinstance(item, dict) else "",
                    "queries": item.get("queries") if isinstance(item, dict) else [],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            for item in existing_subquestions
        }
        for subquestion in new_subquestions:
            key = json.dumps(
                {
                    "subquestion_id": subquestion.get("subquestion_id") or "",
                    "query_family": subquestion.get("query_family") or "",
                    "queries": subquestion.get("queries") or [],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            if key not in seen:
                existing_subquestions.append(subquestion)
        payload = {
            **existing_payload,
            "question": existing_payload.get("question") or args.claim,
            "review_context": existing_payload.get("review_context") or args.review_context or "",
            "approved_query_plan": str(args.approved_query_plan) if args.approved_query_plan else existing_payload.get("approved_query_plan", ""),
            "discovery_backend": getattr(args, "discovery_backend", existing_payload.get("discovery_backend") or ""),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "subquestions": existing_subquestions,
        }
    else:
        payload = {
            "question": args.claim,
            "review_context": args.review_context or "",
            "capture_depth": int(args.capture_depth),
            "approved_query_plan": str(args.approved_query_plan) if args.approved_query_plan else "",
            "discovery_backend": getattr(args, "discovery_backend", ""),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "subquestions": new_subquestions,
        }
    (output_root / "question.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for plan in plans:
        folder = subquestion_dir(output_root, plan)
        folder.mkdir(parents=True, exist_ok=True)
        subquestion = {
            "subquestion_id": plan_subquestion_id(plan),
            "subquestion_slug": plan_subquestion_slug(plan),
            "subquestion_group_slug": plan_group_slug(plan),
            "subquestion_group_title": plan_group_title(plan),
            "round": getattr(plan, "round_index", ""),
            "query_family": getattr(plan, "query_family", ""),
            "subquestion_text": getattr(plan, "claim_subquestion", ""),
            "concept_groups": plan_concept_groups(plan),
            "boolean_query": getattr(plan, "boolean_query", ""),
            "publisher_queries": getattr(plan, "publisher_queries", None) or {},
            "queries": getattr(plan, "queries", []),
            "agent_owner": args.agent_owner or "",
            "capture_depth": int(args.capture_depth),
            "source_role": source_role_for_args(args),
            "parent_article_dir": args.parent_article_dir or "",
            "parent_reference_index": args.parent_reference_index or "",
        }
        (folder / "subquestion.json").write_text(json.dumps(subquestion, ensure_ascii=False, indent=2), encoding="utf-8")
        if not (folder / "subquestion-summary-zh.md").exists():
            (folder / "subquestion-summary-zh.md").write_text(
                "# Subquestion summary requires agent\n\n"
                "Agent/subagent should write this after reading all article folders in this subquestion.\n",
                encoding="utf-8",
            )
        guidance = evidence_guidance(getattr(plan, "query_family", ""))
        reading_lens = subquestion_reading_lens(getattr(plan, "query_family", ""))
        brief = [
            "# Subagent Brief",
            "",
            f"- Big question: {args.claim}",
            f"- Subquestion ID: {plan_subquestion_id(plan)}",
            f"- Group: {plan_group_title(plan)} (`{plan_group_slug(plan)}`)",
            f"- Query family: {getattr(plan, 'query_family', '')}",
            f"- Subquestion: {getattr(plan, 'claim_subquestion', '')}",
            f"- Concept Boolean map: {getattr(plan, 'boolean_query', '') or 'not available'}",
            f"- Capture depth: {int(args.capture_depth)}",
            f"- Source role: {source_role_for_args(args)}",
            f"- Agent owner: {args.agent_owner or 'unassigned'}",
            "",
            "## Concept Groups",
            "",
        ]
        for group in plan_concept_groups(plan):
            terms = ", ".join(str(term) for term in group.get("terms", []))
            brief.append(f"- {group.get('label', 'group')}: {terms}")
        brief.extend([
            "",
            "## Evidence Focus",
            "",
            f"- Atomic goal: answer this subquestion only: {getattr(plan, 'claim_subquestion', '')}",
            "- subquestion_reading_lens:",
            *[f"  - {item}" for item in reading_lens],
            f"- Include: {guidance['include']}",
            f"- Exclude: {guidance['exclude']}",
            f"- Expected evidence: {guidance['evidence']}",
            f"- Reference selection standard: {guidance['reference']}",
            "- Use Keshav's multi-pass, goal-specific reading process: first identify problem/background/contribution and structure, then inspect methods/data/figures/tables/evidence, then reconstruct the argument and record assumptions, limits, gaps, and subquestion value.",
            "",
            "## Queries",
            "",
        ])
        brief.extend(f"- {query}" for query in getattr(plan, "queries", []) or [])
        brief.extend([
            "",
            "## Required Agent Work",
            "",
            "- Use only this subquestion folder, its article folders, the approved query-plan basis, and the artifacts listed below. Do not rely on main-conversation history as evidence.",
            "- Read primary article folders under `sources/*/articles/`.",
            "- Read second-level reference captures under `references/*/articles/` when present.",
            "- Write `reading-note-zh.md` for each article from the full text, figures, tables, metadata, and references.",
            "- Do not write only `subquestion-summary-zh.md` or only `subagent-response.md`; every primary article folder must receive its own `reading-note-zh.md` first.",
            "- Each note must follow the subquestion_reading_lens, not a generic summary template.",
            "- Each note must cover what the paper did, methods/data, findings, innovation, figures/tables worth checking, close-reading recommendation, and relevance to this subquestion.",
            "- Each note must include the literal validation markers: `five cs`, `图表检查 / figure table check`, `worth_close_reading:`, `worth_close_reading_score_0_to_5:`, `对 subquestion coverage 的影响 / coverage impact`, `high-value seed ledger`, `reference pick / selected reference`, `gap list`, and `proposed next query`.",
            "- Each note must extract a typed seed ledger: named resources, datasets/benchmarks, methods/models/workflows, evaluation terms/metrics, cited seed papers, gaps/blockers, and proposed next simple queries.",
            "- If the article is worth close reading, also write `recommended-references.md`, `recommended-references.csv`, and `recommended-references.json` in that article folder.",
            "- Recommend 2 references per important article by default; each row must include reference text, reason, citation context, and relation to this subquestion.",
            "- Do not run `reference_followup.py` or trigger second-level reference capture yet; reference aggregation and capture happen centrally after all primary subquestions reach terminal coverage.",
            "- Treat `recommended-references.*` as future follow-up evidence, not as permission to capture immediately.",
            "- Do not chase third-level references unless the user explicitly asks.",
            "- Write or update `subquestion-summary-zh.md` after primary notes and primary coverage scoring. It can be updated again after the later centralized reference-follow-up phase.",
            "- The main agent will run `validate_subquestion_reading_gate.py` after your response. If any article note, required marker, response, or summary is missing, the subquestion loop remains blocked.",
        ])
        (folder / "agent-brief.md").write_text("\n".join(brief) + "\n", encoding="utf-8")
        prompt_lines = [
            "# Subquestion Subagent Prompt",
            "",
            "You own only this atomic subquestion. Use local evidence from this subquestion folder, not the main conversation history.",
            "Continue this same subquestion role for reading notes, high-value seed extraction, reference marking, gap review, primary coverage scoring, and next-query suggestions.",
            "Do not run discovery, abstract preview, browser navigation, full-text capture, or delegate to other agents. The main agent controls tools and queue transitions.",
            "",
            f"Subquestion: {getattr(plan, 'claim_subquestion', '')}",
            f"Query family: {getattr(plan, 'query_family', '')}",
            "",
            "subquestion_reading_lens:",
            *[f"- {item}" for item in reading_lens],
            "",
            "Allowed inputs: `agent-brief.md`, article folders under `sources/*/articles/` and, after the centralized reference phase, `references/*/articles/`; `reading-note-zh.md`, `recommended-references.*`, `coverage-review/subquestion-coverage-review.*`, and local loop-state exports for this subquestion.",
            "Completion gate: every primary article under `sources/*/articles/` must have `reading-note-zh.md` with all required literal markers before you write final coverage claims.",
            "Required output artifact: write `subagent-response.md` in this folder after each review pass. Include `review_mode: subagent`, a concrete `agent_id`, reviewed artifact paths, evidence, typed seed ledger, reference picks, gaps, blockers, coverage score, and next action. If no subagent tool is callable, the main agent writes `main-agent-fallback.md`; do not claim fallback yourself.",
        ]
        (folder / "subagent-prompt.md").write_text("\n".join(prompt_lines) + "\n", encoding="utf-8")
        response_path = folder / "subagent-response.md"
        if not response_path.exists():
            response_path.write_text("", encoding="utf-8")
        reference_prompt = folder / "reference-subagent-prompt.md"
        if not reference_prompt.exists():
            reference_prompt.write_text(
                "# Reference Subagent Prompt\n\n"
                "During the centralized reference-follow-up phase, review only local reference candidates for this subquestion. Use `reading-note-zh.md`, `recommended-references.*`, "
                "`reference-candidates.*`, `final-reference-selection.*`, and the related captured article folders as evidence. "
                "Do not use main-conversation history, run publisher discovery, open browsers, capture articles, or delegate to other agents.\n\n"
                "Write `reference-subagent-response.md` with `review_mode: subagent`, a concrete `agent_id`, reviewed artifacts, "
                "selected references, rejected references, blockers, and rationale tied to this subquestion. If no subagent tool is callable, "
                "the main agent writes `reference-main-agent-fallback.md`; do not claim fallback yourself.\n",
                encoding="utf-8",
            )
        reference_response = folder / "reference-subagent-response.md"
        if not reference_response.exists():
            reference_response.write_text("", encoding="utf-8")
        write_reading_notes_index(folder, [])


def write_approved_plan_artifacts(output_root: Path, args: argparse.Namespace, plan_path: Path) -> None:
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    subquestions = payload.get("subquestions") if isinstance(payload.get("subquestions"), list) else []
    question_path = output_root / "question.json"
    existing_question = {}
    if question_path.exists():
        try:
            loaded_question = json.loads(question_path.read_text(encoding="utf-8"))
            if isinstance(loaded_question, dict):
                existing_question = loaded_question
        except Exception:
            existing_question = {}
    existing_subquestions = existing_question.get("subquestions") if isinstance(existing_question.get("subquestions"), list) else []
    merged_subquestions = list(existing_subquestions)
    seen_subquestions = {
        json.dumps({
            "subquestion_id": item.get("subquestion_id") if isinstance(item, dict) else "",
            "query_family": item.get("query_family") if isinstance(item, dict) else "",
            "queries": item.get("queries") if isinstance(item, dict) else [],
        }, ensure_ascii=False, sort_keys=True)
        for item in merged_subquestions
    }
    for item in subquestions:
        if not isinstance(item, dict):
            continue
        key = json.dumps({
            "subquestion_id": item.get("subquestion_id") or "",
            "query_family": item.get("query_family") or "",
            "queries": item.get("queries") or [],
        }, ensure_ascii=False, sort_keys=True)
        if key not in seen_subquestions:
            merged_subquestions.append(item)
            seen_subquestions.add(key)
    question_payload = {
        **existing_question,
        "question": existing_question.get("question") or args.claim,
        "review_context": existing_question.get("review_context") or args.review_context or "",
        "capture_depth": int(existing_question.get("capture_depth") or args.capture_depth),
        "approved_query_plan": str(plan_path),
        "discovery_backend": getattr(args, "discovery_backend", "") or existing_question.get("discovery_backend") or "",
        "subquestions": merged_subquestions,
    }
    if existing_question:
        question_payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    else:
        question_payload["created_at"] = datetime.now().isoformat(timespec="seconds")
    (output_root / "question.json").write_text(json.dumps(question_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    rounds: list[dict[str, Any]] = []
    for index, item in enumerate(subquestions, start=1):
        if not isinstance(item, dict):
            continue
        row = {
            "round": item.get("round") or index,
            "query_family": item.get("query_family") or "",
            "claim_subquestion": item.get("subquestion_text") or "",
            "subquestion_text": item.get("subquestion_text") or "",
            "subquestion_id": item.get("subquestion_id") or f"subquestion-{index:02d}",
            "subquestion_slug": item.get("subquestion_slug") or "",
            "subquestion_group_slug": item.get("subquestion_group_slug") or "general",
            "subquestion_group_title": item.get("subquestion_group_title") or "General",
            "concept_groups": item.get("concept_groups") or [],
            "boolean_query": item.get("boolean_query") or "",
            "publisher_queries": item.get("publisher_queries") or {},
            "source_targets": item.get("source_targets") or [],
            "publisher_targets": item.get("publisher_targets") or [],
            "publisher_discovery_plan": item.get("publisher_discovery_plan") or {},
            "queries": item.get("queries") or [],
        }
        rounds.append(row)
        folder = output_root / "subquestions" / str(row["subquestion_group_slug"]) / str(row["subquestion_id"])
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "subquestion.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        reading_lens = subquestion_reading_lens(str(row["query_family"]))
        (folder / "agent-brief.md").write_text(
            "\n".join([
                "# Subagent Brief",
                "",
                f"- Big question: {args.claim}",
                f"- Subquestion ID: {row['subquestion_id']}",
                f"- Group: {row['subquestion_group_title']} (`{row['subquestion_group_slug']}`)",
                f"- Query family: {row['query_family']}",
                f"- Subquestion: {row['subquestion_text']}",
                "",
                "## Evidence Focus",
                "",
                f"- Atomic goal: answer this subquestion only: {row['subquestion_text']}",
                "- subquestion_reading_lens:",
                *[f"  - {item}" for item in reading_lens],
                "",
                "## Required Agent Work",
                "",
                "- Use only this subquestion folder, its article folders, the approved query-plan basis, and the artifacts listed below. Do not rely on main-conversation history as evidence.",
                "- Read primary article folders under `sources/*/articles/`.",
                "- Write `reading-note-zh.md` for each article from the full text, figures, tables, metadata, and references.",
                "- Do not write only `subquestion-summary-zh.md` or only `subagent-response.md`; every primary article folder must receive its own `reading-note-zh.md` first.",
                "- Each note must include the literal validation markers: `five cs`, `图表检查 / figure table check`, `worth_close_reading:`, `worth_close_reading_score_0_to_5:`, `对 subquestion coverage 的影响 / coverage impact`, `high-value seed ledger`, `reference pick / selected reference`, `gap list`, and `proposed next query`.",
                "- If the article is worth close reading, also write `recommended-references.md`, `recommended-references.csv`, and `recommended-references.json` in that article folder.",
                "- Do not run `reference_followup.py` or trigger second-level reference capture yet; reference aggregation and capture happen centrally after all primary subquestions reach terminal coverage.",
                "- Treat `recommended-references.*` as future follow-up evidence, not as permission to capture immediately.",
                "- Write or update `subquestion-summary-zh.md` after primary notes and primary coverage scoring. It can be updated again after the later centralized reference-follow-up phase.",
                "- The main agent will run `validate_subquestion_reading_gate.py` after your response. If any article note, required marker, response, or summary is missing, the subquestion loop remains blocked.",
            ]) + "\n",
            encoding="utf-8",
        )
        (folder / "subagent-prompt.md").write_text(
            "\n".join(
                [
                    "# Subquestion Subagent Prompt",
                    "",
                    "You own only this atomic subquestion. Use local evidence from this subquestion folder, not the main conversation history.",
                    "For this subquestion, complete the primary local reasoning loop: read every captured primary article, write `reading-note-zh.md`, recommend references from close-read articles, update gaps/seeds, and score primary coverage. Reference aggregation/capture is a later centralized phase after all primary subquestions are terminal.",
                    "Do not run discovery, abstract preview, browser navigation, full-text capture, reference follow-up, or delegate to other agents. The main agent controls tools and queue transitions.",
                    "",
                    f"Subquestion ID: {row['subquestion_id']}",
                    f"Subquestion: {row['subquestion_text']}",
                    f"Query family: {row['query_family']}",
                    "",
                    "subquestion_reading_lens:",
                    *[f"- {item}" for item in reading_lens],
                    "",
                    "Allowed inputs: `agent-brief.md`, `subquestion.json`, article folders under `sources/*/articles/` and, after centralized reference capture, `references/*/articles/`; `reading-note-zh.md`, `recommended-references.*`, `reference-candidates.*`, `final-reference-selection.*`, `coverage-review/subquestion-coverage-review.*`, and local loop-state exports for this subquestion.",
                    "Completion gate: every primary article under `sources/*/articles/` must have `reading-note-zh.md` with all required literal markers before you write final coverage claims.",
                    "Required output artifact: write `subagent-response.md` in this folder after each review pass. Include `review_mode: subagent`, a concrete `agent_id`, reviewed artifact paths, article note status, recommended references, gaps, blockers, coverage score, and next action. If no subagent tool is callable, the main agent writes `main-agent-fallback.md`; do not claim fallback yourself.",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (folder / "reference-subagent-prompt.md").write_text(
            "# Reference Subagent Prompt\n\n"
            "During the centralized reference-follow-up phase, review only local reference candidates for this subquestion. Use `reading-note-zh.md`, `recommended-references.*`, "
            "`reference-candidates.*`, `final-reference-selection.*`, and the related captured article folders as evidence. "
            "Do not use main-conversation history, run publisher discovery, open browsers, capture articles, or delegate to other agents.\n\n"
            "Write `reference-subagent-response.md` with `review_mode: subagent`, a concrete `agent_id`, reviewed artifacts, "
            "selected references, rejected references, blockers, and rationale tied to this subquestion. If no subagent tool is callable, "
            "the main agent writes `reference-main-agent-fallback.md`; do not claim fallback yourself.\n",
            encoding="utf-8",
        )
        if not (folder / "subagent-response.md").exists():
            (folder / "subagent-response.md").write_text("", encoding="utf-8")
        if not (folder / "reference-subagent-response.md").exists():
            (folder / "reference-subagent-response.md").write_text("", encoding="utf-8")
        if not (folder / "reading-notes-index.csv").exists():
            (folder / "reading-notes-index.csv").write_text("article_dir,note_status\n", encoding="utf-8")
    query_rounds_path = output_root / "query-rounds.json"
    existing_rounds = []
    if query_rounds_path.exists():
        try:
            loaded_rounds = json.loads(query_rounds_path.read_text(encoding="utf-8"))
            if isinstance(loaded_rounds, list):
                existing_rounds = loaded_rounds
        except Exception:
            existing_rounds = []
    merged_rounds = list(existing_rounds)
    seen_rounds = {
        json.dumps({
            "subquestion_id": item.get("subquestion_id") if isinstance(item, dict) else "",
            "query_family": item.get("query_family") if isinstance(item, dict) else "",
            "queries": item.get("queries") if isinstance(item, dict) else [],
        }, ensure_ascii=False, sort_keys=True)
        for item in merged_rounds
    }
    for row in rounds:
        key = json.dumps({
            "subquestion_id": row.get("subquestion_id") or "",
            "query_family": row.get("query_family") or "",
            "queries": row.get("queries") or [],
        }, ensure_ascii=False, sort_keys=True)
        if key not in seen_rounds:
            merged_rounds.append(row)
            seen_rounds.add(key)
    query_rounds_path.write_text(json.dumps(merged_rounds, ensure_ascii=False, indent=2), encoding="utf-8")


def write_reading_notes_index(folder: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "article_dir", "publisher", "source_bucket", "source_role", "title", "year", "doi",
        "capture_depth", "note_status", "agent_owner", "agent_score", "agent_assessment_status",
    ]
    index_path = folder / "reading-notes-index.csv"
    with index_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({
                **row,
                "agent_score": row.get("agent_score") or "",
                "agent_assessment_status": row.get("agent_assessment_status") or ("pending_agent_review" if row.get("status") == "captured" else ""),
            })


def write_reading_notes_indexes(output_root: Path, summary_rows: list[dict[str, Any]]) -> None:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in summary_rows:
        subquestion_id = str(row.get("subquestion_id") or "")
        if not subquestion_id:
            continue
        group_slug = str(row.get("subquestion_group_slug") or "general")
        grouped.setdefault((group_slug, subquestion_id), []).append(row)
    for (group_slug, subquestion_id), rows in grouped.items():
        folder = output_root / "subquestions" / group_slug / subquestion_id
        folder.mkdir(parents=True, exist_ok=True)
        write_reading_notes_index(folder, rows)


def append_log(output_root: Path, event: dict[str, Any]) -> None:
    event = {"time": datetime.now().isoformat(timespec="seconds"), **event}
    with (output_root / "capture-log.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def write_indexes(article_dir: Path, data: dict[str, Any]) -> None:
    figures = data.get("figures") or []
    fig_dir = article_dir / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    fig_lines = ["# Figures", ""]
    if figures:
        for fig in figures:
            idx = int(fig.get("index") or 0)
            caption = fig.get("caption") or fig.get("image") or ""
            files = sorted(p.name for p in fig_dir.glob(f"figure-{idx:02d}.*") if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"})
            fig_lines.append(f"- Figure {idx}: {caption}")
            if files:
                fig_lines.append(f"  - files: {', '.join(files)}")
    else:
        fig_lines.append("None detected.")
    (fig_dir / "index.md").write_text("\n".join(fig_lines) + "\n", encoding="utf-8")

    tables = data.get("tables") or []
    table_dir = article_dir / "tables"
    table_dir.mkdir(parents=True, exist_ok=True)
    table_lines = ["# Tables", ""]
    if tables:
        for table in tables:
            idx = int(table.get("index") or 0)
            caption = table.get("caption") or table.get("label") or ""
            table_lines.append(f"- Table {idx}: {caption}")
            table_lines.append(f"  - csv: table-{idx:02d}.csv")
            table_lines.append(f"  - rows: {len(table.get('rows') or [])}")
    else:
        table_lines.append("None detected.")
    (table_dir / "index.md").write_text("\n".join(table_lines) + "\n", encoding="utf-8")


def write_reference_exports(article_dir: Path, data: dict[str, Any]) -> None:
    references = data.get("references") or []
    (article_dir / "references.json").write_text(json.dumps(references, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# References", ""]
    if references:
        for idx, ref in enumerate(references, start=1):
            lines.append(f"{idx}. {ref}")
    else:
        lines.append("None detected.")
    (article_dir / "references.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_agent_note_placeholder(article_dir: Path, data: dict[str, Any], args: argparse.Namespace) -> None:
    title = data.get("title") or data.get("documentTitle") or "Untitled"
    lines = [
        "# Agent-authored reading note required",
        "",
        "`reading-note-zh.md` must be written by an agent after reading this article entry.",
        "",
        "Required inputs:",
        "",
        "- `captured-fulltext.md`",
        "- `figures/index.md` and available figure image files",
        "- `tables/index.md` and `tables/*.csv`",
        "- `metadata.json`",
        "",
        "The note should summarize, in Chinese:",
        "",
        "- 这篇文章主要做了什么",
        "- 研究对象、数据来源和方法",
        "- 主要创新点",
        "- 主要结论",
        "- 哪些 figures/tables 值得优先查看",
        "- 是否值得精读",
        "- 与用户给定研究问题/综述结构的关系；如果没有给定结构，写“待人工归类”",
        "- 如果值得精读，同时写 `recommended-references.md/json/csv`：默认推荐 2 条最值得追踪的文内 references，并说明 citation context、推荐理由、与当前 subquestion 的关系",
        "",
        "Use a three-pass reading note:",
        "",
        "1. First pass: category, context, correctness, contributions, and clarity.",
        "2. Second pass: methods, data, figures, tables, and supporting evidence.",
        "3. Third pass when valuable: assumptions, weaknesses, missing citations, and reusable ideas.",
        "",
        f"Research question: {args.claim}",
        f"Review context: {args.review_context or '待人工归类'}",
        f"Article title: {title}",
    ]
    (article_dir / "NOTE_REQUIRES_AGENT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_auto_extract_note(article_dir: Path, data: dict[str, Any], args: argparse.Namespace) -> None:
    abstract = clean(data.get("abstract") or "")[:1200]
    blocks = data.get("sectionBlocks") or []
    section_titles = [clean(str(b.get("title") or "")) for b in blocks[:40] if clean(str(b.get("title") or ""))]
    lines = [
        f"# Auto-extract draft - {data.get('title') or data.get('documentTitle') or 'Untitled'}",
        "",
        "> Machine-extracted triage only. An agent must write `reading-note-zh.md` separately.",
        "",
        f"- DOI: `{data.get('doi') or ''}`",
        f"- Research question: {args.claim}",
        f"- Review context: {args.review_context or '待人工归类'}",
        "",
        "## Abstract",
        "",
        abstract or "No abstract extracted.",
        "",
        "## Detected Sections",
        "",
    ]
    lines.extend([f"- {title}" for title in section_titles] or ["- No section headings extracted."])
    lines.extend(["", "## Figures and Tables", ""])
    lines.append(f"- Figures: {len(data.get('figures') or [])}")
    lines.append(f"- Tables: {len(data.get('tables') or [])}")
    (article_dir / "auto-extract-note.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def review_context_fit(args: argparse.Namespace, data: dict[str, Any]) -> str:
    if not args.review_context:
        return "unclassified"
    text = " ".join([
        args.review_context,
        data.get("title") or "",
        data.get("abstract") or "",
        " ".join(str(b.get("title") or "") for b in (data.get("sectionBlocks") or [])[:20]),
    ]).lower()
    context_terms = [t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9-]{3,}|[\u4e00-\u9fff]{2,}", args.review_context)]
    hits = [term for term in context_terms if term in text]
    if hits:
        return "potential fit: " + ", ".join(sorted(set(hits))[:12])
    return "review context provided; fit needs agent assessment"


def row_base(result: dict[str, str], publisher: str, args: argparse.Namespace, job: dict[str, Any], page_num: int) -> dict[str, Any]:
    plan = job.get("plan")
    source_platform = result.get("source_platform") or (
        "publisher-authenticated direct" if publisher != "elsevier" or "publisher" in (result.get("href") or "") else "ScienceDirect direct"
    )
    reference_provenance = ""
    if source_role_for_args(args) == "reference":
        reference_provenance = f"{args.parent_article_dir}#ref-{args.parent_reference_index}"
    return {
        "subquestion_group_slug": plan_group_slug(plan),
        "subquestion_group_title": plan_group_title(plan),
        "subquestion_id": plan_subquestion_id(plan),
        "subquestion_slug": plan_subquestion_slug(plan),
        "subquestion_text": getattr(plan, "claim_subquestion", ""),
        "publisher": publisher,
        "source_platform": source_platform,
        "source_bucket": source_bucket_for(publisher),
        "source_role": source_role_for_args(args),
        "query_round": getattr(plan, "round_index", 1),
        "query_family": getattr(plan, "query_family", ""),
        "query_text": job.get("query") or "",
        "page": page_num,
        "discovery_rank": result.get("_page_rank", ""),
        "capture_depth": int(args.capture_depth),
        "parent_article_dir": args.parent_article_dir or "",
        "parent_reference_index": args.parent_reference_index or "",
        "agent_owner": args.agent_owner or "",
        "status": "started",
        "skip_reason": "",
        "title": result.get("title") or "",
        "authors": result.get("authors") or "",
        "year": result.get("year") or "",
        "journal": result.get("journal") or "",
        "volume": "",
        "issue": "",
        "pages": "",
        "doi": result.get("doi") or "",
        "url": result.get("href") or "",
        "article_type": "",
        "abstract": result.get("abstract") or "",
        "keywords": result.get("keywords") or "",
        "cited_by_count": "",
        "openalex_id": "",
        "crossref_type": "",
        "fulltext_chars": "",
        "section_count": "",
        "figure_count": "",
        "table_count": "",
        "note_status": "",
        "worth_close_reading": "",
        "agent_score": "",
        "agent_assessment_status": "",
        "reference_provenance": reference_provenance,
        "article_dir": "",
        "captured_at": "",
        "review_context_fit": args.review_context or "unclassified",
    }


async def capture_article(
    context,
    result: dict[str, str],
    publisher: str,
    args: argparse.Namespace,
    output_root: Path,
    summary_rows: list[dict[str, Any]],
    job: dict[str, Any],
    page_num: int,
) -> None:
    url = result.get("href") or ""
    if not url or not discovery.article_href_allowed(url, publisher):
        return
    page = await context.new_page()
    row = row_base(result, publisher, args, job, page_num)
    summary_rows.append(row)
    write_run_outputs(output_root, summary_rows)
    append_log(output_root, {"event": "article-started", "publisher": publisher, "url": url, "title": result.get("title") or ""})
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        if args.smart_scroll:
            await smart_scroll_page(page, args)
        else:
            await wait_for_load_best_effort(page, args.load_timeout_ms)
            await page.wait_for_timeout(args.article_settle_ms)
            for _ in range(args.scroll_rounds):
                await page.mouse.wheel(0, args.smart_scroll_step)
                await page.wait_for_timeout(args.scroll_wait_ms)
            await page.wait_for_timeout(args.article_final_settle_ms)

        data = await extract_page_with_retry(page)
        await capture.enrich_linked_table_pages(context, data, args.settle_ms)
        data["inputPublisher"] = publisher
        data["inputReviewContext"] = args.review_context or ""
        if result.get("title") and not data.get("title"):
            data["title"] = result["title"]
        fulltext = data.get("fullText") or data.get("body") or ""
        year = data.get("year") or discovery.extract_year(fulltext) or "unknown-year"
        title = data.get("title") or result.get("title") or "Untitled"
        if not year_in_range(year, args.year_start, args.year_end):
            row.update({
                "status": "skipped",
                "skip_reason": f"year_out_of_range:{year}",
                "title": title,
                "doi": data.get("doi") or "",
                "year": year,
                "journal": data.get("journal") or "",
                "fulltext_chars": len(fulltext),
                "section_count": len(data.get("sectionBlocks") or []),
                "figure_count": len(data.get("figures") or []),
                "table_count": len(data.get("tables") or []),
                "captured_at": datetime.now().isoformat(timespec="seconds"),
            })
            append_log(output_root, {"event": "article-skipped", "reason": "year_out_of_range", "year": year, "url": url})
            return
        problem_reason = elsevier_problem_page_reason(data)
        if problem_reason:
            row.update({
                "status": "skipped",
                "skip_reason": problem_reason,
                "title": title,
                "doi": data.get("doi") or "",
                "year": year,
                "journal": data.get("journal") or "",
                "fulltext_chars": len(fulltext),
                "section_count": len(data.get("sectionBlocks") or []),
                "figure_count": len(data.get("figures") or []),
                "table_count": len(data.get("tables") or []),
                "captured_at": datetime.now().isoformat(timespec="seconds"),
            })
            append_log(output_root, {"event": "article-skipped", "reason": problem_reason, "url": url})
            return
        if len(fulltext) < args.min_chars:
            row.update({
                "status": "skipped",
                "skip_reason": "insufficient_fulltext",
                "title": title,
                "doi": data.get("doi") or "",
                "year": year,
                "journal": data.get("journal") or "",
                "fulltext_chars": len(fulltext),
                "section_count": len(data.get("sectionBlocks") or []),
                "figure_count": len(data.get("figures") or []),
                "table_count": len(data.get("tables") or []),
                "captured_at": datetime.now().isoformat(timespec="seconds"),
            })
            append_log(output_root, {"event": "article-skipped", "reason": "insufficient_fulltext", "url": url})
            return

        parent_dir = article_parent_dir(output_root, publisher, job, args)
        parent_dir.mkdir(parents=True, exist_ok=True)
        article_dir = next_numbered_dir(parent_dir, "ref" if source_role_for_args(args) == "reference" else "primary")
        article_dir.mkdir(parents=True, exist_ok=True)
        (article_dir / "metadata.json").write_text(json.dumps({
            "working_article_id": article_dir.name,
            "final_name_hint": article_dir_name(str(year), title, data.get("doi") or ""),
            "publisher": publisher,
            "source_platform": row["source_platform"],
            "source_bucket": row["source_bucket"],
            "source_role": row["source_role"],
            "subquestion_group_slug": row["subquestion_group_slug"],
            "subquestion_group_title": row["subquestion_group_title"],
            "subquestion_id": row["subquestion_id"],
            "subquestion_slug": row["subquestion_slug"],
            "subquestion_text": row["subquestion_text"],
            "title": title,
            "doi": data.get("doi") or "",
            "year": year,
            "journal": data.get("journal") or "",
            "authors": data.get("authors") or [],
            "url": data.get("url") or url,
            "claim": args.claim,
            "review_context": args.review_context or "",
            "query_round": row.get("query_round"),
            "query_family": row.get("query_family"),
            "query_text": row.get("query_text"),
            "discovery_rank": row.get("discovery_rank"),
            "capture_depth": row.get("capture_depth"),
            "parent_article_dir": row.get("parent_article_dir") or "",
            "parent_reference_index": row.get("parent_reference_index") or "",
            "reference_provenance": row.get("reference_provenance") or "",
            "agent_owner": row.get("agent_owner") or "",
            "follow_tertiary_references": False,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        (article_dir / "fulltext.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.write_snapshot_html:
            (article_dir / "snapshot.html").write_text(await page.content(), encoding="utf-8")
        capture.write_table_exports(article_dir, data)
        await capture.write_figure_exports(context, article_dir, data)
        write_indexes(article_dir, data)
        write_reference_exports(article_dir, data)
        capture.write_structure_exports(article_dir, data)
        capture.write_markdown(article_dir, data)
        (article_dir / "captured-fulltext.md").write_text((article_dir / "fulltext.md").read_text(encoding="utf-8"), encoding="utf-8")
        write_auto_extract_note(article_dir, data, args)
        write_agent_note_placeholder(article_dir, data, args)

        authors = data.get("authors") or []
        row.update({
            "status": "captured",
            "skip_reason": "",
            "title": title,
            "authors": "; ".join(authors) if isinstance(authors, list) else str(authors or ""),
            "year": year,
            "journal": data.get("journal") or "",
            "doi": data.get("doi") or "",
            "url": data.get("url") or url,
            "abstract": clean(data.get("abstract") or "")[:1500],
            "keywords": data.get("keywords") or "",
            "fulltext_chars": len(fulltext),
            "section_count": len(data.get("sectionBlocks") or []),
            "figure_count": len(data.get("figures") or []),
            "table_count": len(data.get("tables") or []),
            "note_status": "pending_agent",
            "article_dir": str(article_dir),
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "review_context_fit": review_context_fit(args, data),
        })
        append_log(output_root, {"event": "article-captured", "article_dir": str(article_dir), "doi": row["doi"]})
    except Exception as exc:
        row.update({"status": "error", "skip_reason": f"{type(exc).__name__}: {str(exc)[:500]}"})
        append_log(output_root, {"event": "article-error", "url": url, "error": row["skip_reason"]})
    finally:
        try:
            await page.close()
        except Exception:
            pass
        write_run_outputs(output_root, summary_rows)


async def run(args: argparse.Namespace) -> Path:
    if args.existing_run_dir:
        output_root = args.existing_run_dir.resolve()
    else:
        output_root = args.output_root.resolve() / f"{safe_name(args.claim, 24)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    output_root.mkdir(parents=True, exist_ok=True)
    summary_rows: list[dict[str, Any]] = []
    if args.existing_run_dir:
        existing_summary = output_root / "run-summary.json"
        if existing_summary.exists():
            try:
                data = json.loads(existing_summary.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    summary_rows = data
            except Exception:
                summary_rows = []
    discovery_rows: list[dict[str, Any]] = []
    if args.existing_run_dir:
        discovery_rows = load_json_rows(output_root / "discovery-audit.json") or load_csv_rows(output_root / "discovery-audit.csv")
    previous_rows = load_existing_dedup_rows(output_root, summary_rows)
    captured_keys: set[str] = set()
    for row in summary_rows:
        key = (str(row.get("publisher") or "") + ":" + str(row.get("doi") or row.get("url") or row.get("title") or "")).lower()
        if key != ":":
            captured_keys.add(key)

    if args.search_url:
        manual_plan = discovery.QueryPlan(1, "manual-search-url", "User-provided publisher search URL.", args.query or ["custom-search-url"])
        discovery.assign_subquestion_ids([manual_plan])
        search_jobs = [{"url": url, "query": (args.query or ["custom-search-url"])[0], "plan": manual_plan} for url in args.search_url]
        plans = [manual_plan]
    elif args.approved_query_plan:
        search_jobs, plans = approved_query_plan_jobs(
            args.approved_query_plan,
            args.max_queries,
            args.year_start,
            args.year_end,
            args.sciencedirect_route,
            args.sciencedirect_article_types,
            args.include_springer,
        )
        if args.publisher != "all":
            search_jobs = [
                job for job in search_jobs
                if discovery.infer_publisher(str(job.get("url") or "")) == args.publisher
            ]
    else:
        plans = discovery.build_query_plans(args.claim, args.query or [], args.max_queries, args.rounds, args.openalex_grounding)
        search_jobs = []
        if args.include_structured_publishers:
            search_jobs, plans = discovery.build_search_jobs(
                args.claim,
                args.query or [],
                args.max_queries,
                args.rounds,
                args.year_start,
                args.year_end,
                args.sciencedirect_route,
                args.sciencedirect_article_types,
                args.include_springer,
                args.openalex_grounding,
            )
            if args.publisher != "all":
                search_jobs = [
                    job for job in search_jobs
                    if discovery.infer_publisher(str(job.get("url") or "")) == args.publisher
                ]
    if args.subquestion_id:
        for plan in plans:
            plan.subquestion_id = args.subquestion_id
            plan.subquestion_slug = args.subquestion_slug or plan_subquestion_slug(plan)
            if args.subquestion_group_slug:
                plan.group_slug = args.subquestion_group_slug
            if args.subquestion_group_title:
                plan.group_title = args.subquestion_group_title
            plan.claim_subquestion = args.subquestion_text or plan.claim_subquestion
            if args.query_family:
                plan.query_family = args.query_family
        for job in search_jobs:
            plan = job.get("plan")
            if plan:
                plan.subquestion_id = args.subquestion_id
                plan.subquestion_slug = args.subquestion_slug or plan_subquestion_slug(plan)
                if args.subquestion_group_slug:
                    plan.group_slug = args.subquestion_group_slug
                if args.subquestion_group_title:
                    plan.group_title = args.subquestion_group_title
                plan.claim_subquestion = args.subquestion_text or plan.claim_subquestion
                if args.query_family:
                    plan.query_family = args.query_family
    query_round_rows = [
        {
            "subquestion_group_slug": plan_group_slug(plan),
            "subquestion_group_title": plan_group_title(plan),
            "subquestion_id": plan_subquestion_id(plan),
            "subquestion_slug": plan_subquestion_slug(plan),
            "round": plan.round_index,
            "query_family": plan.query_family,
            "claim_subquestion": plan.claim_subquestion,
            "concept_groups": plan_concept_groups(plan),
            "boolean_query": getattr(plan, "boolean_query", ""),
            "publisher_queries": getattr(plan, "publisher_queries", None) or {},
            "queries": plan.queries,
        }
        for plan in plans
    ]
    query_rounds_path = output_root / "query-rounds.json"
    if args.existing_run_dir and query_rounds_path.exists():
        try:
            existing_rounds = json.loads(query_rounds_path.read_text(encoding="utf-8"))
            if isinstance(existing_rounds, list):
                seen = {
                    json.dumps(
                        {
                            "subquestion_id": r.get("subquestion_id") or "",
                            "subquestion_group_slug": r.get("subquestion_group_slug") or "",
                            "query_family": r.get("query_family") or "",
                            "queries": r.get("queries") or [],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    for r in existing_rounds if isinstance(r, dict)
                }
                for row in query_round_rows:
                    key = json.dumps(
                        {
                            "subquestion_id": row.get("subquestion_id") or "",
                            "subquestion_group_slug": row.get("subquestion_group_slug") or "",
                            "query_family": row.get("query_family") or "",
                            "queries": row.get("queries") or [],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    if key not in seen:
                        existing_rounds.append(row)
                query_round_rows = existing_rounds
        except Exception:
            pass
    query_rounds_path.write_text(json.dumps(query_round_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    write_question_artifacts(output_root, args, plans)

    if getattr(args, "discovery_backend", "opencli") == "opencli":
        await run_opencli_discovery(args, output_root, search_jobs, discovery_rows, previous_rows)
        write_run_outputs(output_root, summary_rows)
        if args.discovery_only and args.write_query_refinement_packets:
            result = query_refinement.build_packets(
                output_root,
                page=args.query_refinement_page,
                top_candidates_per_query=args.query_refinement_top_candidates,
                picks_per_query=args.query_refinement_picks_per_query,
                iteration=args.query_refinement_iteration,
                total_iterations=args.query_refinement_total_iterations,
            )
            append_log(output_root, {
                "event": "query-refinement-packets-written",
                "output_dir": result.get("output_dir", ""),
                "groups": result.get("groups", 0),
            })
        return output_root

    raise SystemExit("Unsupported discovery backend. Use --discovery-backend opencli.")


def main() -> int:
    today = date.today()
    parser = argparse.ArgumentParser()
    parser.add_argument("--claim", required=True, help="Research question, review claim, or evidence need.")
    parser.add_argument("--review-context", default="", help="Optional section outline or review context for final agent note triage.")
    parser.add_argument("--search-url", action="append", help="Verified publisher-authenticated publisher search URL.")
    parser.add_argument(
        "--query",
        action="append",
        help=(
            "Advanced exact-query override; can be supplied multiple times. Do not use for the user's "
            "thesis/research topic; new root runs should use --approved-query-plan."
        ),
    )
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--max-queries", type=int, default=3)
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--max-results-per-page", type=int, default=5)
    parser.add_argument("--discovery-only", action="store_true", help="Only save search-page discovery audit rows; do not capture article full text.")
    parser.add_argument("--discovery-backend", choices=["opencli"], default="opencli")
    parser.add_argument("--opencli-session", default="lit", help="OpenCLI browser session name for --discovery-backend opencli.")
    parser.add_argument("--publisher-home-url", default=opencli_browser.PUBLISHER_HOME_URL)
    parser.add_argument("--publisher-auth-wait-ms", type=int, default=3000)
    parser.add_argument("--skip-publisher-preflight", action="store_true")
    parser.add_argument("--allow-first-n-capture", action="store_true", help="Debug escape hatch: allow direct first-N capture without query-refinement queue.")
    parser.add_argument("--write-query-refinement-packets", action="store_true", default=True, help="After --discovery-only, write query-refinement subagent packets.")
    parser.add_argument("--no-query-refinement-packets", dest="write_query_refinement_packets", action="store_false")
    parser.add_argument("--query-refinement-page", type=int, default=1)
    parser.add_argument("--query-refinement-top-candidates", type=int, default=12)
    parser.add_argument("--query-refinement-picks-per-query", type=int, default=3)
    parser.add_argument("--query-refinement-iteration", type=int, default=1)
    parser.add_argument("--query-refinement-total-iterations", type=int, default=3)
    parser.add_argument("--year-start", type=int, default=today.year - 4)
    parser.add_argument("--year-end", type=int, default=today.year)
    parser.add_argument("--sciencedirect-route", choices=["direct"], default="direct")
    parser.add_argument("--sciencedirect-article-types", default="FLA,REV")
    parser.add_argument("--include-springer", dest="include_springer", action="store_true", default=True)
    parser.add_argument("--no-springer", dest="include_springer", action="store_false")
    parser.add_argument("--include-structured-publishers", dest="include_structured_publishers", action="store_true", default=True)
    parser.add_argument("--no-structured-publishers", dest="include_structured_publishers", action="store_false")
    parser.add_argument("--publisher", choices=["all", "elsevier", "acs", "wiley", "springer"], default="all", help="Limit generated publisher search jobs. Use elsevier for ScienceDirect-only discovery.")
    parser.add_argument("--openalex-grounding", dest="openalex_grounding", action="store_true", default=True)
    parser.add_argument("--no-openalex-grounding", dest="openalex_grounding", action="store_false")
    parser.add_argument("--approved-query-plan", type=Path, help="User-approved query-plan-preview.json required for new root discovery.")
    parser.add_argument("--output-root", type=Path, default=Path("LiteratureCaptures"))
    parser.add_argument("--existing-run-dir", type=Path, help="Append captures to an existing run root, used for second-level reference follow-up.")
    parser.add_argument("--subquestion-id", default="", help="Force captures into an existing subquestion folder.")
    parser.add_argument("--subquestion-slug", default="", help="Slug for --subquestion-id when appending follow-up captures.")
    parser.add_argument("--subquestion-group-slug", default="", help="Composite group folder for --subquestion-id follow-up captures.")
    parser.add_argument("--subquestion-group-title", default="", help="Display title for --subquestion-group-slug.")
    parser.add_argument("--subquestion-text", default="", help="Subquestion text for follow-up captures.")
    parser.add_argument("--query-family", default="", help="Query family label for forced follow-up captures.")
    parser.add_argument("--capture-depth", type=int, default=1)
    parser.add_argument("--parent-article-dir", default="")
    parser.add_argument("--parent-reference-index", default="")
    parser.add_argument("--agent-owner", default="")
    parser.add_argument("--settle-ms", type=int, default=8000)
    parser.add_argument("--load-timeout-ms", type=int, default=12000)
    parser.add_argument("--manual-blocker-wait-ms", type=int, default=12000, help="When a robot/CAPTCHA page is detected, wait this long for manual resolution before recording blocked_robot.")
    parser.add_argument("--abstract-expand-wait-ms", type=int, default=700, help="Wait after clicking search-page abstract controls before extracting result rows.")
    parser.add_argument("--article-settle-ms", type=int, default=1200)
    parser.add_argument("--article-final-settle-ms", type=int, default=1200)
    parser.add_argument("--scroll-rounds", type=int, default=10)
    parser.add_argument("--scroll-wait-ms", type=int, default=350)
    parser.add_argument("--smart-scroll", dest="smart_scroll", action="store_true", default=True)
    parser.add_argument("--no-smart-scroll", dest="smart_scroll", action="store_false")
    parser.add_argument("--smart-scroll-min-rounds", type=int, default=2)
    parser.add_argument("--smart-scroll-stable-rounds", type=int, default=2)
    parser.add_argument("--smart-scroll-step", type=int, default=3200)
    parser.add_argument("--min-chars", type=int, default=2500)
    parser.add_argument("--allow-non-english-queries", action="store_true", help="Debug escape hatch. Normal skill use requires English claim/query text.")
    parser.add_argument("--write-snapshot-html", action="store_true", help="Opt in to saving raw page HTML snapshots.")
    args = parser.parse_args()
    if not args.allow_non_english_queries:
        non_english = []
        if contains_cjk(args.claim):
            non_english.append("--claim")
        for idx, query in enumerate(args.query or [], start=1):
            if contains_cjk(query):
                non_english.append(f"--query[{idx}]")
        if non_english:
            raise SystemExit(
                "English query planning is required before capture. Translate/normalize the user question into English, "
                "show the subquestion/query plan for approval, then rerun. Non-English fields: " + ", ".join(non_english)
            )
    if int(args.capture_depth) >= 2 and (not args.parent_article_dir or not args.parent_reference_index):
        raise SystemExit(
            "Second-level reference capture requires --parent-article-dir and --parent-reference-index for provenance."
        )
    enforce_capture_gate(args)
    validate_approved_query_plan(args)
    validate_discovery_backend_gate(args)
    enforce_subquestion_append_gate(args)
    if should_enforce_publisher_browser_config(args):
        enforce_publisher_browser_config(args)
        enforce_visible_publisher(args)
    output_root = asyncio.run(run(args))
    print(f"output_root={output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
