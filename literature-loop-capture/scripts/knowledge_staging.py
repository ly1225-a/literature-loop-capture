#!/usr/bin/env python3
"""Build a human-facing knowledge staging folder for a literature run."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def normalize_ws(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def safe_slug(text: str, fallback: str = "item", limit: int = 96) -> str:
    text = normalize_ws(text)
    text = text.replace("/", "-")
    text = re.sub(r"[\\:*?\"<>|]+", " ", text)
    text = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff._-]+", "-", text)
    text = text.strip(".-_").lower()
    return (text[:limit].strip(".-_") or fallback)


def doi_slug(doi: str) -> str:
    return safe_slug(doi.lower().replace("/", "-"), "doi", 80)


def short_hash(text: str, length: int = 10) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def subquestion_dir_name(subquestion_id: str) -> str:
    raw = normalize_ws(subquestion_id) or "unknown_subquestion"
    slug = safe_slug(raw, "unknown_subquestion", 72)
    if slug == raw and raw not in {".", ".."}:
        return raw
    return f"{slug}--{short_hash(raw)}"


def is_clear_doi(value: str) -> bool:
    return bool(re.match(r"^10\.\d{4,9}/\S+$", normalize_ws(value), flags=re.IGNORECASE))


def read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def md_link(label: str, path: Path | str) -> str:
    return f"[{label}]({path})"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_article_artifacts(article_dir: Path) -> list[dict[str, Any]]:
    image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}
    table_suffixes = {".csv", ".md", ".json", ".xlsx", ".xls"}
    generated_names = {"index.md", "manifest.json"}

    candidates: list[tuple[str, Path]] = []
    source_pdf = article_dir / "source.pdf"
    if source_pdf.is_file():
        candidates.append(("pdf", source_pdf))

    for root, kind, suffixes in [
        (article_dir / "figures", "figure", image_suffixes),
        (article_dir / "tables", "table", table_suffixes),
    ]:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.name.lower() in generated_names:
                continue
            if path.is_file() and path.suffix.lower() in suffixes:
                candidates.append((kind, path))

    artifacts: list[dict[str, Any]] = []
    for kind, path in candidates:
        artifacts.append(
            {
                "kind": kind,
                "path": path.resolve(),
                "rel_to_article": str(path.relative_to(article_dir)),
                "filename": path.name,
                "suffix": path.suffix.lower(),
                "size_bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    return artifacts


def canonical_text(text: str) -> str:
    text = normalize_ws(text).lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text)
    return normalize_ws(text)


def article_key(article: dict[str, Any]) -> str:
    doi = normalize_ws(article.get("doi")).lower()
    if doi:
        return f"doi:{doi}"
    title = canonical_text(article.get("title", ""))
    return f"title:{title or rel(article['article_dir'], article['article_dir'].parents[0])}"


def article_status(article: dict[str, Any]) -> str:
    if article.get("note_path"):
        return "reading_note_done"
    if article.get("has_fulltext"):
        return "fulltext_captured_needs_note"
    if article.get("has_source_pdf"):
        return "pdf_supplied_needs_mineru_or_note"
    return "metadata_or_manual_pending"


ARTICLE_STATUS_SORT = {
    "metadata_or_manual_pending": 0,
    "pdf_supplied_needs_mineru_or_note": 1,
    "fulltext_captured_needs_note": 2,
    "reading_note_done": 3,
}


def article_sort_score(article: dict[str, Any]) -> tuple[int, int, int, str, str]:
    return (
        ARTICLE_STATUS_SORT.get(article_status(article), 0),
        1 if article.get("source_role") == "primary" else 0,
        len(article.get("artifacts") or []),
        normalize_ws(article.get("title")).lower(),
        str(article.get("article_dir", "")),
    )


def group_canonical_articles(articles: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for article in articles:
        grouped[article_key(article)].append(article)
    return dict(grouped)


def choose_canonical_article(group: list[dict[str, Any]]) -> dict[str, Any]:
    return max(group, key=article_sort_score)


def target_pdf_exists(row: dict[str, str], run_dir: Path) -> bool:
    target = normalize_ws(row.get("put_pdf_here"))
    if not target:
        return False
    target_path = Path(target)
    if not target_path.is_absolute():
        target_path = run_dir / target_path
    return (target_path / "source.pdf").exists()


def article_dirs(run_dir: Path) -> list[Path]:
    root = run_dir / "subquestions"
    if not root.exists():
        return []
    dirs: list[Path] = []
    for path in root.glob("*/*/**/articles/*"):
        if path.is_dir() and (path / "metadata.json").exists():
            dirs.append(path)
    return sorted(dirs)


def load_articles(run_dir: Path) -> list[dict[str, Any]]:
    articles: list[dict[str, Any]] = []
    for article_dir in article_dirs(run_dir):
        metadata = read_json(article_dir / "metadata.json", {})
        fulltext = read_json(article_dir / "fulltext.json", {})
        note_path = article_dir / "reading-note-zh.md"
        rec_path = article_dir / "recommended-references.csv"
        title = normalize_ws(metadata.get("title") or fulltext.get("title") or article_dir.name)
        subquestion_id = normalize_ws(metadata.get("subquestion_id") or article_dir.parts[-5])
        artifacts = find_article_artifacts(article_dir)
        articles.append(
            {
                "title": title,
                "doi": normalize_ws(metadata.get("doi") or fulltext.get("doi")),
                "year": normalize_ws(metadata.get("year") or fulltext.get("year")),
                "publisher": normalize_ws(metadata.get("publisher") or metadata.get("source_bucket")),
                "venue": normalize_ws(metadata.get("journal") or metadata.get("venue") or fulltext.get("venue")),
                "source_role": normalize_ws(metadata.get("source_role") or ("reference" if "/references/" in str(article_dir) else "primary")),
                "source_bucket": normalize_ws(metadata.get("source_bucket") or metadata.get("pdf_source")),
                "subquestion_id": subquestion_id,
                "subquestion_text": normalize_ws(metadata.get("subquestion_text")),
                "subquestion_group_slug": normalize_ws(metadata.get("subquestion_group_slug")),
                "article_dir": article_dir,
                "note_path": note_path if note_path.exists() else None,
                "recommended_references_path": rec_path if rec_path.exists() else None,
                "has_fulltext": (article_dir / "fulltext.json").exists() or (article_dir / "fulltext.md").exists(),
                "has_source_pdf": (article_dir / "source.pdf").exists(),
                "artifacts": artifacts,
                "abstract": normalize_ws(fulltext.get("abstract"))[:700],
            }
        )
    return articles


def load_coverage(run_dir: Path) -> dict[str, dict[str, Any]]:
    data = read_json(run_dir / "coverage-review" / "subquestion-coverage-review.json", {})
    items = data.get("subquestions") if isinstance(data, dict) else []
    if not isinstance(items, list):
        return {}
    output: dict[str, dict[str, Any]] = {}
    for item in items:
        if isinstance(item, dict) and normalize_ws(item.get("subquestion_id")):
            output[normalize_ws(item.get("subquestion_id"))] = item
    return output


def load_seed_ledgers(run_dir: Path) -> dict[str, list[dict[str, Any]]]:
    seeds: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str, str]] = set()
    for path in sorted((run_dir / "loop-state").glob("*/iteration-*/seed-ledger.json")):
        data = read_json(path, [])
        if not isinstance(data, list):
            continue
        for row in data:
            if not isinstance(row, dict):
                continue
            subq = normalize_ws(row.get("subquestion_id")) or path.parts[-3]
            seed_text = normalize_ws(row.get("seed_text") or row.get("text") or row.get("title"))
            if not seed_text:
                continue
            key = (subq, seed_text.lower(), normalize_ws(row.get("source_article_dir")).lower())
            if key in seen:
                continue
            seen.add(key)
            item = dict(row)
            item["_ledger_file"] = str(path)
            seeds[subq].append(item)
    return seeds


def load_reference_ledgers(run_dir: Path) -> dict[str, list[dict[str, str]]]:
    ledger_files = [
        "reviewed-reference-ledger.csv",
        "supplemental-followup-ledger.csv",
        "final-reference-selection.csv",
        "reference-provenance.csv",
    ]
    ledgers: dict[str, list[dict[str, str]]] = defaultdict(list)
    seen: set[tuple[str, str, str, str]] = set()
    for filename in ledger_files:
        for row in read_csv(run_dir / filename):
            subq = normalize_ws(row.get("subquestion_id"))
            title = normalize_ws(
                row.get("title")
                or row.get("crossref_title")
                or row.get("capture_query")
                or row.get("reference_text")
            )
            doi = normalize_ws(row.get("doi") or row.get("crossref_doi") or row.get("parsed_doi"))
            if not subq or not (title or doi):
                continue
            key = (subq, doi.lower(), title.lower(), filename)
            if key in seen:
                continue
            seen.add(key)
            item = dict(row)
            item["_ledger_file"] = filename
            ledgers[subq].append(item)
    return ledgers


def load_subquestion_summaries(run_dir: Path) -> dict[str, list[Path]]:
    summaries: dict[str, list[Path]] = defaultdict(list)
    for path in sorted((run_dir / "subquestions").glob("*/*/subquestion-summary-zh.md")):
        subq = path.parent.name
        summaries[subq].append(path)
    return summaries


def load_manual_rows(run_dir: Path) -> tuple[dict[str, list[dict[str, str]]], dict[str, list[dict[str, str]]]]:
    source_rows = {
        "priority": read_csv(run_dir / "manual-pdf-priority-needed.csv"),
        "optional": read_csv(run_dir / "manual-pdf-optional-needed.csv"),
        "needed": read_csv(run_dir / "manual-pdf-needed-only.csv"),
    }
    priority_optional_keys = set()
    for bucket in ["priority", "optional"]:
        for row in source_rows[bucket]:
            doi = normalize_ws(row.get("doi"))
            target = normalize_ws(row.get("put_pdf_here"))
            if is_clear_doi(doi):
                priority_optional_keys.add((doi.lower(), target))
    sources = [
        ("priority", "manual-pdf-priority-needed.csv", source_rows["priority"]),
        ("optional", "manual-pdf-optional-needed.csv", source_rows["optional"]),
        ("needed", "manual-pdf-needed-only.csv", source_rows["needed"]),
    ]
    seen: set[tuple[str, str]] = set()
    seen_blocked: set[tuple[str, str, str]] = set()
    rows_by_bucket: dict[str, list[dict[str, str]]] = defaultdict(list)
    blocked_no_doi: dict[str, list[dict[str, str]]] = defaultdict(list)
    for bucket, filename, rows in sources:
        for row in rows:
            doi = normalize_ws(row.get("doi"))
            if not is_clear_doi(doi):
                blocked_key = (
                    normalize_ws(row.get("subquestion_id")),
                    normalize_ws(row.get("title")).lower(),
                    normalize_ws(row.get("put_pdf_here")),
                )
                if blocked_key in seen_blocked:
                    continue
                seen_blocked.add(blocked_key)
                row = dict(row)
                row["_bucket"] = bucket
                row["_source_file"] = filename
                blocked_no_doi[normalize_ws(row.get("subquestion_id")) or "unknown_subquestion"].append(row)
                continue
            key = (doi.lower(), normalize_ws(row.get("put_pdf_here")))
            if bucket == "needed" and key in priority_optional_keys:
                continue
            if key in seen:
                continue
            seen.add(key)
            row = dict(row)
            row["_bucket"] = bucket
            row["_source_file"] = filename
            rows_by_bucket[bucket].append(row)
    return rows_by_bucket, blocked_no_doi


def summarize_query_item(item: Any) -> str:
    if isinstance(item, str):
        return normalize_ws(item)
    if not isinstance(item, dict):
        return normalize_ws(item)
    for key in ["query", "exact_query", "search_query", "text", "title"]:
        value = normalize_ws(item.get(key))
        if value:
            return value
    return normalize_ws(json.dumps(item, ensure_ascii=False))[:180]


def unique_exact_targets(targets: list[Any]) -> list[Any]:
    unique: list[Any] = []
    seen: set[tuple[str, str, str, str]] = set()
    for target in targets:
        if isinstance(target, dict):
            key = (
                normalize_ws(target.get("exact_query") or target.get("query") or target.get("title")).lower(),
                normalize_ws(target.get("doi")).lower(),
                normalize_ws(target.get("route") or target.get("publisher_route")).lower(),
                normalize_ws(target.get("status") or target.get("decision") or target.get("openalex_status")).lower(),
            )
        else:
            key = (summarize_query_item(target).lower(), "", "", "")
        if key in seen:
            continue
        seen.add(key)
        unique.append(target)
    return unique


def load_query_journeys(run_dir: Path) -> dict[str, dict[str, Any]]:
    journeys: dict[str, dict[str, Any]] = defaultdict(lambda: {"initial_queries": [], "iterations": []})
    initial = read_json(run_dir / "agent-query-plan.json", {})
    if isinstance(initial, dict):
        for sq in initial.get("subquestions", []) or []:
            if not isinstance(sq, dict):
                continue
            subq = normalize_ws(sq.get("subquestion_id"))
            if not subq:
                continue
            queries = sq.get("queries") or []
            journeys[subq]["subquestion_text"] = normalize_ws(sq.get("subquestion_text"))
            journeys[subq]["initial_query_rationale"] = normalize_ws(sq.get("query_rationale"))
            journeys[subq]["initial_queries"] = [summarize_query_item(q) for q in queries if summarize_query_item(q)]
    for iteration_dir in sorted((run_dir / "loop-state").glob("*/iteration-*")):
        if not iteration_dir.is_dir():
            continue
        subq = iteration_dir.parent.name
        rationale_json = read_json(iteration_dir / "query-rationale-review.json", {})
        amendment_json = read_json(iteration_dir / "query-plan-amendment.json", {})
        manual_rows = read_csv(iteration_dir / "manual.csv")
        seed_rows = read_json(iteration_dir / "seed-ledger.json", [])
        rationale_md = iteration_dir / "query-rationale-review.md"
        amendment_md = iteration_dir / "query-plan-amendment.md"
        broad = []
        exact = []
        excluded = []
        if isinstance(rationale_json, dict):
            broad.extend(rationale_json.get("broad_discovery_queries") or [])
            exact.extend(rationale_json.get("exact_openalex_grounded_targets") or [])
            excluded.extend(rationale_json.get("excluded_broad_candidates") or [])
        if isinstance(amendment_json, dict):
            broad.extend(amendment_json.get("broad_discovery_queries") or [])
            exact.extend(amendment_json.get("exact_openalex_grounded_targets") or [])
        iteration = {
            "iteration": iteration_dir.name,
            "dir": iteration_dir,
            "review_mode": normalize_ws((rationale_json if isinstance(rationale_json, dict) else {}).get("review_mode")),
            "coverage_decision": normalize_ws((rationale_json if isinstance(rationale_json, dict) else {}).get("coverage_decision")),
            "coverage_score_0_to_5": normalize_ws((rationale_json if isinstance(rationale_json, dict) else {}).get("coverage_score_0_to_5")),
            "missing_evidence": (rationale_json if isinstance(rationale_json, dict) else {}).get("missing_evidence_or_terms") or [],
            "notes": normalize_ws((rationale_json if isinstance(rationale_json, dict) else {}).get("notes")),
            "broad_queries": list(dict.fromkeys(summarize_query_item(q) for q in broad if summarize_query_item(q))),
            "exact_targets": unique_exact_targets(exact),
            "manual_rows": manual_rows,
            "seed_count": len(seed_rows) if isinstance(seed_rows, list) else 0,
            "excluded_broad": list(dict.fromkeys(summarize_query_item(q) for q in excluded if summarize_query_item(q))),
            "rationale_md": rationale_md if rationale_md.exists() else None,
            "amendment_md": amendment_md if amendment_md.exists() else None,
        }
        journeys[subq]["iterations"].append(iteration)
    return dict(journeys)


def collect_recommended_references(articles: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    refs: dict[str, list[dict[str, str]]] = defaultdict(list)
    for article in articles:
        rec_path = article.get("recommended_references_path")
        if not rec_path:
            continue
        for row in read_csv(rec_path):
            row = dict(row)
            row["_source_article_title"] = article["title"]
            row["_source_article_dir"] = str(article["article_dir"])
            refs[article["subquestion_id"]].append(row)
    return refs


def extract_seed_lines(note_path: Path | None) -> list[str]:
    if not note_path or not note_path.exists():
        return []
    lines: list[str] = []
    for raw in note_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = normalize_ws(raw.strip("-*# "))
        lower = line.lower()
        if not line:
            continue
        if "seed" in lower or "高价值" in line or "资源" in line or "数据库" in line or "benchmark" in lower:
            lines.append(line[:260])
    return list(dict.fromkeys(lines))[:20]


def frontmatter(**values: Any) -> str:
    lines = ["---"]
    for key, value in values.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {json.dumps(str(item), ensure_ascii=False)}")
        else:
            lines.append(f"{key}: {json.dumps(str(value or ''), ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines)


def write_manual_inbox(run_dir: Path, out_dir: Path, rows_by_bucket: dict[str, list[dict[str, str]]]) -> dict[str, list[dict[str, str]]]:
    by_subq: dict[str, list[dict[str, str]]] = defaultdict(list)
    for bucket, rows in rows_by_bucket.items():
        for row in rows:
            subq = normalize_ws(row.get("subquestion_id")) or "unknown_subquestion"
            title = normalize_ws(row.get("title")) or "Untitled"
            doi = normalize_ws(row.get("doi"))
            dropbox = out_dir / "subquestions" / subquestion_dir_name(subq) / "manual_pdf_dropbox"
            dropbox.mkdir(parents=True, exist_ok=True)
            canonical_dir = normalize_ws(row.get("put_pdf_here"))
            status = "already_supplied_needs_mineru_or_note" if target_pdf_exists(row, run_dir) else "needs_user_pdf"
            suggested = f"{doi_slug(doi)}__{safe_slug(title, 'paper', 72)}.pdf"
            staged = dict(row)
            staged["_dropbox"] = str(dropbox)
            staged["_dropbox_suggested_filename"] = suggested
            staged["_manual_status"] = status
            by_subq[subq].append(staged)

    for subq, rows in by_subq.items():
        subq_dir = out_dir / "subquestions" / subquestion_dir_name(subq)
        dropbox = subq_dir / "manual_pdf_dropbox"
        dropbox.mkdir(parents=True, exist_ok=True)
        csv_path = subq_dir / "manual_pdf_download_list.csv"
        fieldnames = [
            "status",
            "triage",
            "doi",
            "title",
            "publisher",
            "venue",
            "suggested_filename",
            "canonical_target_dir",
            "source_file",
        ]
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        "status": row.get("_manual_status", ""),
                        "triage": row.get("_bucket", ""),
                        "doi": normalize_ws(row.get("doi")),
                        "title": normalize_ws(row.get("title")),
                        "publisher": normalize_ws(row.get("publisher")),
                        "venue": normalize_ws(row.get("venue")),
                        "suggested_filename": row.get("_dropbox_suggested_filename", ""),
                        "canonical_target_dir": normalize_ws(row.get("put_pdf_here")),
                        "source_file": row.get("_source_file", ""),
                    }
                )
        for filename, wanted_status in [
            ("manual_pdf_to_download.csv", "needs_user_pdf"),
            ("manual_pdf_already_supplied.csv", "already_supplied_needs_mineru_or_note"),
        ]:
            with (subq_dir / filename).open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                for row in rows:
                    if row.get("_manual_status") != wanted_status:
                        continue
                    writer.writerow(
                        {
                            "status": row.get("_manual_status", ""),
                            "triage": row.get("_bucket", ""),
                            "doi": normalize_ws(row.get("doi")),
                            "title": normalize_ws(row.get("title")),
                            "publisher": normalize_ws(row.get("publisher")),
                            "venue": normalize_ws(row.get("venue")),
                            "suggested_filename": row.get("_dropbox_suggested_filename", ""),
                            "canonical_target_dir": normalize_ws(row.get("put_pdf_here")),
                            "source_file": row.get("_source_file", ""),
                        }
                    )
        readme = [
            frontmatter(type="manual_pdf_dropbox", subquestion=subq),
            f"# Manual PDF Dropbox - {subq}",
            "",
            "Put manually downloaded PDFs for this subquestion in this folder.",
            "",
            "`../manual_pdf_to_download.csv` is the short action list for PDFs still needing user download. `../manual_pdf_download_list.csv` keeps the full DOI-backed audit list, including already supplied PDFs waiting for MinerU or notes. Preferred filenames are shown in the CSVs, but exact naming is less important than keeping the PDF in this subquestion dropbox. The later ingest step matches by DOI, title text, and the download list.",
            "",
            "Do not put no-DOI items here unless the metadata is clarified first.",
        ]
        write_text(dropbox / "README.md", "\n".join(readme))
    return by_subq


def clean_empty_manual_inbox_items(out_dir: Path) -> None:
    # Legacy inboxes may contain user-supplied PDFs from older runs. Never clean
    # them implicitly; the current workflow uses manual_pdf_dropbox instead.
    return


GENERATED_ROOT_FILES = {
    "README.md",
    "search_journey.md",
    "knowledge-staging-manifest.json",
}

GENERATED_ROOT_DIRS = {
    "papers",
    "seeds",
    "references",
    "overview",
    "obsidian_export",
}

GENERATED_SUBQUESTION_FILES = {
    "overview.md",
    "papers.md",
    "figures_tables.md",
    "captured_papers.md",
    "reading_notes_index.md",
    "important_seeds.md",
    "recommended_references.md",
    "manual_pdf_needed.md",
    "manual_pdf_download_list.csv",
    "manual_pdf_to_download.csv",
    "manual_pdf_already_supplied.csv",
    "subquestion_summaries.md",
    "query_journey.md",
    "coverage.md",
    "index.md",
}

GENERATED_SUBQUESTION_DIRS = {
    "paper_cards",
}

PROTECTED_SUBQUESTION_DIRS = {
    "manual_pdf_dropbox",
    "manual_pdf_inbox",
}


def prepare_knowledge_output(out_dir: Path, active_subquestions: set[str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in GENERATED_ROOT_FILES:
        path = out_dir / name
        if path.exists() and path.is_file():
            path.unlink()
    for name in GENERATED_ROOT_DIRS:
        path = out_dir / name
        if path.exists() and path.is_dir():
            shutil.rmtree(path)

    subquestions_dir = out_dir / "subquestions"
    if not subquestions_dir.exists():
        return
    active_dir_names = {subquestion_dir_name(subq) for subq in active_subquestions}
    for subq_dir in sorted(path for path in subquestions_dir.iterdir() if path.is_dir()):
        for name in GENERATED_SUBQUESTION_FILES:
            path = subq_dir / name
            if path.exists() and path.is_file():
                path.unlink()
        for name in GENERATED_SUBQUESTION_DIRS:
            path = subq_dir / name
            if path.exists() and path.is_dir():
                shutil.rmtree(path)
        if subq_dir.name in active_dir_names:
            continue
        has_protected_user_staging = any((subq_dir / name).exists() for name in PROTECTED_SUBQUESTION_DIRS)
        if has_protected_user_staging:
            continue
        try:
            subq_dir.rmdir()
        except OSError:
            pass


def format_target_line(target: dict[str, Any]) -> str:
    query = normalize_ws(target.get("exact_query") or target.get("query") or target.get("title"))
    route = normalize_ws(target.get("route") or target.get("publisher_route"))
    status = normalize_ws(target.get("status") or target.get("decision") or target.get("openalex_status"))
    title = normalize_ws(target.get("openalex_title") or target.get("title"))
    doi = normalize_ws(target.get("doi"))
    reason = normalize_ws(target.get("manual_reason") or target.get("reason"))
    bits = [f"- `{query or 'untitled target'}`"]
    if route:
        bits.append(f"route=`{route}`")
    if status:
        bits.append(f"status=`{status}`")
    if title:
        bits.append(f"title={title}")
    if doi:
        bits.append(f"DOI=`{doi}`")
    if reason and reason != "none":
        bits.append(f"reason={reason}")
    return " | ".join(bits)


def write_query_journey_page(run_dir: Path, subq_dir: Path, subq: str, journey: dict[str, Any]) -> None:
    lines = [
        frontmatter(type="query_journey", subquestion=subq),
        f"# Query Journey - {subq}",
        "",
        "This page preserves the literature-search reasoning for this subquestion: previous queries, why they changed, what was routed as broad discovery, what became exact/manual targets, and which artifacts hold the full provenance.",
        "",
        "## Initial Query Plan",
        "",
    ]
    if journey.get("initial_query_rationale"):
        lines.extend(["**Initial rationale:** " + journey["initial_query_rationale"], ""])
    initial_queries = journey.get("initial_queries") or []
    if initial_queries:
        lines.extend([f"- `{q}`" for q in initial_queries])
    else:
        lines.append("No initial query rows found in `agent-query-plan.json`.")
    for iteration in journey.get("iterations") or []:
        lines.extend(
            [
                "",
                f"## {iteration['iteration']}",
                "",
                f"- Review mode: `{iteration.get('review_mode') or 'not recorded'}`",
                f"- Coverage decision entering iteration: `{iteration.get('coverage_decision') or 'not recorded'}`",
                f"- Coverage score entering iteration: `{iteration.get('coverage_score_0_to_5') or 'not recorded'}`",
                f"- Seed rows considered: `{iteration.get('seed_count')}`",
            ]
        )
        if iteration.get("missing_evidence"):
            lines.extend(["", "### Missing Evidence / Search Gaps", ""])
            for gap in iteration["missing_evidence"]:
                lines.append(f"- {normalize_ws(gap)}")
        broad = iteration.get("broad_queries") or []
        lines.extend(["", "### Broad Discovery Queries", ""])
        if broad:
            lines.extend([f"- `{q}`" for q in broad])
        else:
            lines.append("No broad query proposed for this iteration.")
        excluded = iteration.get("excluded_broad") or []
        if excluded:
            lines.extend(["", "### Excluded Broad Candidates", ""])
            lines.extend([f"- `{q}`" for q in excluded])
        exact = iteration.get("exact_targets") or []
        lines.extend(["", "### Exact / Manual Target Ledger", ""])
        if exact:
            for target in exact[:80]:
                if isinstance(target, dict):
                    lines.append(format_target_line(target))
                else:
                    lines.append(f"- `{summarize_query_item(target)}`")
            if len(exact) > 80:
                lines.append(f"- ... {len(exact) - 80} more targets in source JSON.")
        else:
            lines.append("No exact targets recorded.")
        manual_rows = iteration.get("manual_rows") or []
        if manual_rows:
            lines.extend(["", "### Manual Rows From This Iteration", ""])
            for row in manual_rows[:40]:
                title = normalize_ws(row.get("title") or row.get("openalex_title") or row.get("exact_query"))
                doi = normalize_ws(row.get("doi"))
                reason = normalize_ws(row.get("manual_reason") or row.get("reason") or row.get("status"))
                lines.append(f"- {title or 'Untitled'} | DOI: `{doi or 'not recorded'}` | reason: {reason or 'not recorded'}")
            if len(manual_rows) > 40:
                lines.append(f"- ... {len(manual_rows) - 40} more manual rows in `manual.csv`.")
        lines.extend(["", "### Provenance Artifacts", ""])
        for label, path in [
            ("query rationale review", iteration.get("rationale_md")),
            ("query plan amendment", iteration.get("amendment_md")),
        ]:
            if path:
                lines.append(f"- {label}: `{rel(path, run_dir)}`")
    write_text(subq_dir / "query_journey.md", "\n".join(lines))


def canonical_card_filename(key: str, article: dict[str, Any]) -> str:
    doi = normalize_ws(article.get("doi"))
    if doi:
        base = doi_slug(doi)
    else:
        key_text = key.split(":", 1)[1] if ":" in key else key
        base = safe_slug(key_text or article.get("title", ""), "paper", 80)
    return f"{base}--{short_hash(key)}.md"


def article_display_publisher(article: dict[str, Any]) -> str:
    publisher = normalize_ws(article.get("publisher"))
    venue = normalize_ws(article.get("venue"))
    if publisher and venue and publisher != venue:
        return f"{publisher}; {venue}"
    return publisher or venue or "not recorded"


def artifact_markdown_line(artifact: dict[str, Any], run_dir: Path, prefix: str = "-") -> str:
    path = artifact.get("path")
    path_text = rel(path, run_dir) if isinstance(path, Path) else normalize_ws(path)
    return (
        f"{prefix} `{normalize_ws(artifact.get('kind')) or 'artifact'}` "
        f"`{normalize_ws(artifact.get('filename')) or Path(path_text).name}`"
        f" | path: `{path_text}`"
        f" | size: `{artifact.get('size_bytes', '')}`"
        f" | sha256: `{normalize_ws(artifact.get('sha256'))}`"
    )


def write_paper_card(
    run_dir: Path,
    card_path: Path,
    canonical_key: str,
    canonical: dict[str, Any],
    occurrences: list[dict[str, Any]],
) -> None:
    lines = [
        frontmatter(
            type="paper_card",
            canonical_key=canonical_key,
            subquestion=canonical.get("subquestion_id"),
            doi=canonical.get("doi"),
            title=canonical.get("title"),
        ),
        f"# {canonical['title']}",
        "",
        f"- Canonical key: `{canonical_key}`",
        f"- DOI: `{canonical['doi'] or 'not recorded'}`",
        f"- Year: `{canonical['year'] or 'not recorded'}`",
        f"- Publisher / venue: {article_display_publisher(canonical)}",
        f"- Canonical status: `{article_status(canonical)}`",
        f"- Occurrences: `{len(occurrences)}`",
        "",
        "## Source Roles / Paths",
        "",
    ]
    for article in sorted(occurrences, key=lambda a: rel(a["article_dir"], run_dir)):
        lines.append(
            f"- `{article['source_role'] or 'not recorded'}` | status: `{article_status(article)}`"
            f" | path: `{rel(article['article_dir'], run_dir)}`"
        )
    lines.extend(["", "## Reading Assets", ""])
    note_path = canonical.get("note_path")
    rec_path = canonical.get("recommended_references_path")
    lines.append(f"- Reading note: `{rel(note_path, run_dir) if note_path else 'not recorded'}`")
    lines.append(f"- Recommended references: `{rel(rec_path, run_dir) if rec_path else 'not recorded'}`")
    if canonical.get("abstract"):
        lines.extend(["", "## Abstract", "", canonical["abstract"]])
    lines.extend(["", "## Artifacts", ""])
    artifact_count = 0
    for article in sorted(occurrences, key=lambda a: rel(a["article_dir"], run_dir)):
        for artifact in article.get("artifacts") or []:
            artifact_count += 1
            lines.append(
                artifact_markdown_line(artifact, run_dir)
                + f" | article: `{rel(article['article_dir'], run_dir)}`"
            )
    if not artifact_count:
        lines.append("No artifacts found for this canonical paper.")
    lines.extend(["", "## Duplicate Occurrences", ""])
    if len(occurrences) > 1:
        for article in sorted(occurrences, key=lambda a: rel(a["article_dir"], run_dir)):
            lines.append(
                f"- {article['title']} | DOI: `{article['doi'] or 'not recorded'}`"
                f" | role: `{article['source_role'] or 'not recorded'}`"
                f" | path: `{rel(article['article_dir'], run_dir)}`"
            )
    else:
        lines.append("No duplicate occurrences for this canonical paper.")
    write_text(card_path, "\n".join(lines))


def write_subquestion_overview(
    subq_dir: Path,
    subq: str,
    subq_text: str,
    articles: list[dict[str, Any]],
    canonical_groups: dict[str, list[dict[str, Any]]],
    refs: list[dict[str, str]],
    ledger_refs: list[dict[str, str]],
    ledger_seeds: list[dict[str, Any]],
    manual: list[dict[str, str]],
    manual_blocked: list[dict[str, str]],
    cov: dict[str, Any],
) -> None:
    duplicate_groups = [group for group in canonical_groups.values() if len(group) > 1]
    lines = [
        frontmatter(type="subquestion_overview", subquestion=subq, status="staging"),
        f"# Overview - {subq}",
        "",
        subq_text or "Subquestion text not recorded.",
        "",
        "## Coverage",
        "",
        f"- Decision: `{normalize_ws(cov.get('coverage_decision')) or 'not recorded'}`",
        f"- Score: `{normalize_ws(cov.get('coverage_score_0_to_5')) or 'not recorded'}`",
        f"- Stage status: `{normalize_ws(cov.get('coverage_stage_status')) or 'not recorded'}`",
        f"- Remaining gaps: {normalize_ws(cov.get('remaining_gaps') or cov.get('gaps') or 'not recorded')}",
        "",
        "## Counts",
        "",
        f"- Captured article folders: `{len(articles)}`",
        f"- Canonical papers: `{len(canonical_groups)}`",
        f"- Duplicate canonical groups: `{len(duplicate_groups)}`",
        f"- Figure/table/PDF artifacts: `{sum(len(a.get('artifacts') or []) for a in articles)}`",
        f"- Article recommended-reference rows: `{len(refs)}`",
        f"- Root/reference ledger rows: `{len(ledger_refs)}`",
        f"- Seed-ledger rows: `{len(ledger_seeds)}`",
        f"- DOI-backed manual PDF rows: `{len(manual)}`",
        f"- No-DOI manual rows held out: `{len(manual_blocked)}`",
        "",
        "## Main Pages",
        "",
        "- [Canonical papers](papers.md)",
        "- [Paper cards](paper_cards/)",
        "- [Figures and tables](figures_tables.md)",
        "- [Captured papers](captured_papers.md)",
        "- [Reading notes](reading_notes_index.md)",
        "- [Important seeds](important_seeds.md)",
        "- [Recommended references](recommended_references.md)",
        "- [Query/search journey](query_journey.md)",
        "- [Manual PDF needed](manual_pdf_needed.md)",
        "- [Manual PDF dropbox](manual_pdf_dropbox/README.md)",
        "- [Coverage](coverage.md)",
        "- [Subquestion summaries](subquestion_summaries.md)",
    ]
    write_text(subq_dir / "overview.md", "\n".join(lines))


def write_subquestion_papers(
    run_dir: Path,
    subq_dir: Path,
    subq: str,
    canonical_groups: dict[str, list[dict[str, Any]]],
) -> None:
    cards_dir = subq_dir / "paper_cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        frontmatter(type="subquestion_papers", subquestion=subq),
        f"# Papers - {subq}",
        "",
        "Canonical papers are deduped by DOI when available, otherwise by normalized title. Occurrence paths remain listed so duplicate capture folders are auditable.",
        "",
    ]
    if not canonical_groups:
        lines.append("No captured papers found.")
        write_text(subq_dir / "papers.md", "\n".join(lines))
        return
    for key, group in sorted(canonical_groups.items(), key=lambda kv: choose_canonical_article(kv[1])["title"].lower()):
        canonical = choose_canonical_article(group)
        card_name = canonical_card_filename(key, canonical)
        write_paper_card(run_dir, cards_dir / card_name, key, canonical, group)
        lines.append(
            f"- **{canonical['title']}** ({canonical['year'] or 'n.d.'})"
            f" | DOI: `{canonical['doi'] or 'not recorded'}`"
            f" | publisher/venue: {article_display_publisher(canonical)}"
            f" | status: `{article_status(canonical)}`"
            f" | occurrences: `{len(group)}`"
            f" | card: [paper card](paper_cards/{card_name})"
        )
        for article in sorted(group, key=lambda a: rel(a["article_dir"], run_dir)):
            lines.append(
                f"  - occurrence: `{article['source_role'] or 'not recorded'}`"
                f" `{rel(article['article_dir'], run_dir)}`"
            )
    write_text(subq_dir / "papers.md", "\n".join(lines))


def write_subquestion_artifact_index(run_dir: Path, subq_dir: Path, subq: str, articles: list[dict[str, Any]]) -> None:
    lines = [
        frontmatter(type="subquestion_artifacts", subquestion=subq),
        f"# Figures / Tables - {subq}",
        "",
        "This page indexes existing article artifacts in place. It does not copy attachments.",
        "",
    ]
    artifact_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for article in articles:
        for artifact in article.get("artifacts") or []:
            artifact_rows.append((article, artifact))
    if not artifact_rows:
        lines.append("No figures, tables, PDFs, or other article artifacts found.")
        write_text(subq_dir / "figures_tables.md", "\n".join(lines))
        return
    for article, artifact in sorted(
        artifact_rows,
        key=lambda item: (
            item[1].get("kind", ""),
            item[0].get("title", "").lower(),
            item[1].get("filename", ""),
        ),
    ):
        lines.append(
            artifact_markdown_line(artifact, run_dir)
            + f" | article: {article['title']}"
            + f" | article path: `{rel(article['article_dir'], run_dir)}`"
        )
    write_text(subq_dir / "figures_tables.md", "\n".join(lines))


def write_attachment_manifest(run_dir: Path, out_dir: Path, articles: list[dict[str, Any]]) -> None:
    manifest_path = out_dir / "obsidian_export" / "attachments_manifest.csv"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "subquestion_id",
        "article_title",
        "doi",
        "kind",
        "filename",
        "source_path",
        "rel_to_run",
        "rel_to_article",
        "size_bytes",
        "sha256",
    ]
    rows: list[dict[str, Any]] = []
    for article in articles:
        for artifact in article.get("artifacts") or []:
            path = artifact.get("path")
            source_path = str(path) if isinstance(path, Path) else normalize_ws(path)
            rows.append(
                {
                    "subquestion_id": article["subquestion_id"],
                    "article_title": article["title"],
                    "doi": article["doi"],
                    "kind": artifact.get("kind", ""),
                    "filename": artifact.get("filename", ""),
                    "source_path": source_path,
                    "rel_to_run": rel(path, run_dir) if isinstance(path, Path) else source_path,
                    "rel_to_article": artifact.get("rel_to_article", ""),
                    "size_bytes": artifact.get("size_bytes", ""),
                    "sha256": artifact.get("sha256", ""),
                }
            )
    with manifest_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_duplicate_report(run_dir: Path, out_dir: Path, articles: list[dict[str, Any]]) -> None:
    grouped = group_canonical_articles(articles)
    lines = [
        "# Duplicate Report",
        "",
        "Duplicate groups are folded by DOI when available, otherwise by normalized title. Paths point back to the original run capture folders.",
        "",
    ]
    duplicate_items = [(key, group) for key, group in grouped.items() if len(group) > 1]
    if not duplicate_items:
        lines.append("No duplicate canonical paper occurrences found.")
        write_text(out_dir / "papers" / "duplicate-report.md", "\n".join(lines))
        return
    for key, group in sorted(duplicate_items, key=lambda kv: choose_canonical_article(kv[1])["title"].lower()):
        canonical = choose_canonical_article(group)
        lines.extend(
            [
                f"## {canonical['title']}",
                "",
                f"- Canonical key: `{key}`",
                f"- DOI: `{canonical['doi'] or 'not recorded'}`",
                f"- Occurrences: `{len(group)}`",
                "",
            ]
        )
        for article in sorted(group, key=lambda a: rel(a["article_dir"], run_dir)):
            lines.append(
                f"- `{article['subquestion_id']}` {article['title']}"
                f" | DOI: `{article['doi'] or 'not recorded'}`"
                f" | role: `{article['source_role'] or 'not recorded'}`"
                f" | path: `{rel(article['article_dir'], run_dir)}`"
            )
        lines.append("")
    write_text(out_dir / "papers" / "duplicate-report.md", "\n".join(lines))


def write_subquestion_pages(
    run_dir: Path,
    out_dir: Path,
    articles_by_subq: dict[str, list[dict[str, Any]]],
    refs_by_subq: dict[str, list[dict[str, str]]],
    reference_ledgers: dict[str, list[dict[str, str]]],
    seed_ledgers: dict[str, list[dict[str, Any]]],
    manual_by_subq: dict[str, list[dict[str, str]]],
    manual_blocked_by_subq: dict[str, list[dict[str, str]]],
    coverage: dict[str, dict[str, Any]],
    summaries: dict[str, list[Path]],
    query_journeys: dict[str, dict[str, Any]],
) -> None:
    subquestions = sorted(
        set(articles_by_subq)
        | set(refs_by_subq)
        | set(reference_ledgers)
        | set(seed_ledgers)
        | set(manual_by_subq)
        | set(manual_blocked_by_subq)
        | set(coverage)
        | set(summaries)
        | set(query_journeys)
    )
    for subq in subquestions:
        subq_dir = out_dir / "subquestions" / subquestion_dir_name(subq)
        articles = articles_by_subq.get(subq, [])
        refs = refs_by_subq.get(subq, [])
        ledger_refs = reference_ledgers.get(subq, [])
        ledger_seeds = seed_ledgers.get(subq, [])
        manual = manual_by_subq.get(subq, [])
        manual_blocked = manual_blocked_by_subq.get(subq, [])
        cov = coverage.get(subq, {})
        summary_paths = summaries.get(subq, [])
        journey = query_journeys.get(subq, {})
        subq_text = next((a["subquestion_text"] for a in articles if a.get("subquestion_text")), normalize_ws(cov.get("subquestion_text")))
        canonical_groups = group_canonical_articles(articles)

        paper_lines = []
        note_lines = []
        seed_lines = []
        for article in articles:
            article_rel = rel(article["article_dir"], run_dir)
            label = article["title"]
            paper_lines.append(
                f"- **{label}** ({article['year'] or 'n.d.'}; {article['publisher'] or article['source_bucket'] or 'unknown'}; {article['source_role']})"
                f" | DOI: `{article['doi'] or 'not recorded'}`"
                f" | status: `{article_status(article)}`"
                f" | path: `{article_rel}`"
            )
            if article.get("note_path"):
                note_lines.append(f"- {label} -> `{rel(article['note_path'], run_dir)}`")
                for seed in extract_seed_lines(article["note_path"]):
                    seed_lines.append(f"- {seed} _(from {label})_")

        ref_lines = []
        for ref in refs:
            title = normalize_ws(ref.get("reference_title") or ref.get("title") or ref.get("target_title") or ref.get("reference_text"))
            doi = normalize_ws(ref.get("doi") or ref.get("target_doi"))
            ref_lines.append(f"- {title or 'Untitled reference'} | DOI: `{doi or 'not recorded'}` | source: {normalize_ws(ref.get('_source_article_title'))} | ledger: `article recommended-references`")
        for ref in ledger_refs:
            title = normalize_ws(ref.get("title") or ref.get("crossref_title") or ref.get("capture_query") or ref.get("reference_text"))
            doi = normalize_ws(ref.get("doi") or ref.get("crossref_doi") or ref.get("parsed_doi"))
            action = normalize_ws(ref.get("action") or ref.get("capture_hint") or ref.get("route"))
            source = normalize_ws(ref.get("_ledger_file"))
            ref_lines.append(f"- {title or 'Untitled reference'} | DOI: `{doi or 'not recorded'}` | action/route: `{action or 'not recorded'}` | ledger: `{source}`")

        ledger_seed_lines = []
        for seed in ledger_seeds:
            text = normalize_ws(seed.get("seed_text") or seed.get("text") or seed.get("title"))
            kind = normalize_ws(seed.get("seed_kind") or seed.get("seed_role"))
            action = normalize_ws(seed.get("recommended_action"))
            query = normalize_ws(seed.get("proposed_short_query"))
            source_title = normalize_ws(seed.get("source_article_title"))
            ledger_seed_lines.append(
                f"- **{kind or 'seed'}**: {text} | action: `{action or 'not recorded'}` | query: `{query or 'not recorded'}` | source: {source_title or 'not recorded'}"
            )

        manual_lines = []
        manual_download_lines = []
        manual_supplied_lines = []
        for row in manual:
            line = (
                f"- **{normalize_ws(row.get('title'))}** | DOI: `{normalize_ws(row.get('doi'))}`"
                f" | triage: `{row.get('_bucket')}`"
                f" | status: `{row.get('_manual_status')}`"
                f" | dropbox: `{row.get('_dropbox')}`"
                f" | suggested filename: `{row.get('_dropbox_suggested_filename')}`"
            )
            manual_lines.append(line)
            if row.get("_manual_status") == "needs_user_pdf":
                manual_download_lines.append(line)
            else:
                manual_supplied_lines.append(line)
        manual_blocked_lines = []
        for row in manual_blocked:
            manual_blocked_lines.append(
                f"- {normalize_ws(row.get('title')) or 'Untitled'} | source: `{row.get('_source_file')}` | reason: missing DOI; not shown as a download task"
            )

        summary_lines = [f"- `{rel(path, run_dir)}`" for path in summary_paths]

        coverage_body = [
            f"# Coverage - {subq}",
            "",
            f"- Decision: `{normalize_ws(cov.get('coverage_decision')) or 'not recorded'}`",
            f"- Score: `{normalize_ws(cov.get('coverage_score_0_to_5')) or 'not recorded'}`",
            f"- Stage status: `{normalize_ws(cov.get('coverage_stage_status')) or 'not recorded'}`",
            "",
            "## Remaining Gaps",
            "",
            normalize_ws(cov.get("remaining_gaps") or cov.get("gaps") or "not recorded"),
        ]
        write_text(subq_dir / "coverage.md", "\n".join(coverage_body))
        write_text(subq_dir / "captured_papers.md", "# Captured Papers\n\n" + ("\n".join(paper_lines) if paper_lines else "No captured papers found."))
        write_text(subq_dir / "reading_notes_index.md", "# Reading Notes Index\n\n" + ("\n".join(note_lines) if note_lines else "No reading notes found."))
        combined_seed_lines = ledger_seed_lines + list(dict.fromkeys(seed_lines))
        write_text(subq_dir / "important_seeds.md", "# Important Seeds\n\n" + ("\n".join(combined_seed_lines) if combined_seed_lines else "No seed lines extracted from notes or seed ledgers."))
        write_text(subq_dir / "recommended_references.md", "# Recommended References\n\n" + ("\n".join(ref_lines) if ref_lines else "No recommended references found."))
        manual_body = [
            "# Manual PDF Needed",
            "",
            "Only rows with DOI are shown as download tasks.",
            "",
            "Put manually downloaded PDFs for this subquestion in `manual_pdf_dropbox/`. Use `manual_pdf_to_download.csv` for the short action list and `manual_pdf_download_list.csv` for the full DOI-backed audit list.",
            "",
            "## Needs User PDF",
            "",
        ]
        manual_body.append("\n".join(manual_download_lines) if manual_download_lines else "No user-downloaded PDFs currently needed.")
        manual_body.extend(["", "## Already Supplied, Needs MinerU/Note", ""])
        manual_body.append("\n".join(manual_supplied_lines) if manual_supplied_lines else "No already supplied manual PDFs waiting in canonical targets.")
        manual_body.extend(["", "## Full DOI-Backed Audit", ""])
        manual_body.append("\n".join(manual_lines) if manual_lines else "No DOI-backed manual PDFs currently recorded.")
        if manual_blocked_lines:
            manual_body.extend(["", "## Metadata Blocked (No DOI, Not a Download Task)", "", "\n".join(manual_blocked_lines)])
        write_text(subq_dir / "manual_pdf_needed.md", "\n".join(manual_body))
        write_text(subq_dir / "subquestion_summaries.md", "# Subquestion Summaries\n\n" + ("\n".join(summary_lines) if summary_lines else "No subquestion summary files found."))
        write_query_journey_page(run_dir, subq_dir, subq, journey)
        write_subquestion_overview(
            subq_dir,
            subq,
            subq_text,
            articles,
            canonical_groups,
            refs,
            ledger_refs,
            ledger_seeds,
            manual,
            manual_blocked,
            cov,
        )
        write_subquestion_papers(run_dir, subq_dir, subq, canonical_groups)
        write_subquestion_artifact_index(run_dir, subq_dir, subq, articles)
        write_text(
            subq_dir / "index.md",
            "\n".join(
                [
                    frontmatter(type="subquestion_knowledge_index", subquestion=subq, status="staging"),
                    f"# {subq}",
                    "",
                    subq_text or "Subquestion text not recorded.",
                    "",
                    "## Entry Points",
                    "",
                    "- [Overview](overview.md)",
                    "- [Canonical papers](papers.md)",
                    "- [Paper cards](paper_cards/)",
                    "- [Figures and tables](figures_tables.md)",
                    "- [Captured papers](captured_papers.md)",
                    "- [Reading notes](reading_notes_index.md)",
                    "- [Important seeds](important_seeds.md)",
                    "- [Recommended references](recommended_references.md)",
                    "- [Query/search journey](query_journey.md)",
                    "- [Manual PDF needed](manual_pdf_needed.md)",
                    "- [Manual PDF dropbox](manual_pdf_dropbox/README.md)",
                    "- [Coverage](coverage.md)",
                    "- [Subquestion summaries](subquestion_summaries.md)",
                    "",
                    "## Current Counts",
                    "",
                    f"- Captured article folders: {len(articles)}",
                    f"- Canonical papers: {len(canonical_groups)}",
                    f"- Artifact files indexed: {sum(len(a.get('artifacts') or []) for a in articles)}",
                    f"- Article recommended-reference rows: {len(refs)}",
                    f"- Root/reference ledger rows: {len(ledger_refs)}",
                    f"- Seed-ledger rows: {len(ledger_seeds)}",
                    f"- DOI-backed manual PDF rows: {len(manual)}",
                    f"- No-DOI manual rows held out: {len(manual_blocked)}",
                    f"- Coverage decision: `{normalize_ws(cov.get('coverage_decision')) or 'not recorded'}`",
                ]
            ),
        )


def write_root_pages(
    run_dir: Path,
    out_dir: Path,
    articles: list[dict[str, Any]],
    refs_by_subq: dict[str, list[dict[str, str]]],
    reference_ledgers: dict[str, list[dict[str, str]]],
    seed_ledgers: dict[str, list[dict[str, Any]]],
    manual_by_subq: dict[str, list[dict[str, str]]],
    manual_blocked_by_subq: dict[str, list[dict[str, str]]],
    coverage: dict[str, dict[str, Any]],
    query_journeys: dict[str, dict[str, Any]],
) -> None:
    subquestions = sorted(
        set(a["subquestion_id"] for a in articles)
        | set(refs_by_subq)
        | set(reference_ledgers)
        | set(seed_ledgers)
        | set(manual_by_subq)
        | set(manual_blocked_by_subq)
        | set(coverage)
        | set(query_journeys)
    )
    lines = [
        "# Knowledge Staging",
        "",
        "This folder is the human-facing information layer for the literature run. It does not replace canonical capture folders; it indexes them by subquestion and provides clean manual PDF dropboxes.",
        "",
        "## Subquestions",
        "",
    ]
    for subq in subquestions:
        lines.append(f"- [{subq}](subquestions/{subquestion_dir_name(subq)}/index.md)")
    lines.extend(
        [
            "",
            "## Global Indexes",
            "",
            "- [Papers](papers/index.md)",
            "- [Canonical paper index](papers/canonical-index.md)",
            "- [Duplicate paper report](papers/duplicate-report.md)",
            "- [Seeds](seeds/seed-ledger.md)",
            "- [References](references/reference-ledger.md)",
            "- [Search/query journey](search_journey.md)",
            "- [Coverage](overview/coverage.md)",
            "- [Obsidian export staging](obsidian_export/README.md)",
            "- [Attachment manifest](obsidian_export/attachments_manifest.csv)",
            "",
            "## Manual PDF Rule",
            "",
            "Only DOI-backed manual PDF rows are shown in subquestion dropboxes. Rows without DOI remain in the machine audit files until metadata is clarified.",
            "",
            "## Current Totals",
            "",
            f"- Article folders indexed: {len(articles)}",
            f"- Canonical paper keys: {len({article_key(a) for a in articles})}",
            f"- Reading-note recommended-reference rows: {sum(len(v) for v in refs_by_subq.values())}",
            f"- Root/reference ledger rows: {sum(len(v) for v in reference_ledgers.values())}",
            f"- Seed-ledger rows: {sum(len(v) for v in seed_ledgers.values())}",
            f"- DOI-backed manual PDF dropbox rows: {sum(len(v) for v in manual_by_subq.values())}",
            f"- No-DOI manual rows held out: {sum(len(v) for v in manual_blocked_by_subq.values())}",
        ]
    )
    write_text(out_dir / "README.md", "\n".join(lines))

    paper_lines = []
    for article in articles:
        paper_lines.append(
            f"- **{article['title']}** | subquestion: `{article['subquestion_id']}` | role: `{article['source_role']}` | status: `{article_status(article)}` | DOI: `{article['doi'] or 'not recorded'}` | path: `{rel(article['article_dir'], run_dir)}`"
        )
    write_text(out_dir / "papers" / "index.md", "# Papers\n\n" + ("\n".join(paper_lines) if paper_lines else "No papers found."))

    grouped = group_canonical_articles(articles)
    canonical_lines = [
        "# Canonical Paper Index",
        "",
        "This view folds duplicate capture folders by DOI when possible, otherwise by normalized title. Raw occurrences remain in `papers/index.md`.",
        "",
    ]
    for key, group in sorted(grouped.items(), key=lambda kv: (kv[1][0]["subquestion_id"], kv[1][0]["title"].lower())):
        best = choose_canonical_article(group)
        canonical_lines.append(
            f"- **{best['title']}** | subquestion: `{best['subquestion_id']}` | canonical status: `{article_status(best)}` | DOI: `{best['doi'] or 'not recorded'}` | occurrences: `{len(group)}`"
        )
        if len(group) > 1:
            for article in group:
                canonical_lines.append(f"  - occurrence: `{article_status(article)}` `{rel(article['article_dir'], run_dir)}`")
    write_text(out_dir / "papers" / "canonical-index.md", "\n".join(canonical_lines))
    write_duplicate_report(run_dir, out_dir, articles)

    seed_lines = []
    for subq, seeds in sorted(seed_ledgers.items()):
        for seed in seeds:
            text = normalize_ws(seed.get("seed_text") or seed.get("text") or seed.get("title"))
            kind = normalize_ws(seed.get("seed_kind") or seed.get("seed_role"))
            action = normalize_ws(seed.get("recommended_action"))
            query = normalize_ws(seed.get("proposed_short_query"))
            seed_lines.append(f"- `{subq}` **{kind or 'seed'}**: {text} | action: `{action or 'not recorded'}` | query: `{query or 'not recorded'}`")
    for article in articles:
        for seed in extract_seed_lines(article.get("note_path")):
            seed_lines.append(f"- `{article['subquestion_id']}` {seed} _(from {article['title']})_")
    write_text(out_dir / "seeds" / "seed-ledger.md", "# Seed Ledger\n\n" + ("\n".join(list(dict.fromkeys(seed_lines))) if seed_lines else "No seed lines extracted."))

    ref_lines = []
    for subq, refs in sorted(refs_by_subq.items()):
        for ref in refs:
            title = normalize_ws(ref.get("reference_title") or ref.get("title") or ref.get("reference_text"))
            ref_lines.append(f"- `{subq}` {title or 'Untitled reference'} | DOI: `{normalize_ws(ref.get('doi')) or 'not recorded'}` | ledger: `article recommended-references`")
    for subq, refs in sorted(reference_ledgers.items()):
        for ref in refs:
            title = normalize_ws(ref.get("title") or ref.get("crossref_title") or ref.get("capture_query") or ref.get("reference_text"))
            doi = normalize_ws(ref.get("doi") or ref.get("crossref_doi") or ref.get("parsed_doi"))
            action = normalize_ws(ref.get("action") or ref.get("capture_hint") or ref.get("route"))
            source = normalize_ws(ref.get("_ledger_file"))
            ref_lines.append(f"- `{subq}` {title or 'Untitled reference'} | DOI: `{doi or 'not recorded'}` | action/route: `{action or 'not recorded'}` | ledger: `{source}`")
    write_text(out_dir / "references" / "reference-ledger.md", "# Reference Ledger\n\n" + ("\n".join(ref_lines) if ref_lines else "No references found."))

    journey_lines = [
        "# Search / Query Journey",
        "",
        "This index treats query iteration as research evidence: it records how earlier queries, reading-note seeds, gaps, OpenAlex grounding, and user corrections shaped later searches.",
        "",
    ]
    for subq in subquestions:
        journey = query_journeys.get(subq, {})
        iterations = journey.get("iterations") or []
        broad_count = sum(len(it.get("broad_queries") or []) for it in iterations)
        exact_count = sum(len(it.get("exact_targets") or []) for it in iterations)
        journey_lines.append(
            f"- [{subq}](subquestions/{subquestion_dir_name(subq)}/query_journey.md) | initial queries: `{len(journey.get('initial_queries') or [])}` | iterations: `{len(iterations)}` | broad rows: `{broad_count}` | exact/manual targets: `{exact_count}`"
        )
    write_text(out_dir / "search_journey.md", "\n".join(journey_lines))

    cov_lines = []
    for subq, cov in sorted(coverage.items()):
        cov_lines.append(
            f"- `{subq}` decision: `{normalize_ws(cov.get('coverage_decision')) or 'not recorded'}`; score: `{normalize_ws(cov.get('coverage_score_0_to_5')) or 'not recorded'}`"
        )
    write_text(out_dir / "overview" / "coverage.md", "# Coverage Overview\n\n" + ("\n".join(cov_lines) if cov_lines else "No coverage review found."))
    write_text(
        out_dir / "obsidian_export" / "README.md",
        "\n".join(
            [
                "# Obsidian Export Staging",
                "",
                "After manual PDFs are ingested, MinerU-normalized, reading notes are written, and coverage/overview are refreshed, this run can be exported into an Obsidian vault.",
                "",
                "Expected Obsidian information groups:",
                "",
                "- Subquestions: one entry note per subquestion",
                "- Papers: one note per captured primary/reference/PDF article",
                "- Seeds: reusable resources, methods, datasets, benchmarks, and gaps",
                "- References: recommended and captured second-level literature",
                "- Search journeys: how query intent, confidence, exact targets, manual holds, and broad discovery evolved",
                "- Attachment manifest: `attachments_manifest.csv` lists PDFs, figures, and tables in place",
                "",
                "Attachments are indexed but not copied into this staging folder.",
            ]
        ),
    )


def build_knowledge(run_dir: Path) -> Path:
    run_dir = run_dir.resolve()
    out_dir = run_dir / "_knowledge"
    articles = load_articles(run_dir)
    articles_by_subq: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for article in articles:
        articles_by_subq[article["subquestion_id"]].append(article)
    refs_by_subq = collect_recommended_references(articles)
    reference_ledgers = load_reference_ledgers(run_dir)
    seed_ledgers = load_seed_ledgers(run_dir)
    query_journeys = load_query_journeys(run_dir)
    manual_by_bucket, manual_blocked_by_subq = load_manual_rows(run_dir)
    manual_subquestions = {
        normalize_ws(row.get("subquestion_id")) or "unknown_subquestion"
        for rows in manual_by_bucket.values()
        for row in rows
    }
    active_subquestions = (
        set(articles_by_subq)
        | set(refs_by_subq)
        | set(reference_ledgers)
        | set(seed_ledgers)
        | set(manual_subquestions)
        | set(manual_blocked_by_subq)
        | set(load_coverage(run_dir))
        | set(load_subquestion_summaries(run_dir))
        | set(query_journeys)
    )
    prepare_knowledge_output(out_dir, active_subquestions)
    clean_empty_manual_inbox_items(out_dir)
    manual_by_subq = write_manual_inbox(run_dir, out_dir, manual_by_bucket)
    coverage = load_coverage(run_dir)
    summaries = load_subquestion_summaries(run_dir)
    write_subquestion_pages(
        run_dir,
        out_dir,
        articles_by_subq,
        refs_by_subq,
        reference_ledgers,
        seed_ledgers,
        manual_by_subq,
        manual_blocked_by_subq,
        coverage,
        summaries,
        query_journeys,
    )
    write_root_pages(
        run_dir,
        out_dir,
        articles,
        refs_by_subq,
        reference_ledgers,
        seed_ledgers,
        manual_by_subq,
        manual_blocked_by_subq,
        coverage,
        query_journeys,
    )
    write_attachment_manifest(run_dir, out_dir, articles)
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "article_count": len(articles),
        "subquestion_count": len(
            set(articles_by_subq)
            | set(refs_by_subq)
            | set(reference_ledgers)
            | set(seed_ledgers)
            | set(manual_by_subq)
            | set(manual_blocked_by_subq)
            | set(coverage)
            | set(summaries)
            | set(query_journeys)
        ),
        "article_recommended_reference_count": sum(len(v) for v in refs_by_subq.values()),
        "root_reference_ledger_count": sum(len(v) for v in reference_ledgers.values()),
        "seed_ledger_count": sum(len(v) for v in seed_ledgers.values()),
        "manual_pdf_doi_backed_count": sum(len(v) for v in manual_by_subq.values()),
        "manual_pdf_no_doi_blocked_count": sum(len(v) for v in manual_blocked_by_subq.values()),
        "query_journey_subquestion_count": len(query_journeys),
        "output_dir": str(out_dir),
    }
    write_text(out_dir / "knowledge-staging-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return out_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Build _knowledge staging for a literature run.")
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    out_dir = build_knowledge(args.run_dir)
    print(f"Knowledge staging written to: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
