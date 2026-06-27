#!/usr/bin/env python3
"""Create a short-name organized export for a literature capture run."""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def safe_slug(text: str, fallback: str = "untitled", limit: int = 72) -> str:
    text = normalize_ws(text)
    text = re.sub(r"[\\/:*?\"<>|]+", " ", text)
    text = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff.-]+", "-", text, flags=re.UNICODE)
    text = text.strip(".-")
    return (text[:limit].strip(".-") or fallback)


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


def article_name(row: dict[str, Any], article_dir: Path) -> str:
    metadata_path = article_dir / "metadata.json"
    metadata: dict[str, Any] = {}
    if metadata_path.exists():
        try:
            data = read_json(metadata_path)
            if isinstance(data, dict):
                metadata = data
        except Exception:
            metadata = {}
    title = normalize_ws(str(metadata.get("title") or row.get("title") or article_dir.name))
    year = str(metadata.get("year") or row.get("year") or "n.d.")
    role = str(metadata.get("source_role") or row.get("source_role") or "primary")
    prefix = "ref" if role == "reference" else "primary"
    return safe_slug(f"{year}-{prefix}-{title}", fallback=article_dir.name, limit=96)


def unique_path(root: Path, name: str) -> Path:
    candidate = root / name
    if not candidate.exists():
        return candidate
    for index in range(2, 1000):
        candidate = root / f"{name}-{index:02d}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Too many duplicate organized names for {name}")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def organize(run_dir: Path, output_dir: Path) -> list[dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    articles_root = output_dir / "articles"
    articles_root.mkdir(parents=True, exist_ok=True)
    rows = read_summary(run_dir)
    manifest: list[dict[str, Any]] = []
    organized_summary: list[dict[str, Any]] = []
    for row in rows:
        article_dir = Path(str(row.get("article_dir") or ""))
        new_row = dict(row)
        if row.get("status") == "captured" and article_dir.exists():
            role = str(row.get("source_role") or "primary")
            role_root = articles_root / ("references" if role == "reference" else "primary")
            role_root.mkdir(parents=True, exist_ok=True)
            final_name = article_name(row, article_dir)
            target = unique_path(role_root, final_name)
            shutil.copytree(article_dir, target)
            new_row["organized_article_dir"] = str(target)
            manifest.append({
                "old_path": str(article_dir),
                "new_path": str(target),
                "old_name": article_dir.name,
                "new_name": target.name,
                "title": row.get("title") or "",
                "doi": row.get("doi") or "",
                "year": row.get("year") or "",
                "source_role": role,
            })
        organized_summary.append(new_row)
    (output_dir / "organize-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(output_dir / "organize-manifest.csv", manifest)
    (output_dir / "run-summary.organized.json").write_text(json.dumps(organized_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(output_dir / "run-summary.organized.csv", organized_summary)
    (output_dir / "README.md").write_text(
        "\n".join([
            "# Organized Literature Capture Export",
            "",
            f"- Source run: `{run_dir}`",
            f"- Generated: {datetime.now().isoformat(timespec='seconds')}",
            f"- Articles copied: {len(manifest)}",
            "",
            "This export uses short title-based names and keeps DOI values in metadata/manifests, not file names.",
        ]) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    output_dir = (args.output_dir or (run_dir / "organized")).resolve()
    manifest = organize(run_dir, output_dir)
    print(f"organized_dir={output_dir}")
    print(f"articles={len(manifest)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
