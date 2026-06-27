#!/usr/bin/env python3
"""Rank follow-up references and reconcile their Crossref metadata."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import discovery_core as discovery  # noqa: E402


FOLLOWUP_FIELDS = [
    "subquestion_group_slug",
    "subquestion_group_title",
    "subquestion_id",
    "subquestion_slug",
    "query_round",
    "query_family",
    "claim_subquestion",
    "query_text",
    "rank",
    "score",
    "score_basis",
    "agent_score",
    "agent_reason",
    "agent_assessment_status",
    "selection_status",
    "approval_status",
    "citation_count",
    "citation_contexts",
    "reference_text",
    "source_article_title",
    "source_article_dir",
    "source_reference_index",
    "parsed_doi",
    "crossref_doi",
    "crossref_title",
    "crossref_authors",
    "crossref_year",
    "crossref_container",
    "crossref_type",
    "crossref_pdf_url",
    "crossref_landing_url",
    "verification_status",
    "capture_hint",
    "capture_query",
    "capture_notes",
]

CAPTURE_QUEUE_FIELDS = [
    "subquestion_group_slug",
    "subquestion_group_title",
    "subquestion_id",
    "subquestion_slug",
    "query_round",
    "query_family",
    "claim_subquestion",
    "rank",
    "action",
    "capture_depth",
    "follow_tertiary_references",
    "capture_hint",
    "capture_query",
    "pdf_url",
    "doi",
    "title",
    "verification_status",
    "source_article_title",
    "source_article_dir",
    "source_reference_index",
    "manual_reason",
    "approval_status",
    "recommended_cli_args_json",
]

REFERENCE_PROVENANCE_FIELDS = [
    "subquestion_group_slug",
    "subquestion_group_title",
    "subquestion_id",
    "subquestion_slug",
    "query_round",
    "query_family",
    "rank",
    "selection_status",
    "approval_status",
    "agent_score",
    "agent_reason",
    "action",
    "capture_hint",
    "capture_query",
    "pdf_url",
    "doi",
    "title",
    "source_article_title",
    "source_article_dir",
    "source_reference_index",
    "citation_contexts",
    "reference_text",
    "verification_status",
    "manual_reason",
]


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def token_set(text: str) -> set[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}|[\u4e00-\u9fff]{2,}", text or "")
    stop = {
        "the", "and", "for", "with", "from", "into", "about", "using", "based",
        "study", "paper", "article", "review", "method", "methods", "results",
        "conclusion",
    }
    return {w.lower() for w in words if w.lower() not in stop and len(w) > 2}


def doi_from_text(text: str) -> str:
    match = re.search(r"\b(10\.\d{4,9}/[^\s\"<>]+)", text or "", re.I)
    if not match:
        return ""
    doi = match.group(1).rstrip(".,;:)］】")
    return doi


def clean_reference_text(text: str) -> str:
    text = normalize_ws(text)
    text = re.sub(r"^\s*-?\s*(\[\d+\]|\d+[\).])?\s*\.?\s*", "", text)
    text = text.strip(" .;")
    return text


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_summary(run_dir: Path) -> list[dict[str, Any]]:
    json_path = run_dir / "run-summary.json"
    if json_path.exists():
        data = read_json(json_path)
        if isinstance(data, list):
            return data
    csv_path = run_dir / "run-summary.csv"
    if csv_path.exists():
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    return []


def find_run_root(path: Path) -> Path:
    current = path.resolve()
    if current.is_file():
        current = current.parent
    for candidate in [current, *current.parents]:
        if (candidate / "run-summary.json").exists() or (candidate / "run-summary.csv").exists():
            return candidate
    return current


def selected_subquestion_id(path: Path, run_root: Path) -> str:
    subquestion_json = path / "subquestion.json"
    if subquestion_json.exists():
        data = read_json(subquestion_json)
        if isinstance(data, dict):
            return str(data.get("subquestion_id") or "")
    try:
        rel = path.resolve().relative_to((run_root / "subquestions").resolve())
        if rel.parts:
            return rel.parts[0]
    except ValueError:
        pass
    return ""


def filter_summary_for_scope(rows: list[dict[str, Any]], scope_dir: Path, run_root: Path) -> list[dict[str, Any]]:
    subquestion_id = selected_subquestion_id(scope_dir, run_root)
    if not subquestion_id:
        return rows
    scoped: list[dict[str, Any]] = []
    scope_resolved = scope_dir.resolve()
    for row in rows:
        if str(row.get("subquestion_id") or "") == subquestion_id:
            scoped.append(row)
            continue
        article_dir = Path(str(row.get("article_dir") or ""))
        try:
            article_dir.resolve().relative_to(scope_resolved)
            scoped.append(row)
        except Exception:
            pass
    return scoped


def read_query_rounds(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "query-rounds.json"
    if path.exists():
        data = read_json(path)
        if isinstance(data, list):
            return data
    return [{
        "round": 1,
        "query_family": "all",
        "claim_subquestion": "Rank references against the run topic.",
        "queries": [run_dir.name],
    }]


def split_reference_section(text: str) -> tuple[str, str]:
    marker = re.search(r"(?im)^#{1,6}\s*(references|bibliography)\s*$", text)
    if marker:
        return text[:marker.start()], text[marker.end():]
    pos = text.lower().rfind("references")
    if pos >= 0:
        return text[:pos], text[pos:]
    return text, ""


def reference_index_from_text(text: str) -> str:
    match = re.match(r"^\s*-?\s*(?:\[(\d+)\]|(\d+)[).])\s+", text or "")
    if not match:
        return ""
    return match.group(1) or match.group(2) or ""


def citation_contexts_for_indices(body: str, indices: set[str], window: int = 260) -> dict[str, list[str]]:
    contexts: dict[str, list[str]] = {idx: [] for idx in indices}
    if not body or not indices:
        return contexts
    for idx in indices:
        escaped = re.escape(idx)
        patterns = [
            rf"\[(?:[0-9,\-\s]+,)?\s*{escaped}\s*(?:[,;\-]\s*[0-9]+)*\]",
            rf"\((?:[0-9,\-\s]+,)?\s*{escaped}\s*(?:[,;\-]\s*[0-9]+)*\)",
        ]
        seen: set[str] = set()
        for pattern in patterns:
            for match in re.finditer(pattern, body):
                start = max(0, match.start() - window)
                end = min(len(body), match.end() + window)
                context = normalize_ws(body[start:end])
                if context and context not in seen:
                    contexts[idx].append(context)
                    seen.add(context)
                if len(contexts[idx]) >= 6:
                    break
            if len(contexts[idx]) >= 6:
                break
    return contexts


def extract_markdown_reference_entries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    body, tail = split_reference_section(text)
    refs: list[dict[str, Any]] = []
    current: list[str] = []
    current_index = ""
    for raw in tail.splitlines():
        line = raw.strip()
        if not line:
            continue
        if re.match(r"^#{1,6}\s+\S", line) and refs:
            break
        if re.match(r"^(-\s*)?(\[\d+\]|\d+[\).])\s+", line):
            if current:
                refs.append({
                    "reference_text": clean_reference_text(" ".join(current)),
                    "source_reference_index": current_index,
                })
            current_index = reference_index_from_text(line)
            current = [line]
        elif current:
            current.append(line)
    if current:
        refs.append({
            "reference_text": clean_reference_text(" ".join(current)),
            "source_reference_index": current_index,
        })
    refs = [r for r in refs if len(str(r.get("reference_text") or "")) > 30][:300]
    context_map = citation_contexts_for_indices(body, {str(r.get("source_reference_index") or "") for r in refs if r.get("source_reference_index")})
    for ref in refs:
        idx = str(ref.get("source_reference_index") or "")
        contexts = context_map.get(idx) or []
        ref["citation_contexts"] = " || ".join(contexts[:3])
        ref["citation_count"] = len(contexts)
    return refs


def extract_markdown_references(path: Path) -> list[str]:
    return [str(x.get("reference_text") or "") for x in extract_markdown_reference_entries(path)]


def collect_references(run_dir: Path, summary_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for row in summary_rows:
        if row.get("status") != "captured":
            continue
        article_dir = Path(str(row.get("article_dir") or ""))
        if not article_dir.exists():
            continue
        source_title = row.get("title") or article_dir.name
        ref_values: list[str] = []
        ref_entries: list[dict[str, Any]] = []
        ref_json = article_dir / "references.json"
        if ref_json.exists():
            data = read_json(ref_json)
            if isinstance(data, list):
                ref_values = [str(x) for x in data]
        if not ref_values:
            for candidate in [
                article_dir / "mineru" / "fulltext.md",
                article_dir / "fulltext.md",
                article_dir / "captured-fulltext.md",
            ]:
                ref_entries = extract_markdown_reference_entries(candidate)
                if ref_entries:
                    break
        if not ref_entries:
            ref_entries = [
                {"reference_text": clean_reference_text(ref), "source_reference_index": idx, "citation_count": 0, "citation_contexts": ""}
                for idx, ref in enumerate(ref_values, start=1)
            ]
        for entry in ref_entries:
            ref = clean_reference_text(str(entry.get("reference_text") or ""))
            if not ref:
                continue
            source_ref_index = entry.get("source_reference_index") or ""
            refs.append({
                "subquestion_group_slug": row.get("subquestion_group_slug") or "general",
                "subquestion_group_title": row.get("subquestion_group_title") or "General",
                "subquestion_id": row.get("subquestion_id") or "",
                "subquestion_slug": row.get("subquestion_slug") or "",
                "reference_text": ref,
                "source_article_title": source_title,
                "source_article_dir": str(article_dir),
                "source_reference_index": source_ref_index,
                "parsed_doi": doi_from_text(ref),
                "citation_count": entry.get("citation_count") or 0,
                "citation_contexts": entry.get("citation_contexts") or "",
            })
    return refs


def truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "是", "worth_close_reading", "close_read"}


def note_field(text: str, field: str) -> str:
    match = re.search(rf"(?im)^\s*-?\s*{re.escape(field)}\s*[:：]\s*(.+?)\s*$", text or "")
    return normalize_ws(match.group(1)) if match else ""


def note_score(text: str, field: str) -> float | None:
    try:
        return float(note_field(text, field))
    except Exception:
        return None


def article_marked_close_read(row: dict[str, Any], note_text: str) -> bool:
    try:
        score = float(str(row.get("worth_close_reading_score_0_to_5") or "").strip())
    except Exception:
        score = note_score(note_text, "worth_close_reading_score_0_to_5")
    worth = truthy(row.get("worth_close_reading")) or truthy(note_field(note_text, "worth_close_reading"))
    return worth and (score is None or score >= 4)


def read_agent_recommendation_rows(article_dir: Path) -> list[dict[str, Any]]:
    json_path = article_dir / "recommended-references.json"
    csv_path = article_dir / "recommended-references.csv"
    rows: list[dict[str, Any]] = []
    if json_path.exists():
        try:
            data = read_json(json_path)
            if isinstance(data, list):
                rows = [item for item in data if isinstance(item, dict)]
            elif isinstance(data, dict):
                values = data.get("references") or data.get("recommended_references") or []
                if isinstance(values, list):
                    rows = [item for item in values if isinstance(item, dict)]
        except Exception:
            rows = []
    if not rows and csv_path.exists():
        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        except Exception:
            rows = []
    return rows


def collect_agent_picked_references(run_root: Path, summary_rows: list[dict[str, Any]], refs_per_article: int) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for row in summary_rows:
        if row.get("status") != "captured":
            continue
        article_dir = Path(str(row.get("article_dir") or ""))
        if not article_dir.exists():
            continue
        note_text = ""
        note_path = article_dir / "reading-note-zh.md"
        if note_path.exists():
            note_text = note_path.read_text(encoding="utf-8", errors="replace")
        article_marked_close = article_marked_close_read(row, note_text)
        picked = read_agent_recommendation_rows(article_dir)
        if not picked:
            continue
        if not article_marked_close:
            marker = article_dir / "recommended-references.allow"
            if not marker.exists():
                continue
        selected = picked if refs_per_article <= 0 else picked[:refs_per_article]
        for idx, item in enumerate(selected, start=1):
            ref_text = clean_reference_text(
                str(
                    item.get("reference_text")
                    or item.get("reference")
                    or item.get("citation")
                    or item.get("title")
                    or ""
                )
            )
            if not ref_text:
                continue
            reason = normalize_ws(str(item.get("reason") or item.get("agent_reason") or item.get("why") or ""))
            contexts = normalize_ws(str(item.get("citation_context") or item.get("citation_contexts") or item.get("context") or ""))
            relation = normalize_ws(str(item.get("relation_to_subquestion") or item.get("relation") or ""))
            refs.append({
                "subquestion_group_slug": row.get("subquestion_group_slug") or "general",
                "subquestion_group_title": row.get("subquestion_group_title") or "General",
                "subquestion_id": row.get("subquestion_id") or "",
                "subquestion_slug": row.get("subquestion_slug") or "",
                "reference_text": ref_text,
                "source_article_title": row.get("title") or article_dir.name,
                "source_article_dir": str(article_dir),
                "source_reference_index": item.get("source_reference_index") or item.get("reference_index") or idx,
                "parsed_doi": doi_from_text(ref_text) or str(item.get("doi") or ""),
                "citation_count": item.get("citation_count") or 1,
                "citation_contexts": contexts,
                "agent_reason": " | ".join(x for x in [reason, relation] if x),
                "agent_score": item.get("agent_score") or item.get("score") or "",
            })
    return refs


def crossref_query_bibliographic(reference_text: str) -> dict[str, Any]:
    query = normalize_ws(reference_text)[:700]
    if not query:
        return {}
    url = "https://api.crossref.org/works?" + urlencode({"query.bibliographic": query, "rows": "1"})
    request = Request(url, headers={"User-Agent": "literature-loop-capture/1.0"})
    try:
        with urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        items = ((data.get("message") or {}).get("items") or [])
        return items[0] if items else {}
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
        return {}


def authors_from_crossref(work: dict[str, Any]) -> str:
    authors = []
    for author in work.get("author") or []:
        name = " ".join(str(author.get(k) or "") for k in ["given", "family"]).strip()
        if name:
            authors.append(name)
    return "; ".join(authors[:20])


def year_from_crossref(work: dict[str, Any]) -> str:
    for key in ["published-print", "published-online", "published", "issued", "created"]:
        parts = ((work.get(key) or {}).get("date-parts") or [])
        if parts and parts[0]:
            return str(parts[0][0])
    return ""


def title_from_crossref(work: dict[str, Any]) -> str:
    title = work.get("title") or []
    return normalize_ws(str(title[0])) if title else ""


def bibliographic_match_ok(reference_text: str, work: dict[str, Any]) -> bool:
    title = title_from_crossref(work)
    if not title:
        return False
    ref_terms = token_set(reference_text)
    title_terms = token_set(title)
    if not title_terms:
        return False
    overlap = len(ref_terms & title_terms)
    return overlap >= 3 or overlap / max(1, len(title_terms)) >= 0.45


def container_from_crossref(work: dict[str, Any]) -> str:
    values = work.get("container-title") or []
    return normalize_ws(str(values[0])) if values else ""


def pdf_url_from_crossref(work: dict[str, Any]) -> str:
    for link in work.get("link") or []:
        if not isinstance(link, dict):
            continue
        url = normalize_ws(str(link.get("URL") or link.get("url") or ""))
        content_type = normalize_ws(str(link.get("content-type") or link.get("content_type") or "")).lower()
        intended = normalize_ws(str(link.get("intended-application") or link.get("intended_application") or "")).lower()
        if url and ("pdf" in content_type or "pdf" in intended or "/pdf" in url.lower() or url.lower().endswith(".pdf")):
            return url
    return ""


def landing_url_from_crossref(work: dict[str, Any]) -> str:
    return normalize_ws(str(work.get("URL") or work.get("resource", {}).get("primary", {}).get("URL") or ""))


def verify_reference(ref: dict[str, Any], use_crossref: bool) -> dict[str, Any]:
    if not use_crossref:
        return {}
    doi = ref.get("parsed_doi") or ""
    if doi:
        work = discovery.crossref_work_by_doi(doi)
        if work:
            if bibliographic_match_ok(ref.get("reference_text") or "", work):
                status = "crossref_by_doi"
            else:
                status = "doi_crossref_title_mismatch"
        else:
            return {
                "crossref_doi": "",
                "crossref_title": "",
                "crossref_authors": "",
                "crossref_year": "",
                "crossref_container": "",
                "crossref_type": "",
                "crossref_pdf_url": "",
                "crossref_landing_url": "",
                "verification_status": "doi_unverified_by_crossref",
            }
    else:
        work = crossref_query_bibliographic(ref.get("reference_text") or "")
        if work and bibliographic_match_ok(ref.get("reference_text") or "", work):
            status = "crossref_by_bibliographic"
        else:
            work = {}
            status = "unverified"
    return {
        "crossref_doi": work.get("DOI") or "",
        "crossref_title": title_from_crossref(work),
        "crossref_authors": authors_from_crossref(work),
        "crossref_year": year_from_crossref(work),
        "crossref_container": container_from_crossref(work),
        "crossref_type": work.get("type") or "",
        "crossref_pdf_url": pdf_url_from_crossref(work),
        "crossref_landing_url": landing_url_from_crossref(work),
        "verification_status": status,
    }


def capture_hint(row: dict[str, Any]) -> tuple[str, str, str]:
    doi = (row.get("crossref_doi") or row.get("parsed_doi") or "").lower()
    mismatch = row.get("verification_status") == "doi_crossref_title_mismatch"
    title = ("" if mismatch else row.get("crossref_title")) or row.get("reference_text") or ""
    container = (row.get("crossref_container") or "").lower()
    text = " ".join([doi, title.lower(), container])
    if mismatch:
        return ("manual-or-publisher-search", title, "Crossref DOI lookup returned a title that does not match the reference text; reconcile metadata/source manually before capture.")
    if doi.startswith("10.1126") or "science.org" in text or container in {
        "science",
        "science advances",
        "science immunology",
        "science robotics",
        "science signaling",
        "science translational medicine",
    }:
        return ("supplemental-pdf-science", title, "Science-family row; route through supplemental_followup.py and PDF follow-up, not the structured publisher queue.")
    if doi.startswith("10.1038") or "nature.com" in text or "nature portfolio" in text or container == "nature" or container.startswith("nature ") or container.startswith("npj ") or container == "scientific reports":
        return ("supplemental-pdf-nature", title, "Nature-family row; route through supplemental_followup.py and PDF follow-up, not the structured publisher queue.")
    if "arxiv" in text:
        return ("supplemental-pdf-arxiv", title, "arXiv row; route through supplemental_followup.py and PDF follow-up, not the structured publisher queue.")
    if doi.startswith("10.1021") or "acs" in text:
        return ("publisher-acs", title, "Likely ACS; use publisher-authenticated publisher capture if authorized.")
    if doi.startswith("10.1002") or doi.startswith("10.1111") or "wiley" in text:
        return ("publisher-wiley", title, "Likely Wiley; use publisher-authenticated publisher capture if authorized.")
    if doi.startswith("10.1016") or "elsevier" in text or "sciencedirect" in text:
        return ("publisher-elsevier", title, "Likely Elsevier/ScienceDirect; use publisher-authenticated publisher capture if authorized.")
    if doi.startswith("10.1007") or "springer" in text:
        return ("publisher-springer", title, "Likely Springer; use publisher-authenticated publisher capture if authorized.")
    if row.get("crossref_pdf_url"):
        return ("manual-or-publisher-search", title, "Non-core publisher with a Crossref PDF URL; PDF fallback is out of scope for OpenCLI structured capture.")
    return ("manual-or-publisher-search", title, "No supported publisher route could be inferred; inspect manually before any capture.")


def score_reference(ref: dict[str, Any], round_info: dict[str, Any], source_counts: Counter[str]) -> float:
    query_text = " ".join(str(x) for x in round_info.get("queries") or [])
    context = " ".join([str(round_info.get("claim_subquestion") or ""), query_text])
    context_terms = token_set(context)
    ref_text = " ".join([
        ref.get("citation_contexts") or "",
        ref.get("reference_text") or "",
        ref.get("crossref_title") or "",
        ref.get("crossref_container") or "",
    ])
    ref_terms = token_set(ref_text)
    citation_terms = token_set(ref.get("citation_contexts") or "")
    context_overlap = len(context_terms & citation_terms)
    reference_overlap = len(context_terms & ref_terms)
    score = float(context_overlap * 5 + reference_overlap * 2)
    try:
        score += min(3.0, int(ref.get("citation_count") or 0) * 0.5)
    except ValueError:
        pass
    if ref.get("parsed_doi") or ref.get("crossref_doi"):
        score += 1.0
    try:
        year = int(ref.get("crossref_year") or 0)
        if year >= 2023:
            score += 1.5
        elif year >= 2020:
            score += 0.75
    except ValueError:
        pass
    key = (ref.get("crossref_doi") or ref.get("parsed_doi") or ref.get("reference_text") or "").lower()
    score += min(2.0, max(0, source_counts.get(key, 1) - 1) * 0.5)
    return score


def score_basis_text() -> str:
    return (
        "python_pre_rank: citation-context token overlap * 5 plus reference-text token overlap * 2; "
        "+0.5 per in-text citation context up to +3; +1 if DOI is present; "
        "+1.5 if Crossref publication year metadata >= 2023 or +0.75 if >= 2020; "
        "+0.5 per repeated citation across captured sources up to +2. "
        "Agent must confirm final relevance after reading notes/full text."
    )


def dedupe_key(ref: dict[str, Any]) -> str:
    doi = (ref.get("crossref_doi") or ref.get("parsed_doi") or "").lower().strip()
    if doi:
        return "doi:" + doi
    return "text:" + re.sub(r"[^a-z0-9]+", "", (ref.get("reference_text") or "").lower())[:100]


def slugify(value: str, fallback: str = "subquestion") -> str:
    value = normalize_ws(value)
    value = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", value, flags=re.UNICODE)
    value = value.strip("._")
    if not value:
        value = fallback
    return value[:90]


def source_bucket_from_hint(hint: str) -> str:
    if hint.startswith("publisher-"):
        return hint.replace("publisher-", "", 1)
    return "manual_hold"


def subquestion_folder_for_row(run_root: Path, row: dict[str, Any]) -> Path:
    subquestion_id = str(row.get("subquestion_id") or "")
    if subquestion_id:
        group_slug = str(row.get("subquestion_group_slug") or "general")
        return run_root / "subquestions" / group_slug / subquestion_id
    source_dir = Path(str(row.get("source_article_dir") or ""))
    for parent in [source_dir, *source_dir.parents]:
        if parent.name == "subquestions":
            break
        if parent.parent.name == "subquestions":
            return parent
    slug = slugify(f"round-{row.get('query_round') or 'x'}_{row.get('query_family') or row.get('claim_subquestion') or ''}")
    return run_root / "subquestions" / slug


def write_csv_json(csv_path: Path, json_path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    fields = fields or FOLLOWUP_FIELDS
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def approved_for_capture(row: dict[str, Any]) -> bool:
    status = normalize_ws(str(row.get("approval_status") or "")).lower()
    return status in {
        "approved",
        "agent_approved",
        "approved_for_capture",
        "capture_approved",
        "agent_picked_approved",
        "reading_note_approved",
    }


def followup_capture_action(row: dict[str, Any]) -> tuple[str, str]:
    hint = str(row.get("capture_hint") or "")
    verification = str(row.get("verification_status") or "")
    if verification == "doi_crossref_title_mismatch":
        return "manual_hold", "Crossref DOI/title mismatch; reconcile metadata/source manually before capture."
    if not approved_for_capture(row):
        return "manual_hold", "Not approved by reading-note recommendation or an explicit agent approval."
    if hint in {"publisher-acs", "publisher-wiley", "publisher-elsevier", "publisher-springer"}:
        return "capture", ""
    if hint.startswith("supplemental-pdf-"):
        return "manual_hold", "Routed by supplemental_followup.py into the PDF follow-up queue, not this structured Publisher queue."
    return "manual_hold", "Outside configured publisher-authenticated publisher routes, or source cannot be inferred."


def recommended_capture_args(row: dict[str, Any], run_root: Path) -> list[str]:
    action, _ = followup_capture_action(row)
    if action != "capture":
        return []
    query = normalize_ws(str(row.get("capture_query") or row.get("crossref_title") or row.get("reference_text") or ""))
    args = [
        "python",
        "literature-loop-capture\\scripts\\incremental_capture.py",
        "--claim", query,
        "--query", query,
        "--rounds", "1",
        "--max-queries", "1",
        "--max-pages", "1",
        "--max-results-per-page", "20",
        "--discovery-only",
        "--discovery-backend", "opencli",
        "--opencli-session", "lit",
        "--write-query-refinement-packets",
        "--no-openalex-grounding",
        "--include-structured-publishers",
        "--existing-run-dir", str(run_root),
        "--subquestion-id", str(row.get("subquestion_id") or ""),
        "--subquestion-slug", str(row.get("subquestion_slug") or ""),
        "--subquestion-group-slug", str(row.get("subquestion_group_slug") or "general"),
        "--subquestion-group-title", str(row.get("subquestion_group_title") or "General"),
        "--subquestion-text", str(row.get("claim_subquestion") or ""),
        "--query-family", "follow-up-reference",
        "--capture-depth", "2",
        "--parent-article-dir", str(row.get("source_article_dir") or ""),
        "--parent-reference-index", str(row.get("source_reference_index") or ""),
    ]
    hint = str(row.get("capture_hint") or "")
    if hint == "publisher-springer":
        args.append("--include-springer")
    else:
        args.append("--no-springer")
    return args


def write_followup_capture_queue(out_dir: Path, rows: list[dict[str, Any]], run_root: Path) -> None:
    queue_rows: list[dict[str, Any]] = []
    for row in rows:
        action, manual_reason = followup_capture_action(row)
        title = row.get("crossref_title") or row.get("capture_query") or row.get("reference_text") or ""
        doi = row.get("crossref_doi") or row.get("parsed_doi") or ""
        pdf_url = row.get("pdf_url") or row.get("crossref_pdf_url") or ""
        queue_rows.append({
            "subquestion_group_slug": row.get("subquestion_group_slug") or "general",
            "subquestion_group_title": row.get("subquestion_group_title") or "General",
            "subquestion_id": row.get("subquestion_id") or "",
            "subquestion_slug": row.get("subquestion_slug") or "",
            "query_round": row.get("query_round") or "",
            "query_family": row.get("query_family") or "",
            "claim_subquestion": row.get("claim_subquestion") or "",
            "rank": row.get("rank") or "",
            "action": action,
            "capture_depth": 2,
            "follow_tertiary_references": "false",
            "capture_hint": row.get("capture_hint") or "",
            "capture_query": row.get("capture_query") or title,
            "pdf_url": pdf_url,
            "doi": doi,
            "title": title,
            "verification_status": row.get("verification_status") or "",
            "source_article_title": row.get("source_article_title") or "",
            "source_article_dir": row.get("source_article_dir") or "",
            "source_reference_index": row.get("source_reference_index") or "",
            "manual_reason": manual_reason,
            "approval_status": row.get("approval_status") or "pending_agent",
            "recommended_cli_args_json": json.dumps(recommended_capture_args(row, run_root), ensure_ascii=False),
        })
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "followup-capture-queue.json"
    csv_path = out_dir / "followup-capture-queue.csv"
    md_path = out_dir / "followup-capture-queue.md"
    json_path.write_text(json.dumps(queue_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CAPTURE_QUEUE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(queue_rows)
    manual_rows = [row for row in queue_rows if row.get("action") == "manual_hold"]
    (out_dir / "manual-reference-hold.json").write_text(json.dumps(manual_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out_dir / "manual-reference-hold.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CAPTURE_QUEUE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(manual_rows)
    md = [
        "# Follow-up Capture Queue",
        "",
        "This is a second-level capture queue generated from reading-note recommended references.",
        "In `agent-picked` mode, reading-note recommendation is the intellectual approval; this stage only dedupes, reconciles metadata, and routes capture/manual holds.",
        "Rows with `action=capture` should be sent back through structured Publisher discovery/capture or approved PDF follow-up and then receive normal `reading-note-zh.md` notes.",
        "Rows with `action=manual_hold` are outside configured routes or need manual metadata reconciliation/source inspection.",
        "Do not run reference follow-up again on these second-level captures unless the user explicitly asks for deeper reference chasing.",
        "",
    ]
    for item in queue_rows:
        md.extend([
            f"## Round {item['query_round']} rank {item['rank']}: {item['title'][:140]}",
            "",
            f"- Action: {item['action']}",
            f"- Approval status: {item['approval_status']}",
            f"- Capture hint: {item['capture_hint']}",
            f"- DOI: `{item['doi']}`",
            f"- Query: `{item['capture_query']}`",
            f"- PDF URL: `{item.get('pdf_url') or ''}`",
            f"- Manual reason: {item['manual_reason']}",
            f"- CLI args JSON: `{item['recommended_cli_args_json']}`",
            "",
        ])
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")


def render_followup_markdown(rows: list[dict[str, Any]], title: str, per_subquestion_count: int, label: str = "Items per subquestion") -> str:
    md = ["# " + title, "", f"- Generated: {datetime.now().isoformat(timespec='seconds')}", f"- {label}: {per_subquestion_count}", ""]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = f"{row.get('query_round') or ''}::{row.get('claim_subquestion') or ''}"
        grouped[key].append(row)
    for _, items in grouped.items():
        if not items:
            continue
        first = items[0]
        md.extend([
            f"## Round {first.get('query_round') or ''}: {first.get('query_family') or ''}",
            "",
            f"- Subquestion: {first.get('claim_subquestion') or ''}",
            f"- Query: `{first.get('query_text') or ''}`",
            "",
        ])
        for row in items:
            md.extend([
                f"### {row.get('rank')}. {row.get('crossref_title') or row.get('reference_text')[:120]}",
                "",
                f"- Score: {row.get('score')}",
                f"- Agent status: {row.get('agent_assessment_status') or ''}",
                f"- Selection status: {row.get('selection_status') or ''}",
                f"- Citation count: {row.get('citation_count') or 0}",
                f"- DOI: `{row.get('crossref_doi') or row.get('parsed_doi') or ''}`",
                f"- Year/source: {row.get('crossref_year') or ''}; {row.get('crossref_container') or ''}",
                f"- Verification: {row.get('verification_status') or 'not_checked'}",
                f"- Capture hint: {row.get('capture_hint')}; query `{row.get('capture_query') or ''}`",
                f"- From: {row.get('source_article_title') or ''}",
                f"- Citation contexts: {row.get('citation_contexts') or ''}",
                "",
            ])
    return "\n".join(md) + "\n"


def final_selection_markdown_title(rows: list[dict[str, Any]]) -> tuple[str, str, int]:
    if rows and all(approved_for_capture(row) for row in rows):
        return ("Reading-Note Approved Reference Ledger", "Approved rows", len(rows))
    return ("Final Reference Selection Draft", "Draft final top N", len(rows))


def final_selection_draft(rows: list[dict[str, Any]], final_top_n: int) -> list[dict[str, Any]]:
    draft: list[dict[str, Any]] = []
    approved_rows = [row for row in rows if approved_for_capture(row)]
    source_rows = approved_rows if approved_rows else rows[:final_top_n]
    for row in source_rows:
        item = dict(row)
        item["agent_score"] = item.get("agent_score") or ""
        item["agent_reason"] = item.get("agent_reason") or ""
        if approved_for_capture(item):
            item["agent_assessment_status"] = item.get("agent_assessment_status") or "agent_picked_during_reading_note"
            item["selection_status"] = item.get("selection_status") or "agent_picked_saved_for_capture"
            item["approval_status"] = item.get("approval_status") or "agent_picked_approved"
        else:
            item["agent_assessment_status"] = "pending_subagent_final_selection"
            item["selection_status"] = "draft_candidate_not_approved"
            item["approval_status"] = "pending_agent"
        draft.append(item)
    return draft


def provenance_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        action, manual_reason = followup_capture_action(row)
        out.append({
            "subquestion_group_slug": row.get("subquestion_group_slug") or "general",
            "subquestion_group_title": row.get("subquestion_group_title") or "General",
            "subquestion_id": row.get("subquestion_id") or "",
            "subquestion_slug": row.get("subquestion_slug") or "",
            "query_round": row.get("query_round") or "",
            "query_family": row.get("query_family") or "",
            "rank": row.get("rank") or "",
            "selection_status": row.get("selection_status") or "",
            "approval_status": row.get("approval_status") or "pending_agent",
            "agent_score": row.get("agent_score") or "",
            "agent_reason": row.get("agent_reason") or "",
            "action": action,
            "capture_hint": row.get("capture_hint") or "",
            "capture_query": row.get("capture_query") or "",
            "pdf_url": row.get("pdf_url") or row.get("crossref_pdf_url") or "",
            "doi": row.get("crossref_doi") or row.get("parsed_doi") or "",
            "title": row.get("crossref_title") or row.get("capture_query") or row.get("reference_text") or "",
            "source_article_title": row.get("source_article_title") or "",
            "source_article_dir": row.get("source_article_dir") or "",
            "source_reference_index": row.get("source_reference_index") or "",
            "citation_contexts": row.get("citation_contexts") or "",
            "reference_text": row.get("reference_text") or "",
            "verification_status": row.get("verification_status") or "",
            "manual_reason": manual_reason,
        })
    return out


def render_provenance_markdown(rows: list[dict[str, Any]]) -> str:
    md = ["# Reference Provenance", "", "Each row is traceable to a parent captured article and reference index.", ""]
    for row in rows:
        md.extend([
            f"## {row.get('rank')}. {str(row.get('title') or '')[:140]}",
            "",
            f"- Action: {row.get('action')}",
            f"- Approval status: {row.get('approval_status')}",
            f"- DOI: `{row.get('doi') or ''}`",
            f"- PDF URL: `{row.get('pdf_url') or ''}`",
            f"- Parent article: {row.get('source_article_title') or ''}",
            f"- Parent article dir: `{row.get('source_article_dir') or ''}`",
            f"- Parent reference index: `{row.get('source_reference_index') or ''}`",
            f"- Citation contexts: {row.get('citation_contexts') or ''}",
            "",
        ])
    return "\n".join(md) + "\n"


def write_per_subquestion_outputs(run_root: Path, candidate_rows: list[dict[str, Any]], candidate_pool_size: int, final_top_n: int) -> list[dict[str, Any]]:
    grouped: dict[Path, list[dict[str, Any]]] = defaultdict(list)
    final_rows: list[dict[str, Any]] = []
    for row in candidate_rows:
        grouped[subquestion_folder_for_row(run_root, row)].append(row)
    for folder, items in grouped.items():
        folder.mkdir(parents=True, exist_ok=True)
        write_csv_json(folder / "reference-candidates.csv", folder / "reference-candidates.json", items)
        (folder / "reference-candidates.md").write_text(
            render_followup_markdown(items, "Reference Candidates by Subquestion", candidate_pool_size, "Candidate pool size"),
            encoding="utf-8",
        )
        write_csv_json(folder / "reference-followups.csv", folder / "reference-followups.json", items)
        (folder / "reference-followups.md").write_text(
            render_followup_markdown(items, "Reference Follow-ups by Subquestion (Compatibility Copy)", candidate_pool_size, "Candidate pool size"),
            encoding="utf-8",
        )
        draft = final_selection_draft(items, final_top_n)
        final_rows.extend(draft)
        write_csv_json(folder / "final-reference-selection.csv", folder / "final-reference-selection.json", draft)
        final_title, final_label, final_count = final_selection_markdown_title(draft)
        (folder / "final-reference-selection.md").write_text(
            render_followup_markdown(draft, final_title, final_count, final_label),
            encoding="utf-8",
        )
        provenance = provenance_rows(draft)
        write_csv_json(folder / "reference-provenance.csv", folder / "reference-provenance.json", provenance, REFERENCE_PROVENANCE_FIELDS)
        (folder / "reference-provenance.md").write_text(render_provenance_markdown(provenance), encoding="utf-8")
        write_followup_capture_queue(folder, draft, run_root)
    return final_rows


def write_outputs(run_dir: Path, candidate_rows: list[dict[str, Any]], candidate_pool_size: int, final_top_n: int, run_root: Path | None = None) -> list[dict[str, Any]]:
    run_root = run_root or run_dir
    def paths(prefix: str, suffix: str = "") -> tuple[Path, Path, Path]:
        return (
            run_dir / f"{prefix}{suffix}.json",
            run_dir / f"{prefix}{suffix}.csv",
            run_dir / f"{prefix}{suffix}.md",
        )

    json_path, csv_path, md_path = paths("reference-candidates")
    try:
        write_csv_json(csv_path, json_path, candidate_rows)
    except PermissionError:
        suffix = "." + datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path, csv_path, md_path = paths("reference-candidates", suffix)
        write_csv_json(csv_path, json_path, candidate_rows)
    md = render_followup_markdown(candidate_rows, "Reference Candidates", candidate_pool_size, "Candidate pool size")
    try:
        md_path.write_text(md, encoding="utf-8")
    except PermissionError:
        fallback = run_dir / f"reference-candidates.{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        fallback.write_text(md, encoding="utf-8")
    # Backward-compatible aggregate file names. These are candidate rows, not final selections.
    compat_json, compat_csv, compat_md = paths("reference-followups")
    write_csv_json(compat_csv, compat_json, candidate_rows)
    compat_md.write_text(render_followup_markdown(candidate_rows, "Reference Follow-ups (Compatibility Copy)", candidate_pool_size, "Candidate pool size"), encoding="utf-8")
    return write_per_subquestion_outputs(run_root, candidate_rows, candidate_pool_size, final_top_n)


def write_overview_materials(run_dir: Path, summary_rows: list[dict[str, Any]], followup_rows: list[dict[str, Any]]) -> None:
    captured = [r for r in summary_rows if r.get("status") == "captured"]
    source_counts = Counter(str(r.get("publisher") or "") for r in captured)
    article_types = Counter(str(r.get("article_type") or "") for r in captured)
    top_followups = sorted(followup_rows, key=lambda r: float(r.get("score") or 0), reverse=True)[:10]
    lines = [
        "# Overview Materials",
        "",
        f"- Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"- Captured articles: {len(captured)}",
        f"- Sources: {', '.join(f'{k}={v}' for k, v in sorted(source_counts.items())) or 'none'}",
        f"- Article types: {', '.join(f'{k}={v}' for k, v in sorted(article_types.items())) or 'none'}",
        f"- Follow-up reference candidates: {len(followup_rows)}",
        "",
        "This file is source material only. The final `overview.md` must be written by Codex after reading the captured full text, figures, tables, and `reading-note-zh.md` files.",
        "",
        "## Captured Evidence",
        "",
    ]
    for row in captured[:30]:
        lines.append(f"- {row.get('year') or ''} | {row.get('publisher') or ''} | {row.get('title') or ''}")
    lines.extend(["", "## Most Relevant Follow-up References", ""])
    for row in top_followups:
        lines.append(
            f"- Round {row.get('query_round')}: {row.get('crossref_title') or row.get('reference_text')[:120]} "
            f"({row.get('crossref_year') or 'n.d.'}; {row.get('capture_hint')})"
        )
    lines.extend([
        "",
        "## Agent Overview Checklist",
        "",
        "- Summarize the current state of the user question.",
        "- Map the main method families, resources, datasets, and evaluation practices.",
        "- Identify new methods or technical directions that appear across the captured papers.",
        "- Describe unresolved limitations, weak evidence, engineering bottlenecks, and conflicting findings.",
        "- Propose plausible innovation opportunities or solution routes, while separating evidence-backed claims from hypotheses.",
        "- Cite captured article folders and follow-up reference candidates as provenance.",
    ])
    (run_dir / "overview-materials.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    requires_agent = [
        "# OVERVIEW_REQUIRES_AGENT",
        "",
        "Write `overview.md` only after Codex has read the captured article folders and the Chinese reading notes.",
        "",
        "Required structure:",
        "",
        "1. 问题现状",
        "2. 已有方法/资源/证据谱系",
        "3. 新方法或新趋势",
        "4. 主要不足和未解决问题",
        "5. 可创新方向或可行解决路线",
        "6. 值得继续抓取/精读的 references",
        "",
        "Use `overview-materials.md`, `run-summary.csv`, root `reference-candidates.csv`, `final-reference-selection.csv`, `reference-provenance.csv`, `subquestions/*/*/reference-candidates.*`, and `subquestions/*/*/subquestion-summary-zh.md` as supporting material.",
    ]
    (run_dir / "OVERVIEW_REQUIRES_AGENT.md").write_text("\n".join(requires_agent) + "\n", encoding="utf-8")


def build_followups(
    input_dir: Path,
    candidate_pool_size: int,
    final_top_n: int,
    use_crossref: bool,
    crossref_pool_size: int,
) -> list[dict[str, Any]]:
    run_root = find_run_root(input_dir)
    summary_rows = filter_summary_for_scope(read_summary(run_root), input_dir, run_root)
    rounds = read_query_rounds(run_root)
    scoped_subquestion_id = selected_subquestion_id(input_dir, run_root)
    if scoped_subquestion_id:
        rounds = [r for r in rounds if str(r.get("subquestion_id") or "") == scoped_subquestion_id] or rounds
    refs = collect_references(run_root, summary_rows)
    source_key_counts = Counter((r.get("parsed_doi") or r.get("reference_text") or "").lower() for r in refs)
    verified_cache: dict[str, dict[str, Any]] = {}
    out_rows: list[dict[str, Any]] = []
    crossref_pool_size = crossref_pool_size or candidate_pool_size
    for round_info in rounds:
        pre_ranked: list[dict[str, Any]] = []
        seen_pool: set[str] = set()
        for ref in refs:
            row = dict(ref)
            row["score"] = round(score_reference(row, round_info, source_key_counts), 3)
            key = dedupe_key(row)
            if key in seen_pool:
                continue
            seen_pool.add(key)
            pre_ranked.append(row)
        pre_ranked.sort(key=lambda r: float(r.get("score") or 0), reverse=True)
        candidate_pool = pre_ranked[:candidate_pool_size]
        verified_keys = {dedupe_key(row) for row in candidate_pool[:crossref_pool_size]}

        scored: list[dict[str, Any]] = []
        for row in candidate_pool:
            cache_key = row.get("parsed_doi") or row.get("reference_text")[:200]
            if dedupe_key(row) in verified_keys and cache_key not in verified_cache:
                verified_cache[cache_key] = verify_reference(row, use_crossref)
            row.update(verified_cache.get(cache_key, {
                "crossref_doi": "",
                "crossref_title": "",
                "crossref_authors": "",
                "crossref_year": "",
                "crossref_container": "",
                "crossref_type": "",
                "verification_status": "not_checked_outside_crossref_pool",
            }))
            hint, query, notes = capture_hint(row)
            row.update({"capture_hint": hint, "capture_query": query, "capture_notes": notes})
            row["score"] = round(score_reference(row, round_info, source_key_counts), 3)
            scored.append(row)
        scored.sort(key=lambda r: (float(r.get("score") or 0), bool(r.get("crossref_title")), bool(r.get("crossref_doi"))), reverse=True)
        for rank, row in enumerate(scored[:candidate_pool_size], start=1):
            fallback_subquestion_slug = slugify(
                str(round_info.get("subquestion_slug") or round_info.get("query_family") or round_info.get("claim_subquestion") or "subquestion")
            )
            fallback_subquestion_id = str(round_info.get("subquestion_id") or row.get("subquestion_id") or f"round-{round_info.get('round') or 'x'}_{fallback_subquestion_slug}")
            out_rows.append({
                "subquestion_group_slug": round_info.get("subquestion_group_slug") or row.get("subquestion_group_slug") or "general",
                "subquestion_group_title": round_info.get("subquestion_group_title") or row.get("subquestion_group_title") or "General",
                "subquestion_id": fallback_subquestion_id,
                "subquestion_slug": round_info.get("subquestion_slug") or row.get("subquestion_slug") or fallback_subquestion_slug,
                "query_round": round_info.get("round") or "",
                "query_family": round_info.get("query_family") or "",
                "claim_subquestion": round_info.get("claim_subquestion") or "",
                "query_text": "; ".join(str(q) for q in (round_info.get("queries") or [])),
                "rank": rank,
                "score_basis": score_basis_text(),
                "agent_score": "",
                "agent_reason": "",
                "agent_assessment_status": "candidate_prefilter_requires_subagent_review",
                "selection_status": "candidate_pool",
                **{field: row.get(field, "") for field in FOLLOWUP_FIELDS if field not in {
                    "subquestion_group_slug", "subquestion_group_title", "subquestion_id", "subquestion_slug",
                    "query_round", "query_family", "claim_subquestion", "query_text", "rank",
                    "score_basis", "agent_score", "agent_reason", "agent_assessment_status", "selection_status",
                }},
            })
    final_rows = write_outputs(input_dir, out_rows, candidate_pool_size, final_top_n, run_root)
    if input_dir.resolve() == run_root.resolve():
        write_followup_capture_queue(run_root, final_rows, run_root)
        write_csv_json(run_root / "final-reference-selection.csv", run_root / "final-reference-selection.json", final_rows)
        final_title, final_label, final_count = final_selection_markdown_title(final_rows)
        (run_root / "final-reference-selection.md").write_text(
            render_followup_markdown(final_rows, final_title, final_count, final_label),
            encoding="utf-8",
        )
        provenance = provenance_rows(final_rows)
        write_csv_json(run_root / "reference-provenance.csv", run_root / "reference-provenance.json", provenance, REFERENCE_PROVENANCE_FIELDS)
        (run_root / "reference-provenance.md").write_text(render_provenance_markdown(provenance), encoding="utf-8")
        write_overview_materials(run_root, summary_rows, out_rows)
    else:
        write_followup_capture_queue(input_dir, final_rows, run_root)
    return out_rows


def build_followups_agent_picked(
    input_dir: Path,
    candidate_pool_size: int,
    final_top_n: int,
    use_crossref: bool,
    crossref_pool_size: int,
    refs_per_important_paper: int,
) -> list[dict[str, Any]]:
    run_root = find_run_root(input_dir)
    summary_rows = filter_summary_for_scope(read_summary(run_root), input_dir, run_root)
    rounds = read_query_rounds(run_root)
    scoped_subquestion_id = selected_subquestion_id(input_dir, run_root)
    if scoped_subquestion_id:
        rounds = [r for r in rounds if str(r.get("subquestion_id") or "") == scoped_subquestion_id] or rounds
    round_by_subquestion = {str(r.get("subquestion_id") or ""): r for r in rounds if isinstance(r, dict)}
    refs = collect_agent_picked_references(run_root, summary_rows, refs_per_important_paper)
    verified_cache: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ref in refs:
        grouped[str(ref.get("subquestion_id") or "general")].append(ref)

    out_rows: list[dict[str, Any]] = []
    crossref_pool_size = crossref_pool_size or candidate_pool_size
    for subquestion_id, items in grouped.items():
        round_info = round_by_subquestion.get(subquestion_id) or {
            "subquestion_id": subquestion_id,
            "subquestion_slug": items[0].get("subquestion_slug") if items else "",
            "subquestion_group_slug": items[0].get("subquestion_group_slug") if items else "general",
            "subquestion_group_title": items[0].get("subquestion_group_title") if items else "General",
            "round": "",
            "query_family": "agent-picked",
            "claim_subquestion": "",
            "queries": [],
        }
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for item in items:
            key = dedupe_key(item)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(dict(item))
        verified_keys = {dedupe_key(row) for row in deduped[:crossref_pool_size]}
        for rank, row in enumerate(deduped, start=1):
            cache_key = row.get("parsed_doi") or row.get("reference_text", "")[:200]
            if dedupe_key(row) in verified_keys and cache_key not in verified_cache:
                verified_cache[cache_key] = verify_reference(row, use_crossref)
            row.update(verified_cache.get(cache_key, {
                "crossref_doi": "",
                "crossref_title": "",
                "crossref_authors": "",
                "crossref_year": "",
                "crossref_container": "",
                "crossref_type": "",
                "verification_status": "not_checked_outside_crossref_pool",
            }))
            hint, query, notes = capture_hint(row)
            row.update({"capture_hint": hint, "capture_query": query, "capture_notes": notes})
            score = row.get("agent_score")
            try:
                score_value = float(score)
            except Exception:
                score_value = max(1.0, 100.0 - rank)
            out_rows.append({
                "subquestion_group_slug": round_info.get("subquestion_group_slug") or row.get("subquestion_group_slug") or "general",
                "subquestion_group_title": round_info.get("subquestion_group_title") or row.get("subquestion_group_title") or "General",
                "subquestion_id": round_info.get("subquestion_id") or row.get("subquestion_id") or subquestion_id,
                "subquestion_slug": round_info.get("subquestion_slug") or row.get("subquestion_slug") or slugify(subquestion_id),
                "query_round": round_info.get("round") or "",
                "query_family": round_info.get("query_family") or "agent-picked",
                "claim_subquestion": round_info.get("claim_subquestion") or "",
                "query_text": "; ".join(str(q) for q in (round_info.get("queries") or [])),
                "rank": rank,
                "score": round(score_value, 3),
                "score_basis": "agent_picked: selected during reading-note writing from worth-close-reading papers; Python only dedupes, checks DOI and bibliographic metadata with Crossref, and queues capture.",
                "agent_score": row.get("agent_score") or "",
                "agent_reason": row.get("agent_reason") or "",
                "agent_assessment_status": "agent_picked_during_reading_note",
                "selection_status": "agent_picked_saved_for_capture",
                "approval_status": "agent_picked_approved",
                **{field: row.get(field, "") for field in FOLLOWUP_FIELDS if field not in {
                    "subquestion_group_slug", "subquestion_group_title", "subquestion_id", "subquestion_slug",
                    "query_round", "query_family", "claim_subquestion", "query_text", "rank", "score",
                    "score_basis", "agent_score", "agent_reason", "agent_assessment_status", "selection_status", "approval_status",
                }},
            })
    final_rows = write_outputs(input_dir, out_rows, candidate_pool_size, final_top_n, run_root)
    if input_dir.resolve() == run_root.resolve():
        write_followup_capture_queue(run_root, final_rows, run_root)
        write_csv_json(run_root / "final-reference-selection.csv", run_root / "final-reference-selection.json", final_rows)
        final_title, final_label, final_count = final_selection_markdown_title(final_rows)
        (run_root / "final-reference-selection.md").write_text(
            render_followup_markdown(final_rows, final_title, final_count, final_label),
            encoding="utf-8",
        )
        provenance = provenance_rows(final_rows)
        write_csv_json(run_root / "reference-provenance.csv", run_root / "reference-provenance.json", provenance, REFERENCE_PROVENANCE_FIELDS)
        (run_root / "reference-provenance.md").write_text(render_provenance_markdown(provenance), encoding="utf-8")
        write_overview_materials(run_root, summary_rows, out_rows)
    else:
        write_followup_capture_queue(input_dir, final_rows, run_root)
    return out_rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--reference-mode", choices=["agent-picked", "python-prefilter"], default="agent-picked")
    parser.add_argument("--refs-per-important-paper", type=int, default=0, help="Optional cap for agent-picked references per worth-close-reading article. Default 0 keeps all reading-note recommendations.")
    parser.add_argument("--candidate-pool-size", type=int, default=20, help="Candidate references per atomic subquestion.")
    parser.add_argument(
        "--final-top-n",
        type=int,
        default=2,
        help="Legacy cap for python-prefilter drafts. Ignored for reading-note-approved agent-picked rows.",
    )
    parser.add_argument("--top-n", type=int, default=None, help="Deprecated alias for --final-top-n; kept for old commands.")
    parser.add_argument("--crossref-pool-size", type=int, default=0, help="Maximum candidate references per subquestion to check with Crossref metadata lookup. Defaults to --candidate-pool-size.")
    parser.add_argument("--no-crossref", dest="use_crossref", action="store_false", help="Skip Crossref metadata lookup.")
    parser.set_defaults(use_crossref=True)
    args = parser.parse_args()
    final_top_n = args.top_n if args.top_n is not None else args.final_top_n
    candidate_pool_size = max(args.candidate_pool_size, final_top_n)
    if args.reference_mode == "python-prefilter":
        rows = build_followups(
            args.run_dir.resolve(),
            candidate_pool_size,
            final_top_n,
            args.use_crossref,
            args.crossref_pool_size or candidate_pool_size,
        )
    else:
        rows = build_followups_agent_picked(
            args.run_dir.resolve(),
            candidate_pool_size,
            final_top_n,
            args.use_crossref,
            args.crossref_pool_size or candidate_pool_size,
            max(0, args.refs_per_important_paper),
        )
    print(f"reference_candidates={len(rows)}")
    print(f"reference_mode={args.reference_mode}")
    print(f"candidate_pool_size={candidate_pool_size}")
    print(f"final_top_n={final_top_n}")
    print(f"output={args.run_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
