#!/usr/bin/env python3
"""Build supplemental follow-up ledgers and capture queues."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse


FIELDS = [
    "subquestion_group_slug",
    "subquestion_group_title",
    "subquestion_id",
    "subquestion_slug",
    "subquestion_text",
    "lead_source",
    "title",
    "doi",
    "identifier",
    "year",
    "venue",
    "publisher",
    "landing_url",
    "pdf_url",
    "route",
    "structured_source",
    "pdf_source",
    "action",
    "manual_reason",
    "source_article_title",
    "source_article_dir",
    "source_reference_index",
    "citation_contexts",
    "source_iteration",
    "source_exact_query",
    "openalex_id",
    "openalex_match_status",
    "agent_openalex_verified",
    "openalex_verification_status",
    "target_article_dir",
    "merged_sources_json",
]

STRUCTURED_HOSTS = {
    "sciencedirect.com": "elsevier",
    "www.sciencedirect.com": "elsevier",
    "pubs.acs.org": "acs",
    "onlinelibrary.wiley.com": "wiley",
    "link.springer.com": "springer",
}

SCIENCE_PUBLISHER_MARKERS = {
    "aaas",
    "american association for the advancement of science",
}
SCIENCE_FAMILY_VENUES = {
    "science",
    "science advances",
    "science immunology",
    "science robotics",
    "science signaling",
    "science translational medicine",
}
NATURE_PUBLISHER_MARKERS = {
    "nature portfolio",
    "nature publishing group",
    "nature research",
}
NATURE_FAMILY_VENUES = {
    "scientific reports",
}


def normalize_ws(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def clean_doi(value: Any) -> str:
    text = normalize_ws(value)
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.I)
    return text.rstrip(".,;:)").lower()


def arxiv_id_from_text(text: Any) -> str:
    value = normalize_ws(text)
    match = re.search(
        r"(?:arxiv:|arxiv\.org/(?:abs|pdf)/)(\d{4}\.\d{4,5})(?:v\d+)?",
        value,
        re.I,
    )
    return match.group(1) if match else ""


def normalized_title_key(title: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalize_ws(title).lower())[:140]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] = FIELDS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def subquestion_text_map(run_root: Path) -> dict[str, dict[str, str]]:
    mapping: dict[str, dict[str, str]] = {}
    for path in (run_root / "subquestions").glob("*/*/subquestion.json"):
        try:
            data = read_json(path)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        subquestion_id = normalize_ws(data.get("subquestion_id") or path.parent.name)
        mapping[subquestion_id] = {
            "subquestion_group_slug": normalize_ws(
                data.get("subquestion_group_slug") or path.parent.parent.name
            ),
            "subquestion_group_title": normalize_ws(
                data.get("subquestion_group_title") or data.get("group_title") or "General"
            ),
            "subquestion_id": subquestion_id,
            "subquestion_slug": normalize_ws(data.get("subquestion_slug") or path.parent.name),
            "subquestion_text": normalize_ws(
                data.get("subquestion") or data.get("subquestion_text") or ""
            ),
        }
    return mapping


def exact_rows(run_root: Path) -> list[dict[str, Any]]:
    subquestions = subquestion_text_map(run_root)
    rows: list[dict[str, Any]] = []
    for path in sorted((run_root / "loop-state").glob("*/iteration-*/exact-targets.json")):
        try:
            data = read_json(path)
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        subquestion_id = path.parent.parent.name
        base = subquestions.get(
            subquestion_id,
            {
                "subquestion_group_slug": "general",
                "subquestion_group_title": "General",
                "subquestion_id": subquestion_id,
                "subquestion_slug": subquestion_id,
                "subquestion_text": "",
            },
        )
        for item in data:
            if not isinstance(item, dict):
                continue
            if not exact_target_agent_verified(item):
                continue
            exact_query = normalize_ws(item.get("exact_query"))
            doi = clean_doi(item.get("doi"))
            openalex_id = normalize_ws(item.get("openalex_id"))
            rows.append(
                {
                    **base,
                    "lead_source": "exact_openalex_ledger",
                    "title": normalize_ws(item.get("openalex_title") or exact_query),
                    "doi": doi,
                    "identifier": doi or openalex_id or exact_query,
                    "year": normalize_ws(item.get("year")),
                    "venue": normalize_ws(item.get("venue")),
                    "publisher": normalize_ws(item.get("publisher")),
                    "landing_url": normalize_ws(item.get("landing_page_url") or item.get("url")),
                    "source_iteration": path.parent.name,
                    "source_exact_query": exact_query,
                    "openalex_id": openalex_id,
                    "openalex_match_status": normalize_ws(
                        item.get("openalex_status") or item.get("status")
                    ),
                    "agent_openalex_verified": "true",
                    "openalex_verification_status": normalize_ws(
                        item.get("openalex_verification_status")
                        or item.get("verification_status")
                        or "agent_verified"
                    ),
                    "source_reference_index": "iteration_exact_target:" + exact_query,
                }
            )
    return rows


def truthy(value: Any) -> bool:
    return normalize_ws(value).lower() in {"1", "true", "yes", "y", "agent_verified"}


def exact_target_agent_verified(item: dict[str, Any]) -> bool:
    status = normalize_ws(
        item.get("openalex_verification_status")
        or item.get("verification_status")
        or item.get("status")
    ).lower()
    if status in {
        "semantic_mismatch",
        "needs_agent_disambiguation",
        "openalex_not_found",
        "openalex_request_failed",
        "low_confidence",
        "outside_requested_year_window",
    }:
        return False
    if truthy(item.get("agent_openalex_verified")):
        return True
    return status in {"agent_verified", "verified_by_agent", "openalex_agent_verified"}


def summary_rows(run_root: Path) -> list[dict[str, Any]]:
    json_path = run_root / "run-summary.json"
    if json_path.exists():
        data = read_json(json_path)
        return data if isinstance(data, list) else []
    csv_path = run_root / "run-summary.csv"
    return read_csv(csv_path) if csv_path.exists() else []


def article_marked_close(article_dir: Path) -> bool:
    if (article_dir / "recommended-references.allow").exists():
        return True
    note = article_dir / "reading-note-zh.md"
    if not note.exists():
        return False
    text = note.read_text(encoding="utf-8", errors="replace").lower()
    if "worth_close_reading: true" not in text:
        return False
    return re.search(r"worth_close_reading_score_0_to_5:\s*[45](?:\.0)?\b", text) is not None


def recommended_items(article_dir: Path) -> list[dict[str, Any]]:
    json_path = article_dir / "recommended-references.json"
    csv_path = article_dir / "recommended-references.csv"
    if json_path.exists():
        data = read_json(json_path)
        if isinstance(data, dict):
            data = data.get("references") or data.get("recommended_references") or []
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
    return read_csv(csv_path) if csv_path.exists() else []


def recommended_rows(run_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for summary in summary_rows(run_root):
        if normalize_ws(summary.get("status")) != "captured":
            continue
        article_dir_text = normalize_ws(summary.get("article_dir"))
        if not article_dir_text:
            continue
        article_dir = Path(article_dir_text)
        if not article_dir.exists() or not article_marked_close(article_dir):
            continue
        for index, item in enumerate(recommended_items(article_dir), start=1):
            reference_text = normalize_ws(item.get("reference_text") or item.get("reference"))
            title = normalize_ws(item.get("title") or reference_text)
            doi = clean_doi(item.get("doi") or item.get("parsed_doi"))
            arxiv_id = arxiv_id_from_text(" ".join([title, reference_text, normalize_ws(item.get("url"))]))
            rows.append(
                {
                    "subquestion_group_slug": normalize_ws(
                        summary.get("subquestion_group_slug") or "general"
                    ),
                    "subquestion_group_title": normalize_ws(
                        summary.get("subquestion_group_title") or "General"
                    ),
                    "subquestion_id": normalize_ws(summary.get("subquestion_id")),
                    "subquestion_slug": normalize_ws(summary.get("subquestion_slug")),
                    "subquestion_text": normalize_ws(summary.get("subquestion_text")),
                    "lead_source": "recommended_reference",
                    "title": title,
                    "doi": doi,
                    "identifier": doi or arxiv_id or title,
                    "year": normalize_ws(item.get("year")),
                    "venue": normalize_ws(item.get("venue") or item.get("journal")),
                    "publisher": normalize_ws(item.get("publisher")),
                    "landing_url": normalize_ws(
                        item.get("landing_url") or item.get("url") or item.get("link")
                    ),
                    "source_article_title": normalize_ws(summary.get("title") or article_dir.name),
                    "source_article_dir": str(article_dir),
                    "source_reference_index": normalize_ws(
                        item.get("source_reference_index")
                        or item.get("reference_index")
                        or str(index)
                    ),
                    "citation_contexts": normalize_ws(
                        item.get("citation_contexts") or item.get("citation_context") or item.get("context")
                    ),
                }
            )
    return rows


def pdf_url_for_source(row: dict[str, Any], source: str) -> str:
    doi = clean_doi(row.get("doi"))
    landing = normalize_ws(row.get("landing_url"))
    if source == "arxiv":
        arxiv_id = arxiv_id_from_text(
            " ".join([landing, row.get("identifier") or "", row.get("title") or ""])
        )
        return f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else ""
    if source == "science" and doi:
        return f"https://www.science.org/doi/pdf/{quote(doi, safe='/')}"
    if source == "nature":
        match = re.search(r"/articles/([^/?#]+)", landing)
        article_id = match.group(1) if match else ""
        if not article_id and doi:
            article_id = doi.rsplit("/", 1)[-1]
        if article_id:
            return f"https://www.nature.com/articles/{article_id}.pdf"
    return ""


def is_science_family(doi: str, host: str, publisher: str, venue: str, text: str) -> bool:
    if host == "science.org" or host.endswith(".science.org"):
        return True
    if "science.org" in text or doi.startswith("10.1126/"):
        return True
    if any(marker in publisher for marker in SCIENCE_PUBLISHER_MARKERS):
        return True
    return venue in SCIENCE_FAMILY_VENUES


def is_nature_family(doi: str, host: str, publisher: str, venue: str, text: str) -> bool:
    if host == "nature.com" or host.endswith(".nature.com"):
        return True
    if "nature.com" in text or doi.startswith("10.1038/"):
        return True
    if any(marker in publisher for marker in NATURE_PUBLISHER_MARKERS):
        return True
    if venue in NATURE_FAMILY_VENUES or venue.startswith("npj "):
        return True
    return venue == "nature" or (venue.startswith("nature ") and not venue.startswith("nature and "))


def route_lead(row: dict[str, Any]) -> dict[str, str]:
    doi = clean_doi(row.get("doi"))
    landing = normalize_ws(row.get("landing_url"))
    host = urlparse(landing).netloc.lower()
    publisher = normalize_ws(row.get("publisher")).lower()
    venue = normalize_ws(row.get("venue")).lower()
    text = " ".join(
        [
            doi,
            landing.lower(),
            publisher,
            venue,
            normalize_ws(row.get("title")).lower(),
            normalize_ws(row.get("identifier")).lower(),
        ]
    )

    if "arxiv" in text or arxiv_id_from_text(text):
        return {
            "route": "pdf",
            "pdf_source": "arxiv",
            "structured_source": "",
            "pdf_url": pdf_url_for_source(row, "arxiv"),
            "manual_reason": "",
        }
    if is_science_family(doi, host, publisher, venue, text):
        return {
            "route": "pdf",
            "pdf_source": "science",
            "structured_source": "",
            "pdf_url": pdf_url_for_source(row, "science"),
            "manual_reason": "",
        }
    if is_nature_family(doi, host, publisher, venue, text):
        return {
            "route": "pdf",
            "pdf_source": "nature",
            "structured_source": "",
            "pdf_url": pdf_url_for_source(row, "nature"),
            "manual_reason": "",
        }
    for domain, source in STRUCTURED_HOSTS.items():
        if host == domain or host.endswith("." + domain):
            return {
                "route": "structured",
                "structured_source": source,
                "pdf_source": "",
                "pdf_url": "",
                "manual_reason": "",
            }
    if doi.startswith("10.1016/"):
        return structured_route("elsevier")
    if doi.startswith("10.1021/"):
        return structured_route("acs")
    if doi.startswith(("10.1002/", "10.1111/")):
        return structured_route("wiley")
    if doi.startswith("10.1007/"):
        return structured_route("springer")
    return {
        "route": "manual_pdf_hold",
        "pdf_source": "manual",
        "structured_source": "",
        "pdf_url": "",
        "manual_reason": "unsupported_pdf_source",
    }


def structured_route(source: str) -> dict[str, str]:
    return {
        "route": "structured",
        "structured_source": source,
        "pdf_source": "",
        "pdf_url": "",
        "manual_reason": "",
    }


def dedupe_key(row: dict[str, Any]) -> str:
    doi = clean_doi(row.get("doi"))
    if doi:
        return "doi:" + doi
    openalex_id = normalize_ws(row.get("openalex_id")).lower()
    if openalex_id:
        return "openalex:" + openalex_id
    arxiv_id = arxiv_id_from_text(
        " ".join([row.get("identifier") or "", row.get("landing_url") or "", row.get("title") or ""])
    )
    if arxiv_id:
        return "arxiv:" + arxiv_id
    return "title:" + normalized_title_key(row.get("title"))


def provenance_snapshot(row: dict[str, Any]) -> dict[str, str]:
    keys = [
        "lead_source",
        "source_article_dir",
        "source_reference_index",
        "citation_contexts",
        "source_iteration",
        "source_exact_query",
        "openalex_id",
        "openalex_match_status",
        "agent_openalex_verified",
        "openalex_verification_status",
    ]
    return {key: normalize_ws(row.get(key)) for key in keys if normalize_ws(row.get(key))}


def build_supplemental_ledger(run_root: Path) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    sources: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in [*exact_rows(run_root), *recommended_rows(run_root)]:
        complete = {field: "" for field in FIELDS}
        complete.update(row)
        complete["doi"] = clean_doi(complete.get("doi"))
        complete["identifier"] = normalize_ws(
            complete.get("identifier") or complete.get("doi") or complete.get("title")
        )
        complete.update(route_lead(complete))
        complete["action"] = {
            "structured": "structured_capture",
            "pdf": "pdf_capture",
        }.get(complete["route"], "manual_pdf_hold")

        key = dedupe_key(complete)
        if key not in merged:
            merged[key] = complete
        else:
            existing = merged[key]
            if complete["lead_source"] and complete["lead_source"] not in existing["lead_source"].split("+"):
                existing["lead_source"] = "+".join(
                    part for part in [existing["lead_source"], complete["lead_source"]] if part
                )
            for field in FIELDS:
                if not existing.get(field) and complete.get(field):
                    existing[field] = complete[field]
        sources[key].append(provenance_snapshot(complete))

    rows = list(merged.values())
    for row in rows:
        row["merged_sources_json"] = json.dumps(sources[dedupe_key(row)], ensure_ascii=False)
    rows.sort(key=lambda item: (item.get("subquestion_id") or "", item.get("route") or "", item.get("title") or ""))
    return rows


def next_ref_dir(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    existing = [
        int(match.group(1))
        for path in base.glob("ref_*")
        if (match := re.match(r"ref_(\d+)$", path.name))
    ]
    return base / f"ref_{max(existing, default=0) + 1:03d}"


def supplemental_followup_key(row: dict[str, Any]) -> str:
    key = dedupe_key(row)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return f"{key}:{digest}"


def existing_ref_dir_for_key(base: Path, key: str) -> Path | None:
    if not base.exists():
        return None
    for article_dir in sorted(base.glob("ref_*")):
        metadata_path = article_dir / "metadata.json"
        if not metadata_path.exists():
            continue
        try:
            metadata = read_json(metadata_path)
        except Exception:
            continue
        if isinstance(metadata, dict) and metadata.get("supplemental_followup_key") == key:
            return article_dir
    return None


def has_valid_source_pdf(article_dir: Path) -> bool:
    source_pdf = article_dir / "source.pdf"
    try:
        return source_pdf.is_file() and source_pdf.stat().st_size > 8 and source_pdf.read_bytes()[:4] == b"%PDF"
    except OSError:
        return False


def ensure_pdf_folder(run_root: Path, row: dict[str, Any]) -> dict[str, Any]:
    pdf_source = row.get("pdf_source") or "manual"
    article_base = (
        run_root
        / "subquestions"
        / normalize_ws(row.get("subquestion_group_slug") or "general")
        / normalize_ws(row.get("subquestion_id") or "unknown")
        / "references"
        / "pdf"
        / pdf_source
        / "articles"
    )
    followup_key = supplemental_followup_key(row)
    article_dir = existing_ref_dir_for_key(article_base, followup_key) or next_ref_dir(article_base)
    article_dir.mkdir(parents=True, exist_ok=True)

    metadata = {
        "title": row.get("title") or "",
        "doi": row.get("doi") or "",
        "year": row.get("year") or "",
        "journal": row.get("venue") or "",
        "publisher": row.get("publisher") or "",
        "url": row.get("landing_url") or "",
        "pdf_url": row.get("pdf_url") or "",
        "source_role": "reference",
        "lead_source": row.get("lead_source") or "",
        "article_type": "pdf-pending",
        "capture_depth": 2,
        "parent_article_dir": row.get("source_article_dir") or "",
        "parent_reference_index": row.get("source_reference_index") or "",
        "subquestion_id": row.get("subquestion_id") or "",
        "subquestion_group_slug": row.get("subquestion_group_slug") or "general",
        "supplemental_followup_key": followup_key,
    }
    (article_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    status_path = article_dir / "pdf-capture-status.json"
    if not (has_valid_source_pdf(article_dir) and status_path.exists()):
        status = {
            "status": "manual_pdf_needed" if row.get("route") == "manual_pdf_hold" else "pdf_pending",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "pdf_url": row.get("pdf_url") or "",
            "manual_reason": row.get("manual_reason") or "",
        }
        status_path.write_text(
            json.dumps(status, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if row.get("route") == "manual_pdf_hold":
        (article_dir / "PDF_NEEDED.md").write_text(
            "# PDF Needed\n\n"
            f"- Title: {row.get('title') or ''}\n"
            f"- DOI: {row.get('doi') or ''}\n"
            f"- Landing URL: {row.get('landing_url') or ''}\n"
            f"- Reason: {row.get('manual_reason') or ''}\n"
            "\nPlace the downloaded PDF at `source.pdf` in this folder.\n",
            encoding="utf-8",
        )
    row["target_article_dir"] = str(article_dir)
    return row


def normalize_output_row(row: dict[str, Any]) -> dict[str, Any]:
    item = {field: "" for field in FIELDS}
    item.update({key: value for key, value in row.items() if key in item})
    item["doi"] = clean_doi(item.get("doi"))
    item["identifier"] = normalize_ws(item.get("identifier") or item.get("doi") or item.get("title"))
    if not item["route"]:
        item.update(route_lead(item))
    if not item["action"]:
        item["action"] = {
            "structured": "structured_capture",
            "pdf": "pdf_capture",
        }.get(item["route"], "manual_pdf_hold")
    return item


def write_supplemental_outputs(run_root: Path, rows: list[dict[str, Any]]) -> dict[str, int]:
    routed: dict[str, list[dict[str, Any]]] = {
        "structured": [],
        "pdf": [],
        "manual_pdf_hold": [],
    }
    final_rows: list[dict[str, Any]] = []
    for row in rows:
        item = normalize_output_row(row)
        if item["route"] in {"pdf", "manual_pdf_hold"}:
            item = ensure_pdf_folder(run_root, item)
        routed.setdefault(item["route"], []).append(item)
        final_rows.append(item)

    write_json(run_root / "supplemental-followup-ledger.json", final_rows)
    write_csv(run_root / "supplemental-followup-ledger.csv", final_rows)
    write_json(run_root / "structured-reference-queue.json", routed["structured"])
    write_csv(run_root / "structured-reference-queue.csv", routed["structured"])
    write_json(run_root / "pdf-followup-queue.json", routed["pdf"])
    write_csv(run_root / "pdf-followup-queue.csv", routed["pdf"])
    write_json(run_root / "manual-pdf-hold.json", routed["manual_pdf_hold"])
    write_csv(run_root / "manual-pdf-hold.csv", routed["manual_pdf_hold"])

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in final_rows:
        grouped[
            (
                normalize_ws(row.get("subquestion_group_slug") or "general"),
                normalize_ws(row.get("subquestion_id") or "unknown"),
            )
        ].append(row)
    for (group, subquestion_id), items in grouped.items():
        folder = run_root / "subquestions" / group / subquestion_id
        write_json(folder / "supplemental-followup-ledger.json", items)
        write_csv(folder / "supplemental-followup-ledger.csv", items)
        structured_rows = [item for item in items if item["route"] == "structured"]
        pdf_rows = [item for item in items if item["route"] == "pdf"]
        manual_rows = [item for item in items if item["route"] == "manual_pdf_hold"]
        write_json(folder / "structured-reference-queue.json", structured_rows)
        write_csv(folder / "structured-reference-queue.csv", structured_rows)
        write_json(folder / "pdf-followup-queue.json", pdf_rows)
        write_csv(folder / "pdf-followup-queue.csv", pdf_rows)
        write_json(folder / "manual-pdf-hold.json", manual_rows)
        write_csv(folder / "manual-pdf-hold.csv", manual_rows)

    summary = [
        "# Supplemental Follow-up Summary",
        "",
        f"- Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"- Total leads: {len(final_rows)}",
        f"- Structured queue: {len(routed['structured'])}",
        f"- PDF queue: {len(routed['pdf'])}",
        f"- Manual PDF hold: {len(routed['manual_pdf_hold'])}",
        "",
    ]
    (run_root / "supplemental-followup-summary.md").write_text(
        "\n".join(summary),
        encoding="utf-8",
    )
    return {
        "total": len(final_rows),
        "structured": len(routed["structured"]),
        "pdf": len(routed["pdf"]),
        "manual_pdf_hold": len(routed["manual_pdf_hold"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    rows = build_supplemental_ledger(args.run_dir.resolve())
    counts = write_supplemental_outputs(args.run_dir.resolve(), rows)
    print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
