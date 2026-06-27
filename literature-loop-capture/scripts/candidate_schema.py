"""Shared literature candidate schema and identifier-based dedupe helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import re
from typing import Any


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_doi(value: Any) -> str:
    text = clean(value).lower()
    text = re.sub(r"^https?://(dx\.)?doi\.org/", "", text)
    text = re.sub(r"^doi:\s*", "", text)
    return text.rstrip(" .")


def normalize_title(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean(value).lower()).strip()


@dataclass
class LiteratureCandidate:
    candidate_id: str = ""
    title: str = ""
    authors: list[str] | None = None
    year: int | None = None
    journal: str = ""
    abstract: str = ""
    abstract_source: str = ""
    doi: str = ""
    pmid: str = ""
    pmcid: str = ""
    arxiv_id: str = ""
    pii: str = ""
    source_key: str = ""
    publisher_key: str = ""
    landing_url: str = ""
    public_pdf_candidate: str = ""
    query_text: str = ""
    subquestion_id: str = ""
    discovery_iteration: int = 1
    discovery_backend: str = "opencli"
    source_rank: int | None = None
    access_hint: str = ""
    dedupe_key: str = ""

    def finalize(self) -> "LiteratureCandidate":
        self.doi = normalize_doi(self.doi)
        self.title = clean(self.title)
        self.journal = clean(self.journal)
        self.abstract = clean(self.abstract)
        self.landing_url = clean(self.landing_url)
        self.public_pdf_candidate = clean(self.public_pdf_candidate)
        self.dedupe_key = dedupe_key(asdict(self))
        if not self.candidate_id:
            digest = hashlib.sha1(self.dedupe_key.encode("utf-8", errors="ignore")).hexdigest()[:12]
            self.candidate_id = f"cand_{digest}"
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.finalize())


def dedupe_key(row: dict[str, Any]) -> str:
    stage = clean(row.get("stage")).lower()
    if stage and stage != "candidate":
        subquestion_id = clean(row.get("subquestion_id"))
        query = clean(row.get("query_text"))
        publisher = clean(row.get("publisher") or row.get("publisher_key") or row.get("source_key"))
        page = clean(row.get("page")) or "1"
        iteration = clean(row.get("discovery_iteration")) or "1"
        status = clean(row.get("status"))
        return (
            f"{stage}:subq:{subquestion_id}|query:{query}|publisher:{publisher}|"
            f"page:{page}|iteration:{iteration}|status:{status}"
        )
    doi = normalize_doi(row.get("doi"))
    if doi:
        return f"doi:{doi}"
    for key in ["pmid", "pmcid", "arxiv_id", "pii"]:
        value = clean(row.get(key)).lower()
        if value:
            return f"{key}:{value}"
    title = normalize_title(row.get("title"))
    year = clean(row.get("year"))
    authors = row.get("authors") or []
    first_author = ""
    if isinstance(authors, list) and authors:
        first_author = normalize_title(authors[0])
    elif isinstance(authors, str):
        first_author = normalize_title(authors.split(";")[0])
    return f"title:{title}|year:{year}|author:{first_author}"


def merge_deduped(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        key = dedupe_key(row)
        if key not in merged:
            merged[key] = dict(row)
            merged[key]["dedupe_key"] = key
            order.append(key)
            continue
        current = merged[key]
        for field, value in row.items():
            if value and not current.get(field):
                current[field] = value
        current["dedupe_key"] = key
    return [merged[key] for key in order]
