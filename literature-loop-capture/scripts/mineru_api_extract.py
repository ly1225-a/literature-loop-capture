#!/usr/bin/env python3
"""Run MinerU precise API for local supplemental PDF article folders."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

import mineru_normalize

API_ROOT = "https://mineru.net"
UPLOAD_ENDPOINT = f"{API_ROOT}/api/v4/file-urls/batch"
POLL_ENDPOINT_TEMPLATE = f"{API_ROOT}/api/v4/extract-results/batch/{{batch_id}}"
TERMINAL_STATES = {"done", "failed"}
ACTIVE_STATES = {"waiting-file", "pending", "running", "converting"}


@dataclass(frozen=True)
class BatchFile:
    article_dir: Path
    source_pdf: Path
    file_name: str
    data_id: str


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def relative_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def slug(text: str, limit: int = 64) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")
    return (cleaned or "pdf")[:limit]


def scan_pdf_article_dirs(run_root: Path) -> list[Path]:
    return sorted(
        path.parent
        for path in run_root.glob(
            "subquestions/*/*/references/pdf/*/articles/*/source.pdf"
        )
    )


def valid_pdf(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 5:
        return False
    return path.read_bytes()[:5].startswith(b"%PDF")


def build_batch_files(run_root: Path, article_dirs: list[Path]) -> list[BatchFile]:
    rows: list[BatchFile] = []
    for article_dir in article_dirs:
        source_pdf = article_dir / "source.pdf"
        rel = article_dir.resolve().relative_to(run_root.resolve()).as_posix()
        digest = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:10]
        name = f"{slug(rel.replace('/', '-'), 80)}-{digest}.pdf"
        data_id = f"codex_{digest}"
        rows.append(
            BatchFile(
                article_dir=article_dir,
                source_pdf=source_pdf,
                file_name=name,
                data_id=data_id,
            )
        )
    return rows


def authorization_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
    }


def request_with_retries(
    method: str,
    url: str,
    *,
    attempts: int = 5,
    sleep_seconds: float = 5.0,
    **kwargs: Any,
) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.request(method, url, **kwargs)
            if response.status_code < 500:
                return response
            last_exc = RuntimeError(f"http_{response.status_code}")
        except requests.RequestException as exc:
            last_exc = exc
        if attempt < attempts:
            time.sleep(sleep_seconds * attempt)
    if last_exc:
        raise last_exc
    raise RuntimeError("request_failed_without_exception")


def submit_upload_batch(
    token: str,
    batch_files: list[BatchFile],
    *,
    model_version: str,
    language: str,
    enable_table: bool,
    enable_formula: bool,
    is_ocr: bool,
) -> dict[str, Any]:
    payload = {
        "model_version": model_version,
        "language": language,
        "enable_table": enable_table,
        "enable_formula": enable_formula,
        "files": [
            {
                "name": row.file_name,
                "data_id": row.data_id,
                "is_ocr": is_ocr,
            }
            for row in batch_files
        ],
    }
    response = request_with_retries(
        "POST",
        UPLOAD_ENDPOINT,
        headers=authorization_headers(token),
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    result = response.json()
    if result.get("code") != 0:
        raise RuntimeError(f"mineru_upload_request_failed:{result.get('msg')}")
    data = result.get("data") or {}
    file_urls = data.get("file_urls") or []
    if len(file_urls) != len(batch_files):
        raise RuntimeError(
            f"mineru_upload_url_count_mismatch:{len(file_urls)}!={len(batch_files)}"
        )
    return {"batch_id": data.get("batch_id"), "file_urls": file_urls, "raw": result}


def upload_source_pdfs(batch_files: list[BatchFile], file_urls: list[str]) -> list[dict[str, Any]]:
    uploaded: list[dict[str, Any]] = []
    for row, url in zip(batch_files, file_urls):
        with row.source_pdf.open("rb") as handle:
            response = request_with_retries("PUT", url, data=handle, timeout=600)
        ok = response.status_code in {200, 201, 204}
        uploaded.append(
            {
                "article_dir": str(row.article_dir),
                "file_name": row.file_name,
                "status": "uploaded" if ok else "upload_failed",
                "status_code": response.status_code,
                "bytes": row.source_pdf.stat().st_size,
            }
        )
        if not ok:
            raise RuntimeError(f"mineru_upload_failed:{row.file_name}:{response.status_code}")
    return uploaded


def poll_batch(
    token: str,
    batch_id: str,
    *,
    poll_interval: float,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    url = POLL_ENDPOINT_TEMPLATE.format(batch_id=batch_id)
    last_results: list[dict[str, Any]] = []
    while True:
        response = request_with_retries(
            "GET",
            url,
            headers=authorization_headers(token),
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"mineru_poll_failed:{payload.get('msg')}")
        data = payload.get("data") or {}
        results = data.get("extract_result") or []
        last_results = results if isinstance(results, list) else []
        states = {str(row.get("state") or "") for row in last_results}
        if states and states.issubset(TERMINAL_STATES):
            return last_results
        if time.monotonic() >= deadline:
            raise TimeoutError(f"mineru_poll_timeout:{batch_id}:{sorted(states)}")
        time.sleep(poll_interval)


def safe_extract_zip(zip_bytes: bytes, target_dir: Path) -> list[Path]:
    extracted: list[Path] = []
    target_dir.mkdir(parents=True, exist_ok=True)
    root = target_dir.resolve()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            output = (target_dir / member.filename).resolve()
            try:
                output.relative_to(root)
            except ValueError as exc:
                raise RuntimeError(f"unsafe_zip_member:{member.filename}") from exc
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, output.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            extracted.append(output)
    return extracted


def choose_markdown_file(mineru_dir: Path) -> Path | None:
    candidates = [path for path in mineru_dir.rglob("*.md") if path.name != "fulltext.md"]
    if not candidates:
        return None
    for path in candidates:
        if path.name == "full.md":
            return path
    return max(candidates, key=lambda path: path.stat().st_size)


def unpack_mineru_zip(
    article_dir: Path,
    zip_bytes: bytes,
    result: dict[str, Any],
) -> dict[str, Any]:
    mineru_dir = article_dir / "mineru"
    raw_dir = mineru_dir / "api-raw"
    extracted = safe_extract_zip(zip_bytes, raw_dir)
    markdown_file = choose_markdown_file(mineru_dir)
    if not markdown_file:
        status = {
            "status": "blocked_no_mineru_markdown",
            "updated_at": now_iso(),
            "extracted_files": [relative_path(article_dir, path) for path in extracted],
            **result,
        }
        write_json(article_dir / "mineru-api-status.json", status)
        return status
    fulltext = mineru_dir / "fulltext.md"
    shutil.copy2(markdown_file, fulltext)
    status = {
        "status": "mineru_zip_unpacked",
        "updated_at": now_iso(),
        "mineru_fulltext": "mineru/fulltext.md",
        "source_markdown": relative_path(article_dir, markdown_file),
        "extracted_files": [relative_path(article_dir, path) for path in extracted],
        **result,
    }
    write_json(article_dir / "mineru-api-status.json", status)
    return status


def download_result_zip(url: str) -> bytes:
    response = request_with_retries("GET", url, timeout=600)
    response.raise_for_status()
    data = response.content
    if not data.startswith(b"PK"):
        raise RuntimeError("mineru_result_not_zip")
    return data


def run_api_extract(
    run_root: Path,
    *,
    token: str,
    article_dirs: list[Path],
    model_version: str = "vlm",
    language: str = "en",
    enable_table: bool = True,
    enable_formula: bool = True,
    is_ocr: bool = False,
    batch_size: int = 10,
    poll_interval: float = 10.0,
    timeout_seconds: int = 3600,
    normalize: bool = True,
    force: bool = False,
) -> list[dict[str, Any]]:
    pending = []
    for article_dir in article_dirs:
        if not valid_pdf(article_dir / "source.pdf"):
            pending.append({"status": "missing_or_invalid_source_pdf", "article_dir": str(article_dir)})
            continue
        if not force and (article_dir / "mineru" / "fulltext.md").exists():
            pending.append({"status": "already_extracted", "article_dir": str(article_dir)})
            continue
        pending.append(article_dir)

    results: list[dict[str, Any]] = [
        row for row in pending if isinstance(row, dict)
    ]
    dirs = [row for row in pending if isinstance(row, Path)]
    for start in range(0, len(dirs), batch_size):
        chunk = dirs[start : start + batch_size]
        batch_files = build_batch_files(run_root, chunk)
        submitted = submit_upload_batch(
            token,
            batch_files,
            model_version=model_version,
            language=language,
            enable_table=enable_table,
            enable_formula=enable_formula,
            is_ocr=is_ocr,
        )
        batch_id = submitted["batch_id"]
        upload_rows = upload_source_pdfs(batch_files, submitted["file_urls"])
        file_map = {row.file_name: row.article_dir for row in batch_files}
        api_results = poll_batch(
            token,
            str(batch_id),
            poll_interval=poll_interval,
            timeout_seconds=timeout_seconds,
        )
        results.extend(upload_rows)
        for api_result in api_results:
            file_name = str(api_result.get("file_name") or "")
            article_dir = file_map.get(file_name)
            if not article_dir:
                results.append({"status": "blocked_unmapped_result", **api_result})
                continue
            if api_result.get("state") != "done":
                blocked = {
                    "status": "blocked_mineru_failed",
                    "updated_at": now_iso(),
                    "batch_id": batch_id,
                    **api_result,
                }
                write_json(article_dir / "mineru-api-status.json", blocked)
                results.append({"article_dir": str(article_dir), **blocked})
                continue
            zip_url = api_result.get("full_zip_url")
            if not zip_url:
                blocked = {
                    "status": "blocked_missing_full_zip_url",
                    "updated_at": now_iso(),
                    "batch_id": batch_id,
                    **api_result,
                }
                write_json(article_dir / "mineru-api-status.json", blocked)
                results.append({"article_dir": str(article_dir), **blocked})
                continue
            zip_bytes = download_result_zip(str(zip_url))
            unpacked = unpack_mineru_zip(article_dir, zip_bytes, {"batch_id": batch_id, **api_result})
            results.append({"article_dir": str(article_dir), **unpacked})
            if normalize and unpacked.get("status") == "mineru_zip_unpacked":
                results.append(mineru_normalize.normalize_article_dir(article_dir))
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--article-dir", action="append", type=Path, default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--model-version", default="vlm")
    parser.add_argument("--language", default="en")
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-normalize", action="store_true")
    args = parser.parse_args()

    token = os.environ.get("MINERU_API_KEY")
    if not token:
        print(json.dumps([{"status": "blocked_missing_mineru_api_key"}], indent=2))
        return 1

    run_root = args.run_dir.resolve()
    if args.article_dir:
        article_dirs = [path.resolve() for path in args.article_dir]
    else:
        article_dirs = scan_pdf_article_dirs(run_root)
    if args.limit:
        article_dirs = article_dirs[: args.limit]

    try:
        results = run_api_extract(
            run_root,
            token=token,
            article_dirs=article_dirs,
            model_version=args.model_version,
            language=args.language,
            batch_size=args.batch_size,
            poll_interval=args.poll_interval,
            timeout_seconds=args.timeout_seconds,
            normalize=not args.no_normalize,
            force=args.force,
        )
    except Exception as exc:
        results = [{"status": f"blocked_exception:{type(exc).__name__}", "message": str(exc)}]
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(results, ensure_ascii=False, indent=2))
    bad = [
        row for row in results
        if str(row.get("status") or "").startswith(("blocked", "missing", "upload_failed"))
    ]
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
