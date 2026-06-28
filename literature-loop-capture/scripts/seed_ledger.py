#!/usr/bin/env python3
"""Extract seed-ledger rows from captured article reading artifacts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "increase",
    "into",
    "is",
    "of",
    "on",
    "or",
    "paper",
    "study",
    "the",
    "to",
    "under",
    "updated",
    "using",
    "with",
}

NOTE_SEED_PATTERNS = [
    "high-value seed",
    "高价值 seed",
    "proposed next query",
    "gap list",
]


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def short_query(text: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", clean(text))
    kept: list[str] = []
    for word in words:
        normalized = word.lower()
        if normalized in STOP_WORDS:
            continue
        kept.append(normalized)
        if len(kept) == 4:
            break
    return " ".join(kept)


def classify_seed(text: str) -> str:
    lowered = clean(text).lower()
    if any(keyword in lowered for keyword in ["ontology", "ontolog", "taxonomy", "controlled vocabulary"]):
        return "ontology"
    if any(keyword in lowered for keyword in ["database", "dataset", "resource", "db", "registry", "repository"]):
        return "resource"
    if any(keyword in lowered for keyword in ["benchmark", "baseline", "evaluation", "gold standard"]):
        return "benchmark"
    if any(keyword in lowered for keyword in ["method", "algorithm", "alignment", "model", "framework"]):
        return "method"
    return "paper_title"


def note_seed_lines(note_text: str) -> list[str]:
    seeds: list[str] = []
    for raw_line in note_text.splitlines():
        line = clean(raw_line)
        if not line:
            continue
        lowered = line.lower()
        if not any(pattern in lowered for pattern in NOTE_SEED_PATTERNS):
            continue
        parts = re.split(r"[:：]", line, maxsplit=1)
        seeds.append(clean(parts[1] if len(parts) > 1 else line))
    return [seed for seed in seeds if seed]


def _metadata(article_dir: Path) -> dict[str, Any]:
    data = load_json(article_dir / "metadata.json", {})
    return data if isinstance(data, dict) else {}


def _base_row(article_dir: Path, source_type: str, seed_text: str) -> dict[str, Any]:
    metadata = _metadata(article_dir)
    return {
        "subquestion_id": "",
        "source_article_dir": str(article_dir),
        "source_article_title": clean(metadata.get("title")),
        "source_type": source_type,
        "seed_text": seed_text,
        "seed_kind": classify_seed(seed_text),
        "why_it_matters": "",
        "duplicate_risk": "",
        "proposed_short_query": short_query(seed_text),
        "recommended_action": "needs_agent_review",
    }


def _reference_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ["references", "recommended_references", "items"]:
            items = data.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
    return []


def _reference_seed_text(item: dict[str, Any]) -> str:
    for key in ["reference_text", "reference", "citation", "title"]:
        value = clean(item.get(key))
        if value:
            return value
    return ""


def reference_rows(article_dir: Path) -> list[dict[str, Any]]:
    data = load_json(article_dir / "recommended-references.json", [])
    rows: list[dict[str, Any]] = []
    for item in _reference_items(data):
        seed_text = _reference_seed_text(item)
        if not seed_text:
            continue
        row = _base_row(article_dir, "recommended_reference", seed_text)
        row.update({
            "doi": clean(item.get("doi")),
            "why_it_matters": clean(item.get("reason") or item.get("rationale")),
            "recommended_action": "reference_followup_candidate",
        })
        rows.append(row)
    return rows


def _reading_note_rows(article_dir: Path) -> list[dict[str, Any]]:
    note_text = read_text(article_dir / "reading-note-zh.md")
    rows: list[dict[str, Any]] = []
    for seed_text in note_seed_lines(note_text):
        row = _base_row(article_dir, "reading_note", seed_text)
        row["recommended_action"] = "query_iteration_candidate"
        rows.append(row)
    return rows


def build_seed_ledger_rows(article_dirs: list[Path], subquestion_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for article_dir in article_dirs:
        for row in [*reference_rows(article_dir), *_reading_note_rows(article_dir)]:
            seed_text = clean(row.get("seed_text"))
            source_type = clean(row.get("source_type"))
            source_article_dir = clean(row.get("source_article_dir"))
            if not seed_text:
                continue
            key = (seed_text.lower(), source_type.lower(), source_article_dir)
            if key in seen:
                continue
            seen.add(key)
            row["subquestion_id"] = subquestion_id
            rows.append(row)
    return rows
