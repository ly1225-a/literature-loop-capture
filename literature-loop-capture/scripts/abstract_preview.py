#!/usr/bin/env python3
"""Build abstract previews from search-page evidence, OpenCLI, and OpenAlex."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import parse, request
from urllib.error import URLError

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import capture_core as capture  # noqa: E402
import discovery_core as discovery  # noqa: E402
import opencli_browser  # noqa: E402
import source_registry  # noqa: E402


SUPPORTED_OPENALEX_FALLBACK_SOURCES = {"elsevier", "sciencedirect", "acs", "wiley", "springer"}
PUBLISHER_ORDER = ["elsevier", "acs", "wiley", "springer"]
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>?#)]+", re.IGNORECASE)


def clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def word_tokens(text: Any) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", clean(text).lower())
        if len(token) > 2 and token not in {"the", "and", "for", "with", "from", "this", "that", "into", "using"}
    }


def title_similarity(a: Any, b: Any) -> float:
    left = word_tokens(a)
    right = word_tokens(b)
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, len(left | right))


def infer_doi(entry: dict[str, Any]) -> str:
    raw = clean(entry.get("doi"))
    if raw:
        return raw.removeprefix("https://doi.org/").removeprefix("http://doi.org/").lower()
    for field in ["href", "landing_url", "page_url", "context"]:
        match = DOI_RE.search(clean(entry.get(field)))
        if match:
            return parse.unquote(match.group(0)).rstrip(".,;").lower()
    return ""


def is_substantive_abstract(text: Any, *, title: Any = "", source: Any = "") -> bool:
    abstract = clean(text)
    if not abstract:
        return False
    source_text = clean(source).lower()
    title_text = clean(title)
    sentence_count = len(re.findall(r"[.!?](?:\s|$)", abstract))
    if source_text.endswith("_search_result_snippet") and ("..." in abstract or len(abstract) < 700):
        return False
    if title_text and title_text.lower() in abstract.lower() and len(abstract) < 500 and sentence_count <= 2:
        return False
    if re.match(r"^(article|review|original|research|conference paper|book chapter)\b", abstract, flags=re.IGNORECASE):
        if abstract.lower().endswith(" abstract") and len(abstract) < 600:
            return False
    if len(abstract) < 280:
        return False
    return sentence_count >= 2 or len(abstract) >= 650


def source_key_for_entry(entry: dict[str, Any]) -> str:
    href = clean(entry.get("href"))
    page_url = clean(entry.get("page_url"))
    source_key = clean(entry.get("source_key") or entry.get("publisher")).lower()
    return source_key or discovery.infer_publisher(href or page_url) or source_registry.infer_source_key(href)


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def normalized_identifier(value: Any) -> str:
    return clean(value).lower().rstrip("/")


def article_cache_key(entry: dict[str, Any]) -> str:
    doi = infer_doi(entry)
    if doi:
        return f"doi:{doi}"
    for field in ["href", "landing_url_after_redirect", "landing_url"]:
        value = normalized_identifier(entry.get(field))
        if value:
            return f"url:{value}"
    title = normalized_identifier(entry.get("title") or entry.get("page_title"))
    return f"title:{title}" if title else ""


def preview_row_key(entry: dict[str, Any]) -> str:
    article_key = article_cache_key(entry)
    parts = [
        clean(entry.get("group_id")),
        clean(entry.get("subquestion_id")),
        clean(entry.get("publisher") or entry.get("source_key")),
        clean(entry.get("current_query") or entry.get("query_text")),
        clean(entry.get("source_bucket")),
        clean(entry.get("rank")),
        article_key,
    ]
    return "|".join(parts)


def existing_preview_rows(output_dir: Path, planned_keys: set[str]) -> list[dict[str, Any]]:
    path = output_dir / "abstract-preview.csv"
    if not path.exists():
        return []
    rows = []
    for row in read_csv(path):
        if preview_row_key(row) in planned_keys:
            rows.append(dict(row))
    return rows


def reusable_preview_cache(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = article_cache_key(row)
        if not key:
            continue
        if row.get("status") != "abstract_found":
            continue
        if not is_substantive_abstract(row.get("abstract"), title=row.get("title") or row.get("page_title"), source=row.get("abstract_source")):
            continue
        cache[key] = row
    return cache


def latest_recommendations(run_dir: Path) -> Path:
    candidates = sorted(
        run_dir.glob("query-refinement/iteration-*/query-refinement-recommendations.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise SystemExit("No query-refinement-recommendations.json found. Have the search reviewer fill the template first.")
    return candidates[0]


def article_entries(recommendations: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for group in recommendations.get("groups") or []:
        group_id = clean(group.get("group_id"))
        subquestion_id = clean(group.get("subquestion_id"))
        publisher = clean(group.get("publisher") or group.get("source_key"))
        current_query = clean(group.get("current_query"))
        for bucket in ["abstract_probe_articles", "capture_articles", "top_articles"]:
            for item in group.get(bucket) or []:
                href = clean(item.get("href") or item.get("landing_url"))
                title = clean(item.get("title"))
                if not href or not title:
                    continue
                entries.append({
                    "group_id": group_id,
                    "subquestion_id": subquestion_id,
                    "publisher": publisher,
                    "source_key": clean(item.get("source_key")) or publisher,
                    "current_query": current_query,
                    "source_bucket": bucket,
                    "rank": item.get("rank", ""),
                    "title": title,
                    "href": href,
                    "abstract": clean(item.get("abstract")),
                    "abstract_source": clean(item.get("abstract_source")),
                    "page_url": clean(item.get("page_url") or item.get("search_url") or item.get("searchUrl")),
                    "context": clean(item.get("context")),
                    "search_description": clean(item.get("abstract") or item.get("description") or item.get("context")),
                    "doi": clean(item.get("doi")),
                    "agent_reason": clean(item.get("reason") or item.get("agent_reason") or item.get("rcs_reasoning")),
                    "agent_score": item.get("rcs_0_to_10") or item.get("agent_score", ""),
                    "rcs_0_to_10": item.get("rcs_0_to_10", ""),
                    "rcs_flag": clean(item.get("rcs_flag")),
                    "rcs_reasoning": clean(item.get("rcs_reasoning")),
                })
    return entries


def article_entries_from_queue(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for row in rows:
        href = clean(row.get("href") or row.get("landing_url"))
        title = clean(row.get("title"))
        if not href or not title:
            continue
        entries.append({
            "group_id": clean(row.get("group_id")),
            "subquestion_id": clean(row.get("subquestion_id")),
            "publisher": clean(row.get("publisher") or row.get("publisher_key") or row.get("source_key")),
            "source_key": clean(row.get("source_key")),
            "current_query": clean(row.get("query_text")),
            "source_bucket": clean(row.get("source_bucket")) or "abstract_probe_articles",
            "rank": row.get("rank", ""),
            "title": title,
            "href": href,
            "abstract": clean(row.get("abstract")),
            "abstract_source": clean(row.get("abstract_source")),
            "page_url": clean(row.get("page_url") or row.get("search_url") or row.get("searchUrl")),
            "context": clean(row.get("context")),
            "search_description": clean(row.get("context") or row.get("description") or row.get("abstract")),
            "doi": clean(row.get("doi")),
            "agent_reason": clean(row.get("agent_reason") or row.get("rcs_reasoning")),
            "agent_score": row.get("rcs_0_to_10") or row.get("agent_score", ""),
            "rcs_0_to_10": row.get("rcs_0_to_10", ""),
            "rcs_flag": clean(row.get("rcs_flag")),
            "rcs_reasoning": clean(row.get("rcs_reasoning")),
        })
    return entries


def interleave_entries_by_publisher(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {publisher: [] for publisher in PUBLISHER_ORDER}
    extras: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        publisher = clean(entry.get("publisher") or entry.get("source_key")).lower()
        if publisher in buckets:
            buckets[publisher].append(entry)
        else:
            extras.setdefault(publisher, []).append(entry)
    ordered_publishers = PUBLISHER_ORDER + sorted(extras)
    out: list[dict[str, Any]] = []
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


def openalex_request(params: dict[str, str]) -> dict[str, Any]:
    url = "https://api.openalex.org/works?" + parse.urlencode(params)
    req = request.Request(url, headers={"User-Agent": "literature-loop-capture/1.0"})
    try:
        with request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError) as exc:
        return {"_error": f"{type(exc).__name__}: {exc}"}


def abstract_from_openalex_inverted_index(index: Any) -> str:
    if not isinstance(index, dict):
        return ""
    positioned: dict[int, str] = {}
    for token, positions in index.items():
        if not isinstance(positions, list):
            continue
        for position in positions:
            try:
                positioned[int(position)] = str(token)
            except (TypeError, ValueError):
                continue
    return clean(" ".join(positioned[pos] for pos in sorted(positioned))) if positioned else ""


def openalex_metadata_fallback(entry: dict[str, Any]) -> dict[str, str]:
    doi = infer_doi(entry)
    title = clean(entry.get("title"))
    if doi:
        data = openalex_request({"filter": f"doi:{doi}", "per-page": "1"})
    elif title:
        data = openalex_request({"search": title, "per-page": "1"})
    else:
        return {"abstract": "", "method": "openalex_metadata_missing_identifier"}
    works = data.get("results") if isinstance(data, dict) else []
    if not isinstance(works, list) or not works:
        return {"abstract": "", "method": "openalex_metadata_not_found", "error": clean(data.get("_error") if isinstance(data, dict) else "")}
    work = works[0] if isinstance(works[0], dict) else {}
    work_title = clean(work.get("title"))
    if title and work_title:
        similarity = title_similarity(title, work_title)
        threshold = 0.35 if doi else 0.55
        if similarity < threshold:
            return {
                "abstract": "",
                "method": "openalex_metadata_title_mismatch",
                "title": work_title,
                "error": f"OpenAlex title mismatch; similarity={similarity:.2f}; query_title={title}; openalex_title={work_title}",
            }
    primary_location = work.get("primary_location") if isinstance(work.get("primary_location"), dict) else {}
    source = primary_location.get("source") if isinstance(primary_location.get("source"), dict) else {}
    authors = []
    for authorship in work.get("authorships") or []:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author") if isinstance(authorship.get("author"), dict) else {}
        name = clean(author.get("display_name"))
        if name:
            authors.append(name)
    abstract = abstract_from_openalex_inverted_index(work.get("abstract_inverted_index"))
    doi_text = clean(work.get("doi")).removeprefix("https://doi.org/").removeprefix("http://doi.org/")
    return {
        "abstract": abstract,
        "method": "openalex_metadata" if abstract else "openalex_metadata_no_abstract",
        "title": work_title,
        "journal": clean(source.get("display_name")),
        "year": clean(work.get("publication_year")),
        "doi": doi_text,
        "authors": "; ".join(authors[:12]),
    }


def pubmed_efetch_metadata(href: str) -> dict[str, str]:
    match = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", href)
    if not match:
        return {"abstract": "", "method": "pubmed_pmid_missing"}
    pmid = match.group(1)
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + parse.urlencode({"db": "pubmed", "id": pmid, "retmode": "xml"})
    req = request.Request(url, headers={"User-Agent": "literature-loop-capture/1.0"})
    try:
        with request.urlopen(req, timeout=30) as response:
            root = ET.fromstring(response.read())
    except (OSError, URLError, ET.ParseError) as exc:
        return {"abstract": "", "method": "pubmed_efetch_unavailable", "error": f"{type(exc).__name__}: {exc}"}
    article = root.find(".//PubmedArticle")
    if article is None:
        return {"abstract": "", "method": "pubmed_efetch_empty"}
    abstract = " ".join(clean("".join(node.itertext())) for node in article.findall(".//Abstract/AbstractText"))
    return {"abstract": abstract, "method": "pubmed_efetch_xml" if abstract else "pubmed_efetch_no_abstract"}


def merge_metadata(row: dict[str, Any], metadata: dict[str, str]) -> None:
    if metadata.get("title"):
        row["page_title"] = metadata["title"]
    for field in ["doi", "journal", "year", "authors"]:
        if metadata.get(field) and not clean(row.get(field)):
            row[field] = metadata[field]
    if metadata.get("abstract"):
        row["abstract"] = metadata["abstract"]
        row["status"] = "abstract_found"
    row["abstract_extraction_method"] = metadata.get("method") or row.get("abstract_extraction_method") or ""
    if metadata.get("error"):
        row["error"] = metadata["error"]


def refresh_screening_text(row: dict[str, Any]) -> None:
    row["screening_text"] = " ".join(
        part
        for part in [
            clean(row.get("title") or row.get("page_title")),
            clean(row.get("abstract")),
            clean(row.get("context")),
            clean(row.get("search_description")),
        ]
        if part
    )[:5000]


def base_row(entry: dict[str, Any]) -> dict[str, Any]:
    abstract = clean(entry.get("abstract"))
    return {
        **entry,
        "source_key": source_key_for_entry(entry),
        "page_title": clean(entry.get("title")),
        "status": "abstract_found" if abstract else "no_preview_evidence",
        "browser_preview_status": "",
        "browser_preview_error": "",
        "abstract": abstract,
        "abstract_source": clean(entry.get("abstract_source")) or ("search_page_saved" if abstract else ""),
        "abstract_extraction_method": "search_page_saved" if abstract else "",
        "abstract_candidates": "[]",
        "screening_text": " ".join(
            part for part in [clean(entry.get("title")), abstract, clean(entry.get("context")), clean(entry.get("search_description"))] if part
        )[:5000],
        "landing_url_after_redirect": "",
        "identifier_confidence": "",
    }


def opencli_metadata(entry: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    href = clean(entry.get("href"))
    if not href:
        return {"abstract": "", "method": "opencli_missing_href", "error": "missing href"}
    session = clean(args.opencli_session) or "lit-preview"
    try:
        opencli_browser.open_url(session, href, timeout=90)
        opencli_browser.settle_article_page(
            session,
            initial_wait_ms=int(getattr(args, "article_open_wait_ms", None) or getattr(args, "settle_ms", 5000) or 5000),
            scroll_rounds=int(getattr(args, "scroll_rounds", 0) or 0),
            scroll_wait_ms=int(getattr(args, "scroll_wait_ms", 1000) or 1000),
        )
        data = opencli_browser.eval_json(session, capture.article_extraction_script(), timeout=90)
    except Exception as exc:
        return {"abstract": "", "method": "opencli_article_page_error", "error": f"{type(exc).__name__}: {str(exc)[:500]}"}
    if not isinstance(data, dict):
        return {"abstract": "", "method": "opencli_article_page_invalid_payload"}
    return {
        "abstract": clean(data.get("abstract")),
        "method": data.get("abstractExtractionMethod") or data.get("abstract_extraction_method") or "opencli_article_page",
        "title": clean(data.get("title") or data.get("documentTitle")),
        "doi": clean(data.get("doi")),
        "journal": clean(data.get("journal")),
        "year": clean(data.get("year")),
        "authors": "; ".join(data.get("authors") or []) if isinstance(data.get("authors"), list) else clean(data.get("authors")),
        "landing_url_after_redirect": clean(data.get("url")),
        "abstract_candidates": json.dumps(data.get("abstractCandidates") or [], ensure_ascii=False),
    }


def preview_entry(entry: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    row = base_row(entry)
    original_abstract = clean(row.get("abstract"))
    if is_substantive_abstract(original_abstract, title=row.get("title"), source=row.get("abstract_source")):
        return row
    if original_abstract:
        row["status"] = "no_substantive_preview_evidence"
        row["abstract_extraction_method"] = "search_page_saved_non_substantive"
    source_key = source_key_for_entry(entry)
    if source_key in SUPPORTED_OPENALEX_FALLBACK_SOURCES:
        fallback = openalex_metadata_fallback(entry)
        merge_metadata(row, fallback)
        if is_substantive_abstract(row.get("abstract"), title=row.get("title"), source="openalex_metadata"):
            row["abstract_source"] = "openalex_metadata"
            refresh_screening_text(row)
            return row
    if "pubmed.ncbi.nlm.nih.gov" in clean(entry.get("href")).lower():
        fallback = pubmed_efetch_metadata(clean(entry.get("href")))
        merge_metadata(row, fallback)
        if is_substantive_abstract(row.get("abstract"), title=row.get("title"), source="pubmed_efetch"):
            row["abstract_source"] = "pubmed_efetch"
            refresh_screening_text(row)
            return row
    metadata = opencli_metadata(entry, args)
    row["browser_preview_status"] = metadata.get("method", "")
    row["browser_preview_error"] = metadata.get("error", "")
    if metadata.get("landing_url_after_redirect"):
        row["landing_url_after_redirect"] = metadata["landing_url_after_redirect"]
    if metadata.get("abstract_candidates"):
        row["abstract_candidates"] = metadata["abstract_candidates"]
    merge_metadata(row, metadata)
    if is_substantive_abstract(row.get("abstract"), title=row.get("title"), source="opencli_article_page"):
        row["abstract_source"] = "opencli_article_page"
        refresh_screening_text(row)
        return row
    row["status"] = "abstract_missing_needs_dom_audit"
    if original_abstract and not clean(row.get("abstract")):
        row["abstract"] = original_abstract
        row["abstract_source"] = clean(entry.get("abstract_source")) or "search_page_saved_non_substantive"
        refresh_screening_text(row)
    return row


def preview_entry_from_cache(entry: dict[str, Any], cached: dict[str, Any]) -> dict[str, Any]:
    row = base_row(entry)
    for field in [
        "page_title", "status", "doi", "authors", "journal", "year",
        "browser_preview_status", "browser_preview_error", "abstract",
        "abstract_source", "abstract_extraction_method", "abstract_candidates",
        "landing_url_after_redirect", "error",
    ]:
        if clean(cached.get(field)):
            row[field] = cached[field]
    row["identifier_confidence"] = "reused_previous_abstract_preview"
    refresh_screening_text(row)
    return row


def native_metadata_fallback(entry: dict[str, Any], source_key: str | None = None) -> dict[str, str]:
    """Compatibility wrapper for tests and callers that need metadata-only fallback."""
    source = clean(source_key or source_key_for_entry(entry)).lower()
    if source in SUPPORTED_OPENALEX_FALLBACK_SOURCES:
        return openalex_metadata_fallback(entry)
    if "pubmed.ncbi.nlm.nih.gov" in clean(entry.get("href")).lower():
        return pubmed_efetch_metadata(clean(entry.get("href")))
    return {"abstract": "", "method": "unsupported_source_no_native_metadata"}


def preview_one(_client: Any, entry: dict[str, Any]) -> dict[str, Any]:
    """Metadata-only preview path retained for focused unit tests.

    The OpenCLI CLI path uses `preview_entry`; this wrapper deliberately does not
    call browser automation.
    """
    row = base_row(entry)
    if clean(row.get("abstract")):
        row["status"] = "abstract_previewed"
        row["abstract_source"] = "existing_candidate_abstract"
        row["abstract_extraction_method"] = "existing_candidate_abstract"
        return row
    metadata = native_metadata_fallback(entry, source_key_for_entry(entry))
    merge_metadata(row, metadata)
    if clean(row.get("abstract")):
        row["status"] = "abstract_previewed"
        row["abstract_source"] = metadata.get("method") or "native_metadata"
    else:
        row["status"] = "abstract_missing_needs_dom_audit"
    return row


def write_agent_screening_packet(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    packet = []
    lines = [
        "# Agent Screening Packet",
        "",
        "Use this packet to decide which candidates should move to the existing OpenCLI publisher full-text capture queue.",
        "",
    ]
    for index, row in enumerate(rows, start=1):
        evidence = {
            "index": index,
            "title": row.get("page_title") or row.get("title"),
            "url": row.get("href"),
            "source_key": row.get("source_key"),
            "doi": row.get("doi"),
            "year": row.get("year"),
            "status": row.get("status"),
            "abstract_extraction_method": row.get("abstract_extraction_method"),
            "search_description": row.get("search_description"),
            "abstract": row.get("abstract"),
            "screening_text": row.get("screening_text"),
        }
        packet.append(evidence)
        lines.extend([
            f"## {index}. {evidence['title'] or 'Untitled'}",
            "",
            f"- Source: `{evidence['source_key'] or ''}`",
            f"- DOI: `{evidence['doi'] or ''}`",
            f"- Year: {evidence['year'] or ''}",
            f"- URL: {evidence['url'] or ''}",
            f"- Status: `{evidence['status'] or ''}`",
            f"- Extraction: `{evidence['abstract_extraction_method'] or ''}`",
            "",
            "### Search Description",
            evidence["search_description"] or "No search description.",
            "",
            "### Abstract Field",
            evidence["abstract"] or "No abstract field extracted.",
            "",
            "### Screening Text",
            evidence["screening_text"] or "No combined screening text.",
            "",
            "### Agent Decision",
            "- capture_decision: capture | maybe | skip",
            "- reason:",
            "",
        ])
    (output_dir / "agent-screening-packet.json").write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "agent-screening-packet.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_outputs(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "abstract-preview.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    write_agent_screening_packet(output_dir, rows)
    fields = [
        "group_id", "subquestion_id", "publisher", "source_key", "current_query", "source_bucket",
        "rank", "agent_score", "rcs_0_to_10", "rcs_flag", "rcs_reasoning",
        "title", "href", "status", "page_title", "doi",
        "authors", "journal", "year", "page_url", "context", "browser_preview_status",
        "browser_preview_error", "abstract", "search_description", "screening_text",
        "abstract_source", "abstract_extraction_method", "abstract_candidates",
        "landing_url_after_redirect", "identifier_confidence", "agent_reason", "error",
    ]
    with (output_dir / "abstract-preview.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    lines = ["# Abstract Preview", ""]
    for row in rows:
        lines.extend([
            f"## {row.get('page_title') or row.get('title') or 'Untitled'}",
            "",
            f"- Group: `{row.get('group_id', '')}`",
            f"- Source: {row.get('source_key', '')}",
            f"- Query: `{row.get('current_query', '')}`",
            f"- DOI: `{row.get('doi', '')}`",
            f"- Year: {row.get('year', '')}",
            f"- Status: `{row.get('status', '')}`",
            f"- Abstract extraction: `{row.get('abstract_extraction_method', '')}`",
            f"- URL: {row.get('href', '')}",
            f"- Agent reason: {row.get('agent_reason', '')}",
            "",
            row.get("abstract") or row.get("error") or "No abstract available from search page, OpenCLI page metadata, or OpenAlex fallback.",
            "",
        ])
    (output_dir / "abstract-preview.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> Path:
    run_dir = args.run_dir.resolve()
    recommendations_path = None
    if args.abstract_queue:
        entries = article_entries_from_queue(read_csv(args.abstract_queue.resolve()))
    else:
        recommendations_path = (args.recommendations or latest_recommendations(run_dir)).resolve()
        recommendations = load_json(recommendations_path, {})
        if not isinstance(recommendations, dict):
            raise SystemExit("recommendations must be a JSON object")
        entries = article_entries(recommendations)
    entries = interleave_entries_by_publisher(entries)
    if args.limit:
        entries = entries[:args.limit]
    output_dir = (args.output_dir or ((recommendations_path.parent if recommendations_path else run_dir) / "abstract-preview")).resolve()
    planned_keys = {preview_row_key(entry) for entry in entries}
    rows: list[dict[str, Any]] = [] if getattr(args, "no_resume", False) else existing_preview_rows(output_dir, planned_keys)
    completed_keys = {preview_row_key(row) for row in rows}
    article_cache = reusable_preview_cache(rows)
    try:
        for entry in entries:
            row_key = preview_row_key(entry)
            if row_key in completed_keys:
                continue
            cached = article_cache.get(article_cache_key(entry))
            if cached:
                row = preview_entry_from_cache(entry, cached)
            else:
                row = preview_entry(entry, args)
            rows.append(row)
            completed_keys.add(row_key)
            cached_key = article_cache_key(row)
            if cached_key and row.get("status") == "abstract_found":
                article_cache[cached_key] = row
            write_outputs(output_dir, rows)
    finally:
        try:
            opencli_browser.close(clean(args.opencli_session) or "lit-preview")
        except Exception:
            pass
    return output_dir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--recommendations", type=Path, help="Reviewer-filled query-refinement-recommendations.json.")
    parser.add_argument("--abstract-queue", type=Path, help="Applied abstract-preview-queue.csv from apply_query_decisions.py.")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--opencli-session", default="lit-preview")
    parser.add_argument("--settle-ms", type=int, default=2500)
    parser.add_argument("--article-open-wait-ms", type=int, default=5000)
    parser.add_argument("--scroll-rounds", type=int, default=1)
    parser.add_argument("--scroll-wait-ms", type=int, default=350)
    parser.add_argument("--no-resume", action="store_true", help="Ignore an existing abstract-preview.csv and rebuild from the start.")
    args = parser.parse_args()
    output_dir = run(args)
    print(f"output_dir={output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
