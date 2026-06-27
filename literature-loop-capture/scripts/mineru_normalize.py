#!/usr/bin/env python3
"""Normalize MinerU API article folders into capture-compatible files."""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
TABLE_EXTENSIONS = {".csv", ".tsv", ".html", ".htm", ".xlsx", ".xls"}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_mineru_log(article_dir: Path, stdout: str, stderr: str) -> Path:
    log = article_dir / "mineru-command.log"
    log.write_text(stdout + "\n--- STDERR ---\n" + stderr, encoding="utf-8")
    return log


def extract_sections(markdown: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    current = {"heading": "Document", "level": 1, "text": ""}
    for line in markdown.splitlines():
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            if current["text"].strip():
                sections.append(current)
            current = {
                "heading": match.group(2).strip(),
                "level": len(match.group(1)),
                "text": "",
            }
            continue
        current["text"] += line + "\n"
    if current["text"].strip() or not sections:
        sections.append(current)
    return sections


def empty_index(title: str) -> str:
    return "# " + title + "\n\nNo items detected in PDF/MinerU output.\n"


def relative_to_article(article_dir: Path, path: Path) -> str:
    try:
        return path.relative_to(article_dir).as_posix()
    except ValueError:
        return path.as_posix()


def normalize_markdown_asset_path(path_text: str) -> str:
    path = path_text.strip().strip("'\"")
    path = path.split("#", 1)[0].split("?", 1)[0]
    return path.replace("\\", "/")


def markdown_image_captions(markdown: str) -> dict[str, str]:
    captions: dict[str, str] = {}
    for match in re.finditer(r"!\[([^\]]*)\]\(([^)]+)\)", markdown):
        caption = match.group(1).strip()
        path_text = normalize_markdown_asset_path(match.group(2))
        if not path_text:
            continue
        path = Path(path_text)
        for key in {
            path_text,
            path.name,
            path.stem,
            Path(path_text).as_posix(),
        }:
            if key and caption:
                captions[key] = caption
    return captions


def guess_content_type(path: Path) -> str:
    extension = path.suffix.lower().lstrip(".")
    if extension == "jpg":
        extension = "jpeg"
    if extension in {"png", "jpeg", "webp", "bmp", "tif", "tiff"}:
        return f"image/{extension}"
    if extension == "csv":
        return "text/csv"
    if extension == "tsv":
        return "text/tab-separated-values"
    if extension in {"html", "htm"}:
        return "text/html"
    return "application/octet-stream"


def discover_files(root: Path, extensions: set[str]) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in extensions
    )


def choose_mineru_markdown(mineru_dir: Path) -> Path | None:
    """Find the best markdown file emitted by the MinerU API."""
    if not mineru_dir.exists():
        return None
    canonical = mineru_dir / "fulltext.md"
    if canonical.exists():
        return canonical

    candidates = [
        path
        for path in mineru_dir.rglob("*.md")
        if path.is_file() and path.name != "fulltext.md"
    ]
    if not candidates:
        return None

    for preferred_name in ("full.md", "source.md"):
        preferred = [path for path in candidates if path.name == preferred_name]
        if preferred:
            return max(preferred, key=lambda path: path.stat().st_size)
    return max(candidates, key=lambda path: path.stat().st_size)


def ensure_mineru_fulltext(article_dir: Path) -> tuple[Path | None, str]:
    """Promote nested MinerU markdown to mineru/fulltext.md when needed."""
    mineru_dir = article_dir / "mineru"
    target = mineru_dir / "fulltext.md"
    source = choose_mineru_markdown(mineru_dir)
    if source is None:
        return None, ""
    if source.resolve() != target.resolve():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return target, relative_to_article(article_dir, source)


def copy_mineru_figures(article_dir: Path, markdown: str) -> list[dict[str, Any]]:
    figures_dir = article_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    captions = markdown_image_captions(markdown)
    manifest: list[dict[str, Any]] = []
    for index, source in enumerate(discover_files(article_dir / "mineru", IMAGE_EXTENSIONS)):
        extension = source.suffix.lower()
        if extension == ".jpeg":
            extension = ".jpg"
        local_file = f"figure-{index:02d}{extension}"
        target = figures_dir / local_file
        shutil.copy2(source, target)
        relative_source = relative_to_article(article_dir, source)
        source_path = source.as_posix()
        caption = (
            captions.get(relative_source)
            or captions.get(source.name)
            or captions.get(source.stem)
            or source.stem
        )
        manifest.append(
            {
                "caption": caption,
                "image": source_path,
                "index": index,
                "label": "",
                "local_file": local_file,
                "download_status": "copied_from_mineru",
                "download_content_type": guess_content_type(source),
                "download_bytes": target.stat().st_size,
            }
        )
    write_json(figures_dir / "manifest.json", manifest)
    write_figure_index(figures_dir / "index.md", manifest)
    return manifest


def count_delimited_rows(path: Path) -> int:
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as handle:
            rows = list(csv.reader(handle, delimiter=delimiter))
    except OSError:
        return 0
    if not rows:
        return 0
    return max(len(rows) - 1, 0)


def copy_mineru_tables(article_dir: Path) -> list[dict[str, Any]]:
    tables_dir = article_dir / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    for index, source in enumerate(discover_files(article_dir / "mineru", TABLE_EXTENSIONS)):
        extension = source.suffix.lower()
        local_file = f"table-{index:02d}{extension}"
        target = tables_dir / local_file
        shutil.copy2(source, target)
        row_count = (
            count_delimited_rows(source)
            if extension in {".csv", ".tsv"}
            else 0
        )
        manifest.append(
            {
                "caption": source.stem,
                "href": relative_to_article(article_dir, source),
                "html": source.read_text(encoding="utf-8", errors="replace")
                if extension in {".html", ".htm"}
                else "",
                "index": index,
                "label": f"Table {index + 1}",
                "local_file": local_file,
                "rows": [],
                "row_count": row_count,
                "download_status": "copied_from_mineru",
                "download_content_type": guess_content_type(source),
                "download_bytes": target.stat().st_size,
            }
        )
    write_json(tables_dir / "manifest.json", manifest)
    write_table_index(tables_dir / "index.md", manifest)
    return manifest


def write_figure_index(path: Path, manifest: list[dict[str, Any]]) -> None:
    if not manifest:
        path.write_text(empty_index("Figures"), encoding="utf-8")
        return
    lines = ["# Figures", ""]
    for item in manifest:
        label = item.get("label") or f"Figure {item.get('index', 0) + 1}"
        local_file = item.get("local_file") or ""
        caption = item.get("caption") or ""
        lines.append(f"- **{label}** `{local_file}`")
        if caption:
            lines.append(f"  - {caption}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_table_index(path: Path, manifest: list[dict[str, Any]]) -> None:
    if not manifest:
        path.write_text(empty_index("Tables"), encoding="utf-8")
        return
    lines = ["# Tables", ""]
    for item in manifest:
        label = item.get("label") or f"Table {item.get('index', 0) + 1}"
        local_file = item.get("local_file") or ""
        row_count = item.get("row_count")
        row_text = f", rows={row_count}" if row_count is not None else ""
        caption = item.get("caption") or ""
        lines.append(f"- **{label}** `{local_file}`{row_text}")
        if caption:
            lines.append(f"  - {caption}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def capture_depth_value(metadata: dict[str, Any]) -> int:
    try:
        return int(metadata.get("capture_depth") or 2)
    except (TypeError, ValueError):
        return 2


def compatibility_note() -> str:
    return (
        "# Auto Extract Note\n\n"
        "PDF text was normalized from MinerU output. "
        "Agent reading note is still required.\n"
    )


def agent_note_required() -> str:
    return (
        "# NOTE_REQUIRES_AGENT\n\n"
        "Write `reading-note-zh.md` after reviewing the PDF-derived full text, "
        "figures, tables, and metadata.\n"
    )


def normalize_article_dir(article_dir: Path) -> dict[str, Any]:
    """Write standard literature-capture files from an existing MinerU folder."""
    source_pdf = article_dir / "source.pdf"
    if not source_pdf.exists():
        return {"status": "missing_source_pdf", "article_dir": str(article_dir)}
    mineru_md, mineru_source_markdown = ensure_mineru_fulltext(article_dir)
    if mineru_md is None:
        return {"status": "missing_mineru_fulltext", "article_dir": str(article_dir)}

    extracted_at = now_iso()
    metadata_path = article_dir / "metadata.json"
    metadata = read_json(metadata_path)
    markdown = mineru_md.read_text(encoding="utf-8", errors="replace")
    sections = extract_sections(markdown)
    section_headers = [
        {"heading": section["heading"], "level": section["level"]}
        for section in sections
    ]

    (article_dir / "fulltext.md").write_text(markdown, encoding="utf-8")
    (article_dir / "captured-fulltext.md").write_text(markdown, encoding="utf-8")
    figures_manifest = copy_mineru_figures(article_dir, markdown)
    tables_manifest = copy_mineru_tables(article_dir)
    write_json(
        article_dir / "fulltext.json",
        {
            "title": metadata.get("title") or article_dir.name,
            "article_type": "pdf-mineru",
            "sections": sections,
            "text_length": len(markdown),
            "source_pdf": "source.pdf",
            "extracted_at": extracted_at,
        },
    )
    write_json(
        article_dir / "structure.json",
        {
            "article_type": "pdf-mineru",
            "sections": section_headers,
            "source_pdf": "source.pdf",
            "extracted_at": extracted_at,
        },
    )

    metadata.update(
        {
            "article_type": "pdf-mineru",
            "source_role": metadata.get("source_role") or "reference",
            "capture_depth": capture_depth_value(metadata),
            "fulltext_chars": len(markdown),
            "section_count": len(sections),
            "figure_count": len(figures_manifest),
            "table_count": len(tables_manifest),
            "mineru_source_markdown": mineru_source_markdown,
            "note_status": "pending_agent",
        }
    )
    write_json(metadata_path, metadata)

    (article_dir / "auto-extract-note.md").write_text(
        compatibility_note(),
        encoding="utf-8",
    )
    (article_dir / "NOTE_REQUIRES_AGENT.md").write_text(
        agent_note_required(),
        encoding="utf-8",
    )
    status = read_json(article_dir / "pdf-capture-status.json")
    status.update(
        {
            "status": "normalized",
            "updated_at": extracted_at,
            "source_pdf": "source.pdf",
            "mineru_fulltext": "mineru/fulltext.md",
            "mineru_source_markdown": mineru_source_markdown,
        }
    )
    write_json(article_dir / "pdf-capture-status.json", status)
    return {
        "status": "normalized",
        "article_dir": str(article_dir),
        "chars": len(markdown),
        "sections": len(sections),
        "figures": len(figures_manifest),
        "tables": len(tables_manifest),
    }


def run_mineru(article_dir: Path, command: str) -> dict[str, Any]:
    """Local MinerU execution is disabled in this skill."""
    return {
        "status": "blocked_local_mineru_disabled_use_mineru_api_extract",
        "article_dir": str(article_dir),
        "message": "Use mineru_api_extract.py with MINERU_API_KEY; local model commands are not part of the standard workflow.",
    }


def scan_pdf_article_dirs(run_root: Path) -> list[Path]:
    """Find PDF reference article folders that contain source.pdf."""
    return sorted(
        path.parent
        for path in run_root.glob(
            "subquestions/*/*/references/pdf/*/articles/*/source.pdf"
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--article-dir", type=Path)
    parser.add_argument(
        "--mineru-command",
        default="",
        help="Disabled. Use mineru_api_extract.py with MINERU_API_KEY instead.",
    )
    args = parser.parse_args()

    if args.article_dir:
        targets = [args.article_dir.resolve()]
    else:
        targets = scan_pdf_article_dirs(args.run_dir.resolve())

    results = []
    for article_dir in targets:
        if args.mineru_command:
            mineru_result = run_mineru(article_dir, args.mineru_command)
            results.append(mineru_result)
            continue
        results.append(normalize_article_dir(article_dir))
    print(json.dumps(results, ensure_ascii=False, indent=2))
    ok_statuses = {"normalized"}
    return 0 if all(row["status"] in ok_statuses for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
