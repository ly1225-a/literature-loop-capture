#!/usr/bin/env python3
"""Export captured Markdown literature notes into an Obsidian-style vault."""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def safe_slug(text: str, fallback: str = "paper", limit: int = 80) -> str:
    text = normalize_ws(text)
    text = re.sub(r"[\\/:*?\"<>|]+", " ", text)
    text = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff.-]+", "-", text, flags=re.UNICODE)
    text = text.strip(".-").lower()
    return (text[:limit].strip(".-") or fallback)


def yaml_scalar(value: Any) -> str:
    text = str(value or "").replace('"', '\\"')
    return f'"{text}"'


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return read_json(path)
    except Exception:
        return fallback


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


def best_source_md(article_dir: Path) -> Path | None:
    for rel in ["captured-fulltext.md", "fulltext.md", "mineru/fulltext.md", "auto-extract-note.md"]:
        path = article_dir / rel
        if path.exists():
            return path
    return None


def field_from_text(text: str, field: str) -> str:
    pattern = re.compile(rf"^\s*-?\s*{re.escape(field)}\s*[:：]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
    match = pattern.search(text or "")
    return normalize_ws(match.group(1)) if match else ""


def truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "是"}


def score_value(value: Any) -> float | None:
    try:
        return float(str(value or "").strip())
    except Exception:
        return None


def close_read_metadata(row: dict[str, Any], article_dir: Path) -> dict[str, Any]:
    note_text = ""
    note = article_dir / "reading-note-zh.md"
    if note.exists():
        note_text = note.read_text(encoding="utf-8", errors="replace")
    worth_raw = row.get("worth_close_reading")
    if worth_raw in {None, ""}:
        worth_raw = field_from_text(note_text, "worth_close_reading")
    score_raw = row.get("worth_close_reading_score_0_to_5")
    if score_raw in {None, ""}:
        score_raw = field_from_text(note_text, "worth_close_reading_score_0_to_5")
    score = score_value(score_raw)
    return {
        "worth_close_reading": truthy(worth_raw),
        "worth_close_reading_score_0_to_5": score,
        "note_text": note_text,
    }


def read_coverage_index(run_dir: Path) -> dict[str, dict[str, Any]]:
    data = load_json(run_dir / "coverage-review" / "subquestion-coverage-review.json", {})
    items = data.get("subquestions") if isinstance(data, dict) else []
    if not isinstance(items, list):
        return {}
    return {
        normalize_ws(str(item.get("subquestion_id") or "")): item
        for item in items if isinstance(item, dict) and normalize_ws(str(item.get("subquestion_id") or ""))
    }


def coverage_allows_final(row: dict[str, Any], coverage_index: dict[str, dict[str, Any]]) -> bool:
    subquestion_id = normalize_ws(str(row.get("subquestion_id") or ""))
    coverage = coverage_index.get(subquestion_id) or {}
    decision = normalize_ws(str(coverage.get("coverage_decision") or ""))
    blockers = []
    for key in ["blockers", "capture_blockers", "unresolved_blockers"]:
        value = coverage.get(key)
        if isinstance(value, list):
            blockers.extend(value)
        elif normalize_ws(str(value or "")):
            blockers.append(value)
    return decision in {"sufficient", "stop_with_gaps"} and not blockers


def should_export(
    row: dict[str, Any],
    article_dir: Path,
    include_all: bool,
    coverage_index: dict[str, dict[str, Any]] | None = None,
    export_mode: str = "staging",
) -> bool:
    if export_mode == "staging" and include_all:
        return True
    metadata = close_read_metadata(row, article_dir)
    close_read = metadata["worth_close_reading"] and (metadata["worth_close_reading_score_0_to_5"] or 0) >= 4
    if export_mode == "staging":
        return close_read
    if include_all:
        return False
    if not close_read:
        return False
    return coverage_allows_final(row, coverage_index or {})


def paper_identity(row: dict[str, Any], title: str, year: str, fallback: str) -> str:
    doi = normalize_ws(str(row.get("doi") or "")).lower()
    if doi:
        return "doi-" + safe_slug(doi.replace("/", "-"), fallback, 96)
    return safe_slug(f"{year}-{title}", fallback, 96)


def extract_terms(note_text: str, row: dict[str, Any]) -> dict[str, list[str]]:
    buckets = {"concepts": [], "methods": [], "datasets": [], "seeds": [], "references": []}
    for key, patterns in {
        "concepts": ["concept", "概念", "关键词"],
        "methods": ["method", "方法", "model", "pipeline"],
        "datasets": ["dataset", "database", "benchmark", "数据集", "基准"],
        "seeds": ["high-value seed", "高价值 seed", "高价值seed"],
        "references": ["reference", "引用选择", "recommended reference"],
    }.items():
        for line in note_text.splitlines():
            cleaned = normalize_ws(line.strip("-* "))
            if cleaned and any(pattern.lower() in cleaned.lower() for pattern in patterns):
                buckets[key].append(cleaned[:120])
    if row.get("keywords"):
        buckets["concepts"].extend(part.strip() for part in re.split(r"[;,]", str(row.get("keywords"))) if part.strip())
    return {key: list(dict.fromkeys(values))[:12] for key, values in buckets.items()}


def write_page(path: Path, title: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {title}\n\n{body.strip()}\n", encoding="utf-8")
def copytree_replace(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def wikilink(path: Path) -> str:
    return "[[" + str(path).replace("\\", "/") + "]]"


def make_research_card(
    row: dict[str, Any],
    note_text: str,
    vault_relative_source: Path,
    vault_relative_capture: Path,
    discipline: str,
    status: str,
) -> str:
    title = normalize_ws(str(row.get("title") or "Untitled"))
    slug_tags = [safe_slug(discipline, "discipline", 40), "literature-capture"]
    subquestion = normalize_ws(str(row.get("subquestion_text") or ""))
    today = date.today().isoformat()
    frontmatter = [
        "---",
        f"title: {yaml_scalar(title)}",
        "type: research",
        "tags:",
        *[f"  - {tag}" for tag in slug_tags],
        f"created: {today}",
        f"updated: {today}",
        "sources: 1",
        f"source_file: {yaml_scalar(wikilink(vault_relative_source))}",
        f"capture_dir: {yaml_scalar(wikilink(vault_relative_capture))}",
        f"doi: {yaml_scalar(row.get('doi') or '')}",
        f"year: {yaml_scalar(row.get('year') or '')}",
        f"publisher: {yaml_scalar(row.get('publisher') or '')}",
        f"status: {yaml_scalar(status)}",
        "subquestions:",
    ]
    if subquestion:
        frontmatter.append(f"  - {yaml_scalar(subquestion)}")
    frontmatter.append("---")
    body = note_text.strip() if note_text.strip() else "原始抓取已导入，尚未生成详细阅读笔记。"
    return "\n".join(frontmatter) + "\n\n" + "\n".join([
        "## 一句话贡献",
        "",
        "待从阅读笔记中压缩。",
        "",
        "## 读书卡片",
        "",
        body,
        "",
        "## Links",
        "",
        f"- Source: {wikilink(vault_relative_source)}",
        f"- Capture: {wikilink(vault_relative_capture)}",
    ]) + "\n"


def ensure_vault(vault_dir: Path) -> None:
    (vault_dir / "raw" / "inbox").mkdir(parents=True, exist_ok=True)
    (vault_dir / "raw" / "assets").mkdir(parents=True, exist_ok=True)
    (vault_dir / "wiki").mkdir(parents=True, exist_ok=True)
    index = vault_dir / "wiki" / "index.md"
    if not index.exists():
        index.write_text("# Index\n\n", encoding="utf-8")
    overview = vault_dir / "wiki" / "overview.md"
    if not overview.exists():
        overview.write_text("# Overview\n\n", encoding="utf-8")
    log = vault_dir / "wiki" / "log.md"
    if not log.exists():
        log.write_text("# Log\n\n", encoding="utf-8")


def export(run_dir: Path, vault_dir: Path, discipline: str, include_all: bool, export_mode: str = "staging") -> list[dict[str, Any]]:
    if export_mode not in {"staging", "vault"}:
        raise SystemExit("--export-mode must be staging or vault")
    if include_all and export_mode == "vault":
        raise SystemExit("--include-all is allowed only with --export-mode staging")
    target_vault = vault_dir / "staging" if export_mode == "staging" else vault_dir
    ensure_vault(target_vault)
    raw_root = target_vault / "raw" / discipline
    source_root = raw_root / "source-md"
    capture_root = raw_root / "captures"
    source_root.mkdir(parents=True, exist_ok=True)
    capture_root.mkdir(parents=True, exist_ok=True)
    wiki_root = target_vault / "wiki"
    rows = read_summary(run_dir)
    coverage_index = read_coverage_index(run_dir)
    exported: list[dict[str, Any]] = []
    index_lines: list[str] = []
    for row in rows:
        if row.get("status") != "captured":
            continue
        article_dir = Path(str(row.get("article_dir") or ""))
        if not article_dir.exists() or not should_export(row, article_dir, include_all, coverage_index, export_mode):
            continue
        source_md = best_source_md(article_dir)
        if source_md is None:
            continue
        title = normalize_ws(str(row.get("title") or article_dir.name))
        year = str(row.get("year") or "n.d.")
        base_slug = paper_identity(row, title, year, article_dir.name)
        source_target = source_root / f"{base_slug}.md"
        shutil.copy2(source_md, source_target)
        capture_target = capture_root / base_slug
        copytree_replace(article_dir, capture_target)
        note_path = article_dir / "reading-note-zh.md"
        note_text = note_path.read_text(encoding="utf-8", errors="replace") if note_path.exists() else ""
        research_name = f"{base_slug}.md"
        research_path = wiki_root / "papers" / research_name
        research_path.parent.mkdir(parents=True, exist_ok=True)
        rel_source = source_target.relative_to(target_vault)
        rel_capture = capture_target.relative_to(target_vault)
        status = "staging" if export_mode == "staging" else "close_read"
        research_path.write_text(make_research_card(row, note_text, rel_source, rel_capture, discipline, status), encoding="utf-8")
        subquestion_id = normalize_ws(str(row.get("subquestion_id") or "unassigned"))
        subquestion_page = wiki_root / "subquestions" / f"{safe_slug(subquestion_id, 'subquestion')}.md"
        write_page(subquestion_page, subquestion_id, f"- Paper: {wikilink(research_path.relative_to(target_vault).with_suffix(''))}\n- Question: {normalize_ws(str(row.get('subquestion_text') or ''))}")
        for bucket, terms in extract_terms(note_text, row).items():
            for term in terms:
                term_path = wiki_root / bucket / f"{safe_slug(term, bucket, 80)}.md"
                write_page(term_path, term, f"- Paper: {wikilink(research_path.relative_to(target_vault).with_suffix(''))}\n- Subquestion: {wikilink(subquestion_page.relative_to(target_vault).with_suffix(''))}")
        exported.append({
            "title": title,
            "doi": row.get("doi") or "",
            "year": year,
            "source_file": str(rel_source).replace("\\", "/"),
            "capture_dir": str(rel_capture).replace("\\", "/"),
            "research_page": str(research_path.relative_to(target_vault)).replace("\\", "/"),
            "export_mode": export_mode,
            "not_for_final_vault": export_mode == "staging",
        })
        index_lines.append(f"- [[wiki/papers/{research_name[:-3]}|{title}]] ({year})")
    if index_lines:
        with (wiki_root / "index.md").open("a", encoding="utf-8") as handle:
            handle.write("\n## Literature Capture Export " + datetime.now().isoformat(timespec="seconds") + "\n\n")
            handle.write("\n".join(index_lines) + "\n")
        with (wiki_root / "log.md").open("a", encoding="utf-8") as handle:
            handle.write(f"- {datetime.now().isoformat(timespec='seconds')}: exported {len(index_lines)} literature cards from `{run_dir}`.\n")
        with (wiki_root / "overview.md").open("a", encoding="utf-8") as handle:
            handle.write("\n## Latest Literature Capture Export\n\n")
            handle.write("\n".join(index_lines[:20]) + "\n")
    (target_vault / "obsidian-export-manifest.json").write_text(json.dumps(exported, ensure_ascii=False, indent=2), encoding="utf-8")
    return exported


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--vault-dir", type=Path, required=True)
    parser.add_argument("--discipline", default="Literature")
    parser.add_argument("--include-all", action="store_true", help="Export all captured articles, not only close-read/references.")
    parser.add_argument("--export-mode", choices=["staging", "vault"], default="staging")
    args = parser.parse_args()
    exported = export(args.run_dir.resolve(), args.vault_dir.resolve(), args.discipline, args.include_all, args.export_mode)
    print(f"vault_dir={(args.vault_dir.resolve() / 'staging') if args.export_mode == 'staging' else args.vault_dir.resolve()}")
    print(f"exported={len(exported)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
