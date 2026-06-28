#!/usr/bin/env python3
"""Capture supplemental PDF queue rows by direct download or OpenCLI browser."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen

import opencli_browser


MIN_PDF_BYTES = 8

DOWNLOAD_CLICK_SCRIPT = r"""(() => {
  const selectors = [
    'a[download]',
    'a[href$=".pdf"]',
    'a[href*="/pdf"]',
    'button[aria-label*="Download" i]',
    'a[aria-label*="Download" i]',
    'button[title*="Download" i]',
    'a[title*="Download" i]'
  ];
  for (const selector of selectors) {
    const element = document.querySelector(selector);
    if (element) {
      element.click();
      return JSON.stringify({clicked: true, selector});
    }
  }
  return JSON.stringify({clicked: false});
})()"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def target_dir(row: dict[str, str]) -> Path:
    value = str(row.get("target_article_dir") or "").strip()
    if not value:
        raise ValueError("missing_target_article_dir")
    return Path(value)


def target_dir_or_none(row: dict[str, str]) -> Path | None:
    value = str(row.get("target_article_dir") or "").strip()
    return Path(value) if value else None


def write_status(article_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    article_dir.mkdir(parents=True, exist_ok=True)
    status = {
        "status": payload.get("status") or "unknown",
        "updated_at": now_iso(),
    }
    status.update(json_safe(payload))
    (article_dir / "pdf-capture-status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return status


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"byte_count": len(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): json_safe(item)
            for key, item in value.items()
            if key not in {"pdf_bytes", "dataBase64"}
        }
    return str(value)


def row_context(row: dict[str, str]) -> dict[str, str]:
    return {
        "title": str(row.get("title") or ""),
        "doi": str(row.get("doi") or ""),
        "pdf_url": str(row.get("pdf_url") or ""),
        "target_article_dir": str(row.get("target_article_dir") or ""),
    }


def clean_doi(value: Any) -> str:
    text = unquote(str(value or "")).strip()
    match = re.search(r"10\.\d{4,9}/[^\s\"'<>]+", text, flags=re.I)
    if not match:
        return ""
    doi = match.group(0).rstrip(").,;]")
    if doi.endswith(".pdf"):
        doi = doi[:-4]
    return doi


def arxiv_id_from_text(text: Any) -> str:
    match = re.search(
        r"(?:arxiv:|arxiv\.org/(?:abs|pdf)/)(\d{4}\.\d{4,5})(?:v\d+)?",
        str(text or ""),
        flags=re.I,
    )
    return match.group(1) if match else ""


def nature_article_id(row: dict[str, str]) -> str:
    text = " ".join(
        [
            str(row.get("landing_url") or ""),
            str(row.get("pdf_url") or ""),
            str(row.get("url") or ""),
        ]
    )
    match = re.search(r"/articles/([^/?#\s]+)", text)
    if match:
        return match.group(1).removesuffix(".pdf")
    doi = clean_doi(row.get("doi") or row.get("identifier") or text)
    if doi.startswith("10.1038/"):
        return doi.rsplit("/", 1)[-1]
    return ""


def is_publisher_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host in {
        "www.science.org",
        "www.nature.com",
        "www.sciencedirect.com",
        "pubs.acs.org",
        "onlinelibrary.wiley.com",
        "link.springer.com",
    } or any(
        host.endswith("." + allowed)
        for allowed in {
            "science.org",
            "nature.com",
            "sciencedirect.com",
            "acs.org",
            "wiley.com",
            "springer.com",
        }
    )


def unique_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        value = url.strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def direct_pdf_urls(row: dict[str, str]) -> list[str]:
    """Return direct non-Publisher PDF URLs to try before browser capture."""
    source = str(row.get("pdf_source") or "").strip().lower()
    pdf_url = str(row.get("pdf_url") or "").strip()
    urls: list[str] = []
    if source == "arxiv":
        arxiv_id = arxiv_id_from_text(
            " ".join(
                [
                    pdf_url,
                    str(row.get("landing_url") or ""),
                    str(row.get("identifier") or ""),
                    str(row.get("title") or ""),
                ]
            )
        )
        if arxiv_id:
            urls.append(f"https://arxiv.org/pdf/{arxiv_id}")
    elif source == "nature":
        article_id = nature_article_id(row)
        if article_id:
            urls.append(f"https://www.nature.com/articles/{article_id}.pdf")
    elif source == "science":
        doi = clean_doi(
            row.get("doi")
            or row.get("identifier")
            or row.get("pdf_url")
            or row.get("landing_url")
        )
        if doi:
            urls.append(f"https://www.science.org/doi/pdf/{quote(doi, safe='/')}")
    if pdf_url and not is_publisher_url(pdf_url):
        urls.append(pdf_url)
    return unique_urls(urls)


def browser_fetch_urls(row: dict[str, str]) -> list[str]:
    """Return Publisher/current-session PDF URLs for in-browser credentialed fetch."""
    source = str(row.get("pdf_source") or "").strip().lower()
    pdf_url = str(row.get("pdf_url") or "").strip()
    doi = clean_doi(row.get("doi") or row.get("identifier") or pdf_url)
    urls: list[str] = []
    if source == "science" and doi:
        encoded_doi = quote(doi, safe="/")
        urls.extend(
            [
                f"https://www.science.org/doi/pdf/{encoded_doi}",
                f"https://www.science.org/doi/pdf/{encoded_doi}?download=true",
                f"https://www.science.org/doi/pdfdirect/{encoded_doi}",
                f"https://www.science.org/doi/epdf/{encoded_doi}",
            ]
        )
    elif source == "nature":
        article_id = nature_article_id(row)
        if article_id:
            urls.append(f"https://www.nature.com/articles/{article_id}.pdf")
    if pdf_url:
        urls.append(pdf_url)
        if "/doi/pdf/" in pdf_url:
            urls.append(pdf_url.replace("/doi/pdf/", "/doi/epdf/"))
            urls.append(pdf_url.replace("/doi/pdf/", "/doi/pdfdirect/"))
            urls.append(pdf_url + ("&download=true" if "?" in pdf_url else "?download=true"))
    return unique_urls(urls)


def browser_fetch_pdf(row: dict[str, str], session: str) -> dict[str, Any]:
    """Fetch PDF candidates inside the OpenCLI browser session with credentials."""
    urls = browser_fetch_urls(row)
    if not urls:
        return {"status": "blocked_missing_pdf_url", "attempted_urls": []}
    script = r"""
      (async (urls) => {
        const attempts = [];
        for (const url of urls) {
          try {
            const resp = await fetch(url, {
              credentials: 'include',
              headers: {'Accept': 'application/pdf,application/octet-stream,*/*'}
            });
            const ct = resp.headers.get('content-type') || '';
            const ab = await resp.arrayBuffer();
            const bytes = new Uint8Array(ab);
            const head = Array.from(bytes.slice(0, 16));
            const isPdf = bytes.length >= 5 &&
              bytes[0] === 0x25 && bytes[1] === 0x50 && bytes[2] === 0x44 &&
              bytes[3] === 0x46 && bytes[4] === 0x2d;
            if (resp.ok && isPdf) {
              let binary = '';
              const chunkSize = 0x8000;
              for (let offset = 0; offset < bytes.length; offset += chunkSize) {
                binary += String.fromCharCode(...bytes.slice(offset, offset + chunkSize));
              }
              return JSON.stringify({
                status: 'captured',
                url,
                content_type: ct,
                bytes: bytes.length,
                dataBase64: btoa(binary)
              });
            }
            attempts.push({
              url,
              status: resp.status,
              content_type: ct,
              bytes: bytes.length,
              head
            });
          } catch (error) {
            attempts.push({url, error: String(error && error.message || error)});
          }
        }
        return JSON.stringify({status: 'blocked_not_pdf_response', attempted_urls: attempts});
      })(%s)
    """ % json.dumps(urls)
    result = opencli_browser.eval_json(session, script, timeout=180)
    if not isinstance(result, dict):
        return {"status": "blocked_not_pdf_response", "attempted_urls": []}
    if result.get("status") == "captured":
        try:
            pdf_bytes = base64.b64decode(str(result.get("dataBase64") or ""))
        except Exception:
            pdf_bytes = b""
        if pdf_bytes.startswith(b"%PDF"):
            cleaned = dict(result)
            cleaned.pop("dataBase64", None)
            cleaned["pdf_bytes"] = pdf_bytes
            return cleaned
        return {
            "status": "blocked_invalid_pdf_response",
            "url": str(result.get("url") or ""),
            "bytes": len(pdf_bytes),
        }
    return result


def direct_download(row: dict[str, str], timeout: int = 60) -> dict[str, Any]:
    """Download direct PDF URL candidates into source.pdf."""
    article_dir: Path | None = None
    try:
        article_dir = target_dir(row)
        urls = direct_pdf_urls(row)
        if not urls:
            return write_status(
                article_dir,
                {
                    **row_context(row),
                    "status": "blocked_missing_pdf_url",
                    "method": "direct_http",
                },
            )
        failures: list[dict[str, Any]] = []
        last_bytes = 0
        for url in urls:
            try:
                request = Request(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/149.0.0.0 Safari/537.36"
                        ),
                        "Accept": "application/pdf,application/octet-stream,*/*",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Connection": "close",
                    },
                )
                with urlopen(request, timeout=timeout) as response:
                    data = response.read()
                    content_type = str(response.headers.get("content-type", "") or "")
                last_bytes = len(data)
                if not data.startswith(b"%PDF"):
                    failures.append(
                        {
                            "url": url,
                            "content_type": content_type,
                            "bytes": len(data),
                        }
                    )
                    continue
                article_dir.mkdir(parents=True, exist_ok=True)
                (article_dir / "source.pdf").write_bytes(data)
                return write_status(
                    article_dir,
                    {
                        **row_context(row),
                        "status": "captured",
                        "method": "direct_http",
                        "direct_url": url,
                        "bytes": len(data),
                        "source_pdf": str(article_dir / "source.pdf"),
                    },
                )
            except Exception as exc:
                failures.append({"url": url, "error": str(exc)[:500]})
        return write_status(
            article_dir,
            {
                **row_context(row),
                "status": "blocked_not_pdf_response",
                "method": "direct_http",
                "bytes": last_bytes,
                "attempted_urls": failures,
            },
        )
    except Exception as exc:
        if article_dir is not None:
            return write_status(
                article_dir,
                {
                    **row_context(row),
                    "status": "exception",
                    "method": "direct_http",
                    "error": str(exc)[:500],
                },
            )
        raise


def pdf_snapshot(download_dir: Path) -> set[Path]:
    if not download_dir.exists():
        return set()
    return {path.resolve() for path in download_dir.glob("*.pdf") if path.is_file()}


def is_pdf_file(path: Path, min_size: int = MIN_PDF_BYTES) -> bool:
    try:
        if not path.is_file() or path.stat().st_size <= min_size:
            return False
        with path.open("rb") as handle:
            return handle.read(4) == b"%PDF"
    except OSError:
        return False


def newest_new_pdf(download_dir: Path, before: set[Path]) -> Path | None:
    if not download_dir.exists():
        return None
    candidates = [
        path
        for path in download_dir.glob("*.pdf")
        if path.is_file() and path.resolve() not in before
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def move_pdf_to_source(downloaded_pdf: Path, article_dir: Path) -> Path:
    article_dir.mkdir(parents=True, exist_ok=True)
    target = article_dir / "source.pdf"
    if target.exists():
        target.unlink()
    shutil.move(str(downloaded_pdf), str(target))
    return target


def write_pdf_bytes_to_source(pdf_bytes: bytes, article_dir: Path) -> Path:
    article_dir.mkdir(parents=True, exist_ok=True)
    target = article_dir / "source.pdf"
    if target.exists():
        target.unlink()
    target.write_bytes(pdf_bytes)
    return target


def browser_fetch_attempt_status(result: dict[str, Any]) -> dict[str, Any]:
    return {"browser_fetch_attempt": json_safe(result)}


def preferred_browser_open_url(row: dict[str, str]) -> str:
    urls = browser_fetch_urls(row)
    if urls:
        return urls[0]
    return str(row.get("pdf_url") or "").strip()


def browser_exception_status(error: Exception) -> str:
    text = str(error).lower()
    if "authentication" in text or "login" in text or "sign in" in text:
        return "blocked_publisher_auth"
    return "exception"


def capture_browser_pdf(
    row: dict[str, str],
    session: str,
    download_dir: Path,
    wait_seconds: int = 20,
    prior_direct_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Open a Publisher PDF URL and record whether a browser download appeared."""
    article_dir: Path | None = None
    click_result: Any = None
    direct_attempt = {"direct_attempt": prior_direct_status} if prior_direct_status else {}
    browser_fetch_attempt: dict[str, Any] = {}
    try:
        article_dir = target_dir(row)
        pdf_url = str(row.get("pdf_url") or "").strip()
        before = pdf_snapshot(download_dir)
        if not pdf_url:
            return write_status(
                article_dir,
                {
                    **row_context(row),
                    "status": "blocked_missing_pdf_url",
                    "method": "opencli_browser",
                    "download_dir": str(download_dir),
                    **direct_attempt,
                },
            )
        browser_url = preferred_browser_open_url(row)
        opencli_browser.open_url_allow_redirect(session, browser_url, timeout=90)
        opencli_browser.wait_time(session, wait_seconds)
        browser_fetch_result = browser_fetch_pdf(row, session)
        browser_fetch_attempt = browser_fetch_attempt_status(browser_fetch_result)
        pdf_bytes = browser_fetch_result.get("pdf_bytes")
        if browser_fetch_result.get("status") == "captured" and isinstance(pdf_bytes, bytes) and pdf_bytes.startswith(b"%PDF"):
            source_pdf = write_pdf_bytes_to_source(pdf_bytes, article_dir)
            return write_status(
                article_dir,
                {
                    **row_context(row),
                    "status": "downloaded",
                    "method": "opencli_browser_fetch",
                    "download_dir": str(download_dir),
                    "browser_url": browser_url,
                    "source_pdf": str(source_pdf),
                    "bytes": len(pdf_bytes),
                    **direct_attempt,
                    **browser_fetch_attempt,
                },
            )
        click_result = opencli_browser.eval_json(session, DOWNLOAD_CLICK_SCRIPT, timeout=60)
        opencli_browser.wait_time(session, wait_seconds)
        downloaded = newest_new_pdf(download_dir, before)
        if downloaded is None:
            return write_status(
                article_dir,
                {
                    **row_context(row),
                    "status": "blocked_pdf_viewer_download",
                    "method": "opencli_browser",
                    "download_dir": str(download_dir),
                    "browser_url": browser_url,
                    "click_result": click_result,
                    **direct_attempt,
                    **browser_fetch_attempt,
                },
            )
        if not is_pdf_file(downloaded):
            try:
                downloaded.unlink()
            except OSError:
                pass
            return write_status(
                article_dir,
                {
                    **row_context(row),
                    "status": "blocked_invalid_pdf_download",
                    "method": "opencli_browser",
                    "download_dir": str(download_dir),
                    "browser_url": browser_url,
                    "click_result": click_result,
                    "downloaded_pdf": str(downloaded),
                    **direct_attempt,
                    **browser_fetch_attempt,
                },
            )
        source_pdf = move_pdf_to_source(downloaded, article_dir)
        return write_status(
            article_dir,
            {
                **row_context(row),
                "status": "downloaded",
                "method": "opencli_browser",
                "download_dir": str(download_dir),
                "browser_url": browser_url,
                "click_result": click_result,
                "source_pdf": str(source_pdf),
                **direct_attempt,
                **browser_fetch_attempt,
            },
        )
    except Exception as exc:
        if article_dir is not None:
            return write_status(
                article_dir,
                {
                    **row_context(row),
                    "status": browser_exception_status(exc),
                    "method": "opencli_browser",
                    "download_dir": str(download_dir),
                    "click_result": click_result,
                    "error": str(exc)[:500],
                    **direct_attempt,
                    **browser_fetch_attempt,
                },
            )
        raise


def count_status(counts: dict[str, int], status: str) -> None:
    counts[status] = counts.get(status, 0) + 1


def read_queue(queue_csv: Path) -> list[dict[str, str]]:
    with queue_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def print_publisher_login_reminder(rows: list[dict[str, str]]) -> None:
    sources = {
        str(row.get("pdf_source") or "").strip().lower()
        for row in rows
    }
    if sources & {"science", "nature"}:
        print(
            "Reminder: before supplemental PDF capture, confirm the OpenCLI browser "
            "profile is logged in to Science/Nature and can download PDFs from the "
            "publisher site directly.",
            file=sys.stderr,
        )


def existing_source_pdf_status(row: dict[str, str]) -> dict[str, Any] | None:
    article_dir = target_dir_or_none(row)
    if article_dir is None:
        return None
    source_pdf = article_dir / "source.pdf"
    if not is_pdf_file(source_pdf):
        return None
    return write_status(
        article_dir,
        {
            **row_context(row),
            "status": "already_captured",
            "method": "existing_source_pdf",
            "bytes": source_pdf.stat().st_size,
            "source_pdf": str(source_pdf),
        },
    )


def capture_pdf_queue(
    queue_csv: Path,
    opencli_session: str,
    download_dir: Path | None = None,
) -> dict[str, int]:
    """Capture all rows in a supplemental PDF queue CSV."""
    counts: dict[str, int] = {"total": 0}
    downloads = download_dir or (Path.home() / "Downloads")
    rows = read_queue(queue_csv)
    print_publisher_login_reminder(rows)
    for row in rows:
        counts["total"] = counts.get("total", 0) + 1
        try:
            if target_dir_or_none(row) is None:
                count_status(counts, "blocked_malformed_row")
                continue
            existing = existing_source_pdf_status(row)
            if existing is not None:
                count_status(counts, str(existing.get("status") or "already_captured"))
                continue
            source = str(row.get("pdf_source") or "").strip().lower()
            if source in {"arxiv", "nature", "science"}:
                result = direct_download(row)
                if str(result.get("status") or "") == "captured" or source == "arxiv":
                    count_status(counts, str(result.get("status") or "unknown"))
                    continue
                result = capture_browser_pdf(
                    row,
                    opencli_session,
                    downloads,
                    prior_direct_status=result,
                )
            else:
                result = capture_browser_pdf(row, opencli_session, downloads)
            count_status(counts, str(result.get("status") or "unknown"))
        except Exception:
            article_dir = target_dir_or_none(row)
            if article_dir is not None:
                write_status(
                    article_dir,
                    {
                        **row_context(row),
                        "status": "blocked_malformed_row",
                        "method": "pdf_queue",
                    },
                )
            count_status(counts, "blocked_malformed_row")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("queue_csv", type=Path)
    parser.add_argument("--opencli-session", required=True)
    parser.add_argument("--download-dir", type=Path)
    args = parser.parse_args()
    counts = capture_pdf_queue(
        args.queue_csv.resolve(),
        args.opencli_session,
        args.download_dir.resolve() if args.download_dir else None,
    )
    print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
