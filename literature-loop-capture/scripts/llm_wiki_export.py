#!/usr/bin/env python3
"""Export a literature run as an llm_wiki project or legacy raw sources."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT_EXPORT_FILES = [
    ("overview.md", "final_overview", "raw_sources/final/overview.md"),
    ("subquestion-final-summaries-zh.md", "final_subquestion_summaries", "raw_sources/final/subquestion-final-summaries-zh.md"),
    ("_knowledge/search_journey.md", "query_journey", "raw_sources/search_journey.md"),
    ("_knowledge/papers/duplicate-report.md", "duplicate_report", "raw_sources/papers/duplicate-report.md"),
    ("_knowledge/papers/canonical-index.md", "paper_index", "raw_sources/papers/canonical-index.md"),
    ("_knowledge/seeds/seed-ledger.md", "seed_ledger", "raw_sources/seeds/seed-ledger.md"),
    ("_knowledge/references/reference-ledger.md", "reference_ledger", "raw_sources/references/reference-ledger.md"),
    ("_knowledge/overview/coverage.md", "coverage", "raw_sources/coverage.md"),
]

SUBQUESTION_FILES = [
    "overview.md",
    "papers.md",
    "reading_notes_index.md",
    "important_seeds.md",
    "recommended_references.md",
    "query_journey.md",
    "coverage.md",
    "figures_tables.md",
    "subquestion_summaries.md",
]

PROJECT_ARTICLE_PROVENANCE_FILES = [
    "metadata.json",
    "fulltext.json",
    "fulltext.md",
    "captured-fulltext.md",
    "structure.json",
    "references.json",
    "recommended-references.csv",
    "recommended-references.json",
    "reading-note-zh.md",
    "references.md",
    "recommended-references.md",
]

PROJECT_ARTICLE_SOURCE_FILES = [
    "article.md",
]

PROJECT_DOSSIER_ROOT_FILES = [
    "overview.md",
    "subquestion-final-summaries-zh.md",
    "_knowledge/README.md",
    "_knowledge/search_journey.md",
    "_knowledge/papers/duplicate-report.md",
    "_knowledge/papers/index.md",
    "_knowledge/papers/canonical-index.md",
    "_knowledge/seeds/seed-ledger.md",
    "_knowledge/references/reference-ledger.md",
    "_knowledge/overview/coverage.md",
    "_knowledge/knowledge-staging-manifest.json",
]


def normalize_ws(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def safe_slug(text: str, fallback: str = "item", limit: int = 96) -> str:
    text = normalize_ws(text)
    text = text.replace("/", "-")
    text = re.sub(r"[\\:*?\"<>|]+", " ", text)
    text = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff._-]+", "-", text)
    text = text.strip(".-_").lower()
    return (text[:limit].strip(".-_") or fallback)


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


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def load_knowledge_staging_module() -> Any:
    path = Path(__file__).with_name("knowledge_staging.py")
    spec = importlib.util.spec_from_file_location("knowledge_staging_for_llm_wiki", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot load knowledge_staging.py from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def yaml_value(value: Any) -> str:
    return json.dumps(str(value or ""), ensure_ascii=False)


def frontmatter(row: dict[str, Any]) -> str:
    lines = ["---"]
    for key in [
        "source_run",
        "source_path",
        "source_kind",
        "subquestion_id",
        "title",
        "doi",
        "publisher",
        "article_dir",
    ]:
        value = row.get(key)
        if value:
            lines.append(f"{key}: {yaml_value(value)}")
    lines.append("---")
    return "\n".join(lines)


def copy_with_frontmatter(
    source: Path,
    target: Path,
    *,
    run_dir: Path,
    kind: str,
    subquestion_id: str = "",
    title: str = "",
    doi: str = "",
    publisher: str = "",
    article_dir: str = "",
) -> dict[str, Any]:
    source = source.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = source.read_text(encoding="utf-8", errors="replace")
    row = {
        "source_run": run_dir.name,
        "source_path": rel(source, run_dir),
        "source_kind": kind,
        "subquestion_id": subquestion_id,
        "title": title,
        "doi": doi,
        "publisher": publisher,
        "article_dir": article_dir,
        "export_path": rel(target, target.parents[2]) if len(target.parents) > 2 else str(target),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    target.write_text(frontmatter(row) + "\n\n" + body.rstrip() + "\n", encoding="utf-8")
    return row


def rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def posix_rel(path: Path, root: Path) -> str:
    return rel(path, root).replace("\\", "/")


def markdown_relative_link(from_file: Path, target: Path) -> str:
    return Path(os.path.relpath(target, from_file.parent)).as_posix()


def append_link_if_exists(lines: list[str], from_file: Path, label: str, target: Path) -> None:
    if target.exists():
        lines.append(f"- [{label}]({markdown_relative_link(from_file, target)})")


def copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def read_text_if_exists(path: Path, limit_chars: int | None = None) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if limit_chars and len(text) > limit_chars:
        return text[:limit_chars].rstrip() + "\n\n...[truncated in wiki page; full file is in raw/sources]"
    return text


def append_optional_markdown_section(lines: list[str], title: str, path: Path, empty_message: str = "") -> None:
    text = read_text_if_exists(path)
    if not text and not empty_message:
        return
    lines.extend(["", f"## {title}", ""])
    if text:
        lines.append(text.rstrip())
    else:
        lines.append(empty_message)


def clean_article_text(text: Any) -> str:
    text = str(text or "")
    text = text.replace("Click to copy section linkSection link copied!", "")
    text = text.replace("Section link copied!", "")
    return text.strip()


REFERENCE_SECTION_RE = re.compile(
    r"(?im)^\s{0,3}(?:#{1,6}\s*)?(references?|reference list|bibliography|literature cited|works cited|参考文献)\s*$"
)


def strip_reference_sections(text: str) -> str:
    """Remove tail reference lists from article ingest text.

    Reference files remain available in provenance/dossier ledgers; article.md
    should focus LLM Wiki chunks on the captured body and reading note.
    """
    text = text.rstrip()
    min_start = max(800, int(len(text) * 0.25))
    for match in REFERENCE_SECTION_RE.finditer(text):
        if match.start() >= min_start:
            return text[: match.start()].rstrip()
    return text


def full_article_abstract(article_dir: Path) -> str:
    metadata = read_json(article_dir / "metadata.json", {})
    fulltext = read_json(article_dir / "fulltext.json", {})
    for source in [fulltext, metadata]:
        value = source.get("abstract") or source.get("description")
        if value:
            return clean_article_text(value)
    return ""


def article_body_markdown(article_dir: Path) -> tuple[str, str]:
    for filename in ["captured-fulltext.md", "fulltext.md"]:
        path = article_dir / filename
        if path.exists():
            return strip_reference_sections(clean_article_text(path.read_text(encoding="utf-8", errors="replace"))), filename
    fulltext = read_json(article_dir / "fulltext.json", {})
    if fulltext.get("fullText"):
        return strip_reference_sections(clean_article_text(fulltext.get("fullText"))), "fulltext.json:fullText"
    section_blocks = fulltext.get("sectionBlocks")
    if isinstance(section_blocks, list) and section_blocks:
        parts: list[str] = []
        for block in section_blocks:
            if not isinstance(block, dict):
                continue
            title = normalize_ws(block.get("title"))
            text = clean_article_text(block.get("text"))
            if title:
                parts.append(f"## {title}")
            if text:
                parts.append(text)
        return strip_reference_sections("\n\n".join(parts).strip()), "fulltext.json:sectionBlocks"
    return "", ""


def write_article_ingest_markdown(target: Path, article: dict[str, Any], run_dir: Path, project_dir: Path, assets: list[dict[str, str]] | None = None) -> None:
    article_dir = article["article_dir"]
    metadata = read_json(article_dir / "metadata.json", {})
    fulltext = read_json(article_dir / "fulltext.json", {})
    abstract = full_article_abstract(article_dir)
    body, body_source = article_body_markdown(article_dir)
    front = source_page_frontmatter(
        {
            "type": "article_source",
            "title": article.get("title", ""),
            "doi": article.get("doi", ""),
            "year": article.get("year", ""),
            "publisher": article.get("publisher", ""),
            "venue": article.get("venue", ""),
            "subquestion_id": article.get("subquestion_id", ""),
            "source_role": article.get("source_role", ""),
            "canonical_article_dir": posix_rel(article_dir, run_dir),
            "body_source": body_source,
            "metadata_title": metadata.get("title") or fulltext.get("title") or fulltext.get("documentTitle"),
        }
    )
    lines = [
        front,
        "",
        f"# {article.get('title') or 'Untitled Article'}",
        "",
        "## Bibliographic Metadata",
        "",
        f"- DOI: `{article.get('doi') or 'not recorded'}`",
        f"- Year: `{article.get('year') or 'not recorded'}`",
        f"- Publisher: `{article.get('publisher') or 'not recorded'}`",
        f"- Venue: `{article.get('venue') or 'not recorded'}`",
        f"- Source role: `{article.get('source_role') or 'not recorded'}`",
        f"- Original capture folder: `{posix_rel(article_dir, run_dir)}`",
        "",
    ]
    if abstract:
        lines.extend(["## Abstract", "", abstract, ""])
    lines.extend(["## Local Assets", ""])
    if assets:
        for asset in assets:
            asset_path = Path(asset["export_path"])
            target_path = asset_path if asset_path.is_absolute() else project_dir / asset_path
            link = markdown_relative_link(target, target_path)
            label = Path(asset["export_path"]).name
            if asset.get("asset_kind") == "figure" and Path(label).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                lines.append(f"- `{asset.get('asset_kind')}` {label}\n\n  ![{label}]({link})")
            else:
                lines.append(f"- `{asset.get('asset_kind') or 'asset'}` [{label}]({link})")
    else:
        lines.append("No figure/table assets found for this article occurrence.")
    lines.append("")
    lines.extend(["## Full Text", ""])
    lines.append(body or "No Markdown/fullText body was found for this article occurrence.")
    append_optional_markdown_section(
        lines,
        "Reading Note",
        article_dir / "reading-note-zh.md",
        "No reading-note-zh.md found for this article occurrence.",
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def project_article_slug(ks: Any, key: str, article: dict[str, Any]) -> str:
    doi = normalize_ws(article.get("doi"))
    if doi:
        base = ks.doi_slug(doi)
    else:
        base = safe_slug(article.get("title", ""), "paper", 80)
    return f"{base}--{ks.short_hash(key, 8)}"


def source_role_rank(article: dict[str, Any]) -> int:
    role = normalize_ws(article.get("source_role")).lower()
    if role == "primary":
        return 0
    if role == "reference":
        return 1
    return 2


def source_page_frontmatter(values: dict[str, Any]) -> str:
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


def write_project_root_docs(project_dir: Path, run_dir: Path, subquestions: list[str]) -> None:
    purpose = [
        "# FOODKG Literature Evidence Wiki",
        "",
        f"Project export generated from `{run_dir.name}`.",
        "",
        "Research objective: review how to construct a food flavor knowledge graph, preserving captured articles, reading notes, figures, tables, important seeds, recommended references, coverage decisions, and query-iteration reasoning as traceable evidence.",
        "",
        "Use this project in LLM Wiki as a local research knowledge base. `raw/sources/` is the primary ingest surface, `raw/assets/` stores figures/tables linked from article Markdown without entering the text ingest queue, `raw/provenance/` stores machine and audit files, and `wiki/` contains only lightweight navigation and research-process skeleton pages. Article-level source pages, concepts, entities, related links, and graph connections are generated by LLM Wiki after ingest.",
        "",
        "Subquestions:",
    ]
    purpose.extend([f"- `{subq}`" for subq in subquestions] or ["- not recorded"])
    schema = """# Schema

## Page Types

- `synthesis`: final overview and cross-subquestion conclusions.
- `subquestion_process`: lightweight navigation for one research subquestion, linking to raw dossier pages, raw article ingest files, seeds, references, and unresolved boundaries.
- `query_history`: lightweight process pages for query plans, broad discovery terms, exact target ledgers, user comments, and coverage-driven iteration reasoning.
- `ledger`: lightweight seed, reference, duplicate, manual-hold, and attachment audit pages.

## Article Raw Source Frontmatter

- `type`: `article_source` for `raw/sources/articles/**/article.md`.
- `title`, `doi`, `year`, `publisher`, `venue`: bibliographic metadata when available.
- `subquestion_id`: subquestion where this article occurrence was captured.
- `source_role`: primary, reference, manual, or another capture role.
- `canonical_article_dir`: original capture folder relative to the literature run.
- `body_source`: capture artifact used for the article body, such as `captured-fulltext.md` or `fulltext.json:sectionBlocks`.

## Evidence Rules

`raw/sources/articles/**/article.md` is the canonical ingest text for each article occurrence. It should contain bibliographic metadata, abstract, local asset links, full text, and reading-note context. Do not append full `references.md` or `recommended-references.md` sections to article ingest pages; they inflate LLM Wiki chunks and usually duplicate citation strings rather than source evidence. Reference files stay in `raw/provenance/**` or curated dossier ledgers for audit and follow-up.

`raw/sources/dossier/**` contains curated process and synthesis evidence: subquestion overviews, query journeys, coverage, seeds, references, duplicate reports, and final summaries.

`raw/assets/articles/**` stores extracted figures and raw CSV tables. Article Markdown may link or embed these assets, but binaries and CSV tables must not be copied under `raw/sources/`. Source PDFs are intentionally not copied into the LLM Wiki project because normalized Markdown is the ingest source and the canonical PDF remains in the capture folder.

`raw/provenance/**` stores duplicate machine outputs such as `fulltext.json`, `captured-fulltext.md`, `structure.json`, and audit CSV/JSON files. Treat these as provenance, not primary ingest material.

`wiki/**` is a lightweight navigation layer. It should not contain exporter-authored article source summaries. LLM Wiki generates article source pages, concepts, entities, and related links from raw source ingest.
"""
    readme = """# LLM Wiki Project Export

Open this folder as an LLM Wiki project or copy it into an existing LLM Wiki project after review.

- `purpose.md`: project purpose and subquestions.
- `schema.md`: page types and evidence rules.
- `wiki/index.md`, `wiki/overview.md`, `wiki/log.md`: lightweight project entry points.
- `wiki/subquestions/`, `wiki/queries/`, `wiki/ledgers/`: process navigation pages only.
- `raw/sources/`: curated Markdown source material for LLM Wiki ingest.
- `raw/assets/`: per-article figures and tables linked from `article.md`, kept outside `raw/sources`.
- `raw/provenance/`: source JSON/CSV and duplicate full-text files retained for audit.
- `manifest.csv` / `manifest.json`: provenance map.
"""
    (project_dir / "purpose.md").write_text("\n".join(purpose).rstrip() + "\n", encoding="utf-8")
    (project_dir / "schema.md").write_text(schema.rstrip() + "\n", encoding="utf-8")
    (project_dir / "README.md").write_text(readme.rstrip() + "\n", encoding="utf-8")


def copy_project_dossier_sources(project_dir: Path, run_dir: Path, manifest: list[dict[str, Any]]) -> None:
    raw_root = project_dir / "raw" / "sources" / "dossier"
    for rel_source in PROJECT_DOSSIER_ROOT_FILES:
        source = run_dir / rel_source
        if not source.exists() or not source.is_file():
            continue
        if source.suffix.lower() == ".md":
            target = raw_root / "root" / rel_source.replace("_knowledge/", "")
            kind = "dossier"
        else:
            target = project_dir / "raw" / "provenance" / "dossier" / "root" / rel_source.replace("_knowledge/", "")
            kind = "dossier_provenance"
        copy_file(source, target)
        manifest.append(
            {
                "kind": kind,
                "subquestion_id": "",
                "title": source.name,
                "doi": "",
                "publisher": "",
                "source_path": posix_rel(source, run_dir),
                "export_path": posix_rel(target, project_dir),
                "asset_kind": "",
                "sha256": "",
            }
        )
    knowledge_subquestions = run_dir / "_knowledge" / "subquestions"
    if not knowledge_subquestions.exists():
        return
    for subq_dir in sorted(path for path in knowledge_subquestions.iterdir() if path.is_dir()):
        for filename in SUBQUESTION_FILES + ["captured_papers.md", "manual_pdf_needed.md", "index.md"]:
            source = subq_dir / filename
            if not source.exists() or not source.is_file():
                continue
            target = raw_root / "subquestions" / subq_dir.name / filename
            copy_file(source, target)
            manifest.append(
                {
                    "kind": "dossier_subquestion",
                    "subquestion_id": subq_dir.name,
                    "title": filename,
                    "doi": "",
                    "publisher": "",
                    "source_path": posix_rel(source, run_dir),
                    "export_path": posix_rel(target, project_dir),
                    "asset_kind": "",
                    "sha256": "",
                }
            )


def copy_article_bundle(
    project_dir: Path,
    run_dir: Path,
    article: dict[str, Any],
    article_slug: str,
    subq_slug: str,
    manifest: list[dict[str, Any]],
) -> tuple[Path, list[dict[str, str]]]:
    article_dir = article["article_dir"]
    raw_bundle = project_dir / "raw" / "sources" / "articles" / subq_slug / article_slug
    assets: list[dict[str, str]] = []
    for artifact in article.get("artifacts") or []:
        if not is_llm_wiki_export_asset(artifact):
            continue
        source = artifact.get("path")
        if not isinstance(source, Path) or not source.exists() or not source.is_file():
            continue
        rel_to_article = normalize_ws(artifact.get("rel_to_article")) or source.name
        target = project_dir / "raw" / "assets" / "articles" / subq_slug / article_slug / "assets" / rel_to_article
        copy_file(source, target)
        row = {
            "kind": "asset",
            "subquestion_id": article.get("subquestion_id", ""),
            "title": article.get("title", ""),
            "doi": article.get("doi", ""),
            "publisher": article.get("publisher", ""),
            "source_path": posix_rel(source, run_dir),
            "export_path": posix_rel(target, project_dir),
            "asset_kind": artifact.get("kind", ""),
            "sha256": artifact.get("sha256", ""),
        }
        manifest.append(row)
        assets.append(row)

    ingest_target = raw_bundle / "article.md"
    write_article_ingest_markdown(ingest_target, article, run_dir, project_dir, assets)
    manifest.append(
        {
            "kind": "article_source",
            "subquestion_id": article.get("subquestion_id", ""),
            "title": article.get("title", ""),
            "doi": article.get("doi", ""),
            "publisher": article.get("publisher", ""),
            "source_path": posix_rel(article_dir, run_dir),
            "export_path": posix_rel(ingest_target, project_dir),
            "asset_kind": "article.md",
            "sha256": "",
        }
    )

    provenance_bundle = project_dir / "raw" / "provenance" / "articles" / subq_slug / article_slug
    for filename in PROJECT_ARTICLE_PROVENANCE_FILES:
        source = article_dir / filename
        if not source.exists() or not source.is_file():
            continue
        target = provenance_bundle / filename
        copy_file(source, target)
        manifest.append(
            {
                "kind": "article_provenance",
                "subquestion_id": article.get("subquestion_id", ""),
                "title": article.get("title", ""),
                "doi": article.get("doi", ""),
                "publisher": article.get("publisher", ""),
                "source_path": posix_rel(source, run_dir),
                "export_path": posix_rel(target, project_dir),
                "asset_kind": filename,
                "sha256": "",
            }
        )

    return raw_bundle, assets


def is_llm_wiki_export_asset(artifact: dict[str, Any]) -> bool:
    kind = normalize_ws(artifact.get("kind")).lower()
    if kind == "pdf":
        return False
    source = artifact.get("path")
    return isinstance(source, Path) and source.exists() and source.is_file()


def should_export_article_occurrence(article: dict[str, Any]) -> bool:
    article_dir = article["article_dir"]
    body, _body_source = article_body_markdown(article_dir)
    if body:
        return True
    if (article_dir / "reading-note-zh.md").exists():
        return True
    return any(is_llm_wiki_export_asset(artifact) for artifact in (article.get("artifacts") or []))


def write_project_wiki_wrapper(
    page: Path,
    *,
    page_type: str,
    title: str,
    description: str,
    raw_target: Path,
    project_dir: Path,
    manifest: list[dict[str, Any]],
    subquestion_id: str = "",
) -> bool:
    if not raw_target.exists():
        return False
    front = source_page_frontmatter({"type": page_type, "subquestion_id": subquestion_id, "raw_source": posix_rel(raw_target, project_dir)})
    lines = [
        front,
        "",
        f"# {title}",
        "",
        description,
        "",
        "## Raw Dossier Source",
        "",
        f"- [{posix_rel(raw_target, project_dir)}]({markdown_relative_link(page, raw_target)})",
    ]
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    manifest.append(
        {
            "kind": "wiki",
            "subquestion_id": subquestion_id,
            "title": title,
            "doi": "",
            "publisher": "",
            "source_path": posix_rel(raw_target, project_dir),
            "export_path": posix_rel(page, project_dir),
            "asset_kind": "",
            "sha256": "",
        }
    )
    return True


def write_project_wiki_pages(
    project_dir: Path,
    run_dir: Path,
    subquestions: list[str],
    manifest: list[dict[str, Any]],
) -> None:
    wiki = project_dir / "wiki"
    for rel_dir in ["synthesis", "queries", "ledgers", "subquestions"]:
        (wiki / rel_dir).mkdir(parents=True, exist_ok=True)
    raw_dossier = project_dir / "raw" / "sources" / "dossier"
    write_project_wiki_wrapper(
        wiki / "synthesis" / "final-overview.md",
        page_type="synthesis",
        title="Final Overview",
        description="C-light navigation page for the final synthesis. Use the raw dossier source for full content.",
        raw_target=raw_dossier / "root" / "overview.md",
        project_dir=project_dir,
        manifest=manifest,
    )
    write_project_wiki_wrapper(
        wiki / "synthesis" / "subquestion-final-summaries-zh.md",
        page_type="synthesis",
        title="Subquestion Final Summaries",
        description="C-light navigation page for subquestion-level final summaries. Use the raw dossier source for full content.",
        raw_target=raw_dossier / "root" / "subquestion-final-summaries-zh.md",
        project_dir=project_dir,
        manifest=manifest,
    )
    write_project_wiki_wrapper(
        wiki / "queries" / "search-journey.md",
        page_type="query_history",
        title="Search Journey",
        description="C-light navigation page for project-level query planning and iteration.",
        raw_target=raw_dossier / "root" / "search_journey.md",
        project_dir=project_dir,
        manifest=manifest,
    )
    for raw_target, target, title in [
        (raw_dossier / "root" / "papers" / "duplicate-report.md", wiki / "ledgers" / "duplicate-report.md", "Duplicate Report"),
        (raw_dossier / "root" / "seeds" / "seed-ledger.md", wiki / "ledgers" / "seed-ledger.md", "Seed Ledger"),
        (raw_dossier / "root" / "references" / "reference-ledger.md", wiki / "ledgers" / "reference-ledger.md", "Reference Ledger"),
    ]:
        write_project_wiki_wrapper(
            target,
            page_type="ledger",
            title=title,
            description="C-light navigation page for a raw dossier ledger.",
            raw_target=raw_target,
            project_dir=project_dir,
            manifest=manifest,
        )
    overview_target = wiki / "overview.md"
    overview_lines = [
        "# FOODKG Literature Evidence Project",
        "",
        "This is a C-light LLM Wiki export. Raw sources are the ingest surface; this wiki folder is only navigation and research-process context.",
        "",
        "- [Purpose](../purpose.md)",
        "- [Schema](../schema.md)",
        "- [Raw sources](../raw/sources/)",
        "- [Raw assets](../raw/assets/)",
        "- [Raw provenance](../raw/provenance/)",
        "",
        "## Subquestions",
        "",
    ]
    overview_lines.extend([f"- [{subq}](subquestions/{safe_slug(subq, 'subquestion', 72)}.md)" for subq in subquestions] or ["- not recorded"])
    overview_target.write_text("\n".join(overview_lines).rstrip() + "\n", encoding="utf-8")

    index_lines = [
        "# FOODKG Literature Wiki",
        "",
        "- [Project overview](overview.md)",
    ]
    for label, target in [
        ("Final overview", wiki / "synthesis" / "final-overview.md"),
        ("Subquestion final summaries", wiki / "synthesis" / "subquestion-final-summaries-zh.md"),
        ("Search journey", wiki / "queries" / "search-journey.md"),
        ("Seed ledger", wiki / "ledgers" / "seed-ledger.md"),
        ("Reference ledger", wiki / "ledgers" / "reference-ledger.md"),
        ("Duplicate report", wiki / "ledgers" / "duplicate-report.md"),
        ("Raw article sources", project_dir / "raw" / "sources" / "articles"),
    ]:
        append_link_if_exists(index_lines, wiki / "index.md", label, target)
    index_lines.extend(["", "## Subquestions", ""])
    for subq in subquestions:
        page = wiki / "subquestions" / f"{safe_slug(subq, 'subquestion', 72)}.md"
        index_lines.append(f"- [{subq}](subquestions/{page.name})")
    (wiki / "index.md").write_text("\n".join(index_lines).rstrip() + "\n", encoding="utf-8")

    for subq in subquestions:
        subq_slug = safe_slug(subq, "subquestion", 72)
        subq_page = wiki / "subquestions" / f"{subq_slug}.md"
        query_target = wiki / "queries" / f"{subq_slug}-query-journey.md"
        raw_query = project_dir / "raw" / "sources" / "dossier" / "subquestions" / subq / "query_journey.md"
        write_project_wiki_wrapper(
            query_target,
            page_type="query_history",
            title=f"{subq} Query Journey",
            description="C-light navigation page for this subquestion's query process.",
            raw_target=raw_query,
            project_dir=project_dir,
            manifest=manifest,
            subquestion_id=subq,
        )
        lines = [
            source_page_frontmatter({"type": "subquestion_process", "subquestion_id": subq}),
            "",
            f"# {subq}",
            "",
            "This page is a C-light process navigator. Article source summaries are generated by LLM Wiki after ingesting the raw article files.",
            "",
            "## Raw Dossier Pages",
            "",
        ]
        for filename in ["overview.md", "papers.md", "reading_notes_index.md", "important_seeds.md", "recommended_references.md", "query_journey.md", "coverage.md", "figures_tables.md", "manual_pdf_needed.md", "subquestion_summaries.md"]:
            target = project_dir / "raw" / "sources" / "dossier" / "subquestions" / subq / filename
            if target.exists():
                lines.append(f"- [{filename}]({markdown_relative_link(subq_page, target)})")
        lines.extend(["", "## Query Process", ""])
        if query_target.exists():
            lines.append(f"- [query_journey.md]({markdown_relative_link(subq_page, query_target)})")
        else:
            if raw_query.exists():
                lines.append(f"- [query_journey.md]({markdown_relative_link(subq_page, raw_query)})")
            else:
                lines.append("- No query journey found for this subquestion.")
        raw_article_root = project_dir / "raw" / "sources" / "articles" / subq_slug
        lines.extend(["", "## Raw Article Sources", ""])
        if raw_article_root.exists():
            lines.append(f"- [raw/sources/articles/{subq_slug}]({markdown_relative_link(subq_page, raw_article_root)})")
        else:
            lines.append("- No raw article source folder found for this subquestion.")
        subq_page.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_project_log(project_dir: Path, run_dir: Path, manifest: list[dict[str, Any]]) -> None:
    article_source_count = sum(
        1 for row in manifest if row.get("kind") == "article_source" and row.get("asset_kind") == "article.md"
    )
    asset_count = sum(1 for row in manifest if row.get("kind") == "asset")
    lines = [
        "# Import Log",
        "",
        f"- Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"- Run folder: `{run_dir.name}`",
        f"- Manifest rows: {len(manifest)}",
        f"- Article sources: {article_source_count}",
        f"- Assets: {asset_count}",
    ]
    (project_dir / "wiki" / "log.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def export_llm_wiki_project(run_dir: Path, output_dir: Path | None = None, project_name: str = "foodkg") -> Path:
    run_dir = run_dir.resolve()
    knowledge_dir = run_dir / "_knowledge"
    if not knowledge_dir.exists():
        raise SystemExit(f"_knowledge does not exist: {knowledge_dir}")
    ks = load_knowledge_staging_module()
    project_dir = (output_dir or (run_dir / "llm_wiki_project_export" / project_name)).resolve()
    if project_dir.exists():
        shutil.rmtree(project_dir)
    for rel_dir in [
        "raw/sources",
        "raw/sources/articles",
        "raw/assets",
        "raw/provenance",
        "wiki",
    ]:
        (project_dir / rel_dir).mkdir(parents=True)

    articles = ks.load_articles(run_dir)
    grouped = ks.group_canonical_articles(articles)
    subquestions = sorted({normalize_ws(article.get("subquestion_id")) for article in articles if normalize_ws(article.get("subquestion_id"))})
    if not subquestions:
        subquestions = [path.name for path in subquestion_dirs(knowledge_dir)]

    manifest: list[dict[str, Any]] = []
    write_project_root_docs(project_dir, run_dir, subquestions)
    copy_project_dossier_sources(project_dir, run_dir, manifest)

    for key, occurrences in sorted(grouped.items(), key=lambda item: ks.choose_canonical_article(item[1]).get("title", "").lower()):
        exportable_occurrences = [
            occurrence for occurrence in occurrences if should_export_article_occurrence(occurrence)
        ]
        if not exportable_occurrences:
            continue
        canonical = ks.choose_canonical_article(exportable_occurrences)
        subq_slug = safe_slug(canonical.get("subquestion_id", ""), "unknown_subquestion", 72)
        article_slug = project_article_slug(ks, key, canonical)
        occurrence_bundles: list[tuple[dict[str, Any], Path]] = []
        assets: list[dict[str, str]] = []
        canonical_raw_bundle: Path | None = None
        for occurrence in sorted(exportable_occurrences, key=lambda item: (source_role_rank(item), posix_rel(item["article_dir"], run_dir))):
            occurrence_slug = safe_slug(posix_rel(occurrence["article_dir"], run_dir), occurrence["article_dir"].name, 96)
            bundle_slug = f"{article_slug}/{occurrence_slug}"
            raw_bundle, occurrence_assets = copy_article_bundle(project_dir, run_dir, occurrence, bundle_slug, subq_slug, manifest)
            occurrence_bundles.append((occurrence, raw_bundle))
            assets.extend(occurrence_assets)
            if occurrence["article_dir"] == canonical["article_dir"]:
                canonical_raw_bundle = raw_bundle
        if canonical_raw_bundle is None:
            canonical_raw_bundle = occurrence_bundles[0][1]

    write_project_wiki_pages(project_dir, run_dir, subquestions, manifest)
    write_project_log(project_dir, run_dir, manifest)
    fieldnames = ["kind", "subquestion_id", "title", "doi", "publisher", "source_path", "export_path", "asset_kind", "sha256"]
    write_csv(project_dir / "manifest.csv", manifest, fieldnames)
    (project_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return project_dir


def article_metadata(article_dir: Path) -> dict[str, str]:
    metadata = read_json(article_dir / "metadata.json", {})
    fulltext = read_json(article_dir / "fulltext.json", {})
    return {
        "title": normalize_ws(metadata.get("title") or fulltext.get("title") or article_dir.name),
        "doi": normalize_ws(metadata.get("doi") or fulltext.get("doi")),
        "publisher": normalize_ws(metadata.get("publisher") or metadata.get("source_bucket") or metadata.get("pdf_source")),
        "subquestion_id": normalize_ws(metadata.get("subquestion_id")),
    }


def parse_note_paths(index_path: Path, run_dir: Path) -> list[Path]:
    if not index_path.exists():
        return []
    paths: list[Path] = []
    for match in re.finditer(r"`([^`]+/reading-note-zh\.md)`", index_path.read_text(encoding="utf-8", errors="replace")):
        path = run_dir / match.group(1)
        if path.exists():
            paths.append(path)
    return sorted(dict.fromkeys(paths))


def subquestion_dirs(knowledge_dir: Path) -> list[Path]:
    root = knowledge_dir / "subquestions"
    if not root.exists():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir() and path.name != "references")


def write_static_docs(out_dir: Path, run_dir: Path) -> None:
    purpose = f"""# llm_wiki Raw Sources

This folder is generated from `{run_dir.name}` for ingestion into llm_wiki.

Use it as raw source material, not as a replacement for the canonical capture folders. Every exported Markdown file has YAML frontmatter with `source_run`, `source_path`, `source_kind`, and when available `subquestion_id`, `title`, `doi`, `publisher`, and `article_dir`.

Recommended llm_wiki use:

- build concept pages from final overview, subquestion summaries, important seeds, and query journeys;
- build paper/resource pages from paper cards and reading notes;
- preserve provenance links back to `source_path` before using generated claims;
- treat manual holds and gaps as evidence boundaries, not as missing instructions.
"""
    schema = """# Source Schema

## Core frontmatter

- `source_run`: literature-capture run folder name.
- `source_path`: original file path relative to the run folder.
- `source_kind`: final_overview, final_subquestion_summaries, subquestion_overview, papers, paper_card, reading_note, seed_ledger, reference_ledger, query_journey, coverage, figures_tables, attachment_manifest, or duplicate_report.
- `subquestion_id`: atomic subquestion identifier when available.
- `title`, `doi`, `publisher`, `article_dir`: article provenance when the source is a paper card or reading note.

## Ingestion advice

Prefer high-level synthesis pages first, then drill into subquestion pages, then reading notes and paper cards. Use `manifest.csv` or `manifest.json` to map generated files back to canonical captures.
"""
    (out_dir / "purpose.md").write_text(purpose.rstrip() + "\n", encoding="utf-8")
    (out_dir / "schema.md").write_text(schema.rstrip() + "\n", encoding="utf-8")
    (out_dir / "README.md").write_text(
        "# llm_wiki Export\n\n"
        "- [purpose](purpose.md)\n"
        "- [schema](schema.md)\n"
        "- [manifest.csv](manifest.csv)\n"
        "- [raw sources](raw_sources/)\n",
        encoding="utf-8",
    )


def export_llm_wiki(run_dir: Path, output_dir: Path | None = None) -> Path:
    run_dir = run_dir.resolve()
    knowledge_dir = run_dir / "_knowledge"
    if not knowledge_dir.exists():
        raise SystemExit(f"_knowledge does not exist: {knowledge_dir}")
    out_dir = (output_dir or (run_dir / "llm_wiki_raw_sources")).resolve()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    write_static_docs(out_dir, run_dir)

    manifest: list[dict[str, Any]] = []
    raw_root = out_dir / "raw_sources"

    for rel_source, kind, rel_target in ROOT_EXPORT_FILES:
        source = run_dir / rel_source
        if source.exists():
            manifest.append(copy_with_frontmatter(source, out_dir / rel_target, run_dir=run_dir, kind=kind))

    attachment_manifest = knowledge_dir / "llm_wiki" / "attachments_manifest.csv"
    if attachment_manifest.exists():
        target = raw_root / "attachments_manifest.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(attachment_manifest, target)
        manifest.append(
            {
                "source_run": run_dir.name,
                "source_path": rel(attachment_manifest, run_dir),
                "source_kind": "attachment_manifest",
                "subquestion_id": "",
                "title": "attachments_manifest",
                "doi": "",
                "publisher": "",
                "article_dir": "",
                "export_path": rel(target, out_dir),
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
        )

    for subq_dir in subquestion_dirs(knowledge_dir):
        subq = subq_dir.name
        target_base = raw_root / "subquestions" / safe_slug(subq, "subquestion")
        for filename in SUBQUESTION_FILES:
            source = subq_dir / filename
            if source.exists():
                manifest.append(
                    copy_with_frontmatter(
                        source,
                        target_base / filename,
                        run_dir=run_dir,
                        kind=filename.removesuffix(".md"),
                        subquestion_id=subq,
                    )
                )
        cards_dir = subq_dir / "paper_cards"
        if cards_dir.exists():
            for card in sorted(cards_dir.glob("*.md")):
                manifest.append(
                    copy_with_frontmatter(
                        card,
                        target_base / "paper_cards" / card.name,
                        run_dir=run_dir,
                        kind="paper_card",
                        subquestion_id=subq,
                    )
                )
        for note in parse_note_paths(subq_dir / "reading_notes_index.md", run_dir):
            article_dir = note.parent
            meta = article_metadata(article_dir)
            slug = safe_slug(meta["title"] or article_dir.name, article_dir.name, 90)
            target = target_base / "reading_notes" / f"{slug}--{safe_slug(article_dir.name, 'article', 32)}.md"
            manifest.append(
                copy_with_frontmatter(
                    note,
                    target,
                    run_dir=run_dir,
                    kind="reading_note",
                    subquestion_id=subq,
                    title=meta["title"],
                    doi=meta["doi"],
                    publisher=meta["publisher"],
                    article_dir=rel(article_dir, run_dir),
                )
            )

    fieldnames = [
        "source_run",
        "source_path",
        "source_kind",
        "subquestion_id",
        "title",
        "doi",
        "publisher",
        "article_dir",
        "export_path",
        "created_at",
    ]
    write_csv(out_dir / "manifest.csv", manifest, fieldnames)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_dir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--project-name", default="foodkg")
    parser.add_argument(
        "--raw-sources",
        action="store_true",
        help="Write the legacy llm_wiki_raw_sources export instead of a full llm_wiki project.",
    )
    args = parser.parse_args()
    if args.raw_sources:
        out_dir = export_llm_wiki(args.run_dir, args.output_dir)
        manifest = read_csv(out_dir / "manifest.csv")
        print(f"llm_wiki_raw_sources={out_dir}")
        print(f"exported_sources={len(manifest)}")
    else:
        out_dir = export_llm_wiki_project(args.run_dir, args.output_dir, args.project_name)
        manifest = read_csv(out_dir / "manifest.csv")
        asset_rows = [row for row in manifest if row.get("kind") == "asset"]
        source_pages = [row for row in manifest if row.get("kind") == "source_page"]
        print(f"llm_wiki_project={out_dir}")
        print(f"manifest_rows={len(manifest)}")
        print(f"source_pages={len(source_pages)}")
        print(f"assets_copied={len(asset_rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
