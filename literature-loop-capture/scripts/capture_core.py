#!/usr/bin/env python3
"""Snapshot-only article page capture for review literature projects."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import shutil
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import opencli_browser  # noqa: E402


SUPPORTED = {
    "elsevier", "acs", "nature", "springer", "wiley", "tandfonline", "oxford",
    "acm",
}
OPEN_FIRST: set[str] = set()
DIRECT_REQUIRED = {"elsevier", "acs", "nature", "springer", "wiley", "tandfonline", "oxford", "acm"}
SECTION_ALIASES: dict[str, str] = {}


def clean_doi(value: str) -> str:
    return (value or "").strip().replace("https://doi.org/", "").replace("http://doi.org/", "")


def token(text: str, limit: int = 90) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_")
    return (text[:limit].rstrip("_") or "untitled")


def doi_token(doi: str) -> str:
    return token(doi.lower(), 80)


def canonical_section(section: str) -> str:
    return SECTION_ALIASES.get(str(section), str(section))


def first_author_token(authors: list[Any]) -> str:
    if not authors:
        return "Unknown"
    first = str(authors[0]).strip()
    first = re.sub(r"^Authors:\s*", "", first)
    first = re.sub(r"[\*\d\s]+$", "", first).strip()
    first = re.sub(r"(?:\s+[a-z])+$", "", first).strip()
    if "," in first:
        first = first.split(",", 1)[0]
    else:
        parts = first.split()
        first = parts[-1] if parts else first
    return token(first, 40) or "Unknown"


def article_folder_name(data: dict[str, Any], fallback_title: str, doi: str) -> str:
    title = data.get("title") or data.get("documentTitle") or fallback_title or doi or "untitled"
    year = data.get("year") or ""
    authors = data.get("authors") or []
    prefix = "_".join(p for p in [str(year), first_author_token(authors)] if p)
    name = token("_".join(p for p in [prefix, title] if p), 82)
    return f"{name}__{doi_token(doi)}" if doi else name


def split_author_segment(raw: str) -> list[str]:
    raw = re.sub(r"\*", "", raw or "")
    raw = re.sub(r"(?<=[a-z])(?=[A-Z][a-z])", "|", raw)
    raw = re.sub(r"\b\d+\b", "", raw)
    raw = re.sub(r"\b[a-z]\b", "", raw)
    raw = re.sub(r"\s+", " ", raw)
    parts = [p.strip(" ,;|") for p in re.split(r"\s*\|\s*|\s*,\s*|\s+ and\s+", raw)]
    return [p for p in parts if 2 <= len(p.split()) <= 6 and len(p) <= 90][:20]


def infer_authors_from_text(text: str, title: str = "") -> list[str]:
    text = text or ""
    if title:
        title_match = re.search(re.escape(title) + r"\s+(.{1,500}?)\s+(?:Open PDF Abstract|Abstract|Highlights)", text, re.I)
        if title_match:
            authors = split_author_segment(title_match.group(1))
            if authors:
                return authors
    patterns = [
        r"Author links open overlay panel\s+(.{1,500}?)\s+Show more",
        r"Authors?\s+(.{1,500}?)\s+(?:Abstract|Highlights|Keywords)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        authors = split_author_segment(match.group(1))
        if authors:
            return authors
    return []


def infer_year_from_text(text: str) -> str:
    years = re.findall(r"\b20\d{2}\b", (text or "")[:2500])
    for year in years:
        if year != "2026":
            return year
    return years[0] if years else ""


def replace_dir(src: Path, dst: Path) -> None:
    if src == dst:
        return
    if not src.exists():
        return
    if dst.exists():
        shutil.rmtree(dst)
    shutil.move(str(src), str(dst))


INLINE_SECTION_HEADINGS = [
    "Materials and methods",
    "Results and discussion",
    "Gas Chromatography-Mass Spectrometry (GC-MS) Analysis",
    "Gas Chromatography-Mass Spectrometry (GC–MS) Analysis",
    "Statistical Analysis",
    "Sensory Evaluation",
    "Preparation of Concentrated Citrus EOs",
    "Preparation",
    "Participants",
    "Procedure",
    "Data Analysis",
    "EEG Trial",
    "Introduction",
    "Background",
    "Methodology",
    "Materials",
    "Methods",
    "Results",
    "Discussion",
    "Conclusions",
    "Conclusion",
    "Chemicals",
]


INLINE_SECTION_STOP_WORDS = [
    "We",
    "The",
    "This",
    "In",
    "Our",
    "A",
    "An",
    "For",
    "To",
    "As",
    "Based",
    "Furthermore",
    "Additionally",
    "Worldwide",
    "Recently",
    "However",
    "Therefore",
    "Nevertheless",
    "Although",
    "Table",
    "Figure",
]


def _heading_number_tuple(value: str) -> tuple[int, ...]:
    value = value.strip().rstrip(".")
    if not re.match(r"^[1-9]\d?(?:\.\d+){0,4}$", value):
        return ()
    return tuple(int(part) for part in value.split("."))


def _is_next_numbered_heading(previous: tuple[int, ...] | None, current: tuple[int, ...]) -> bool:
    if not current:
        return False
    if previous is None:
        return current[0] == 1
    if current == previous:
        return False
    if len(current) > len(previous):
        return current[: len(previous)] == previous and current[-1] >= 1
    if len(current) == len(previous):
        if len(current) == 1:
            return current[0] == previous[0] + 1
        return current[:-1] == previous[:-1] and previous[-1] < current[-1] <= previous[-1] + 3
    parent = previous[: len(current) - 1]
    return current[:-1] == parent and current[-1] == previous[len(current) - 1] + 1


def _trim_inline_heading_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title or "").strip(" .;:")
    for word in INLINE_SECTION_STOP_WORDS:
        match = re.search(rf"\s+{re.escape(word)}\b", title)
        if not match:
            continue
        candidate = title[: match.start()].strip(" .;:")
        if len(candidate) >= 4:
            return candidate
    return title


def _generic_numbered_heading_candidates(text: str) -> list[re.Match[str]]:
    return list(
        re.finditer(
            r"(?<![\d.])(?P<number>[1-9]\d?(?:\.\d+){0,4}\.?)\s+"
            r"(?P<head>[A-Z][A-Za-z0-9 ,:;&/()'’\-–—]{2,140})"
            r"(?=\s|$)",
            text,
        )
    )


def split_generic_numbered_sections(text: str) -> list[dict[str, Any]]:
    """Recover numbered sections from flattened publisher text.

    Some pages expose real headings visually, but the browser text snapshot
    flattens them into one long block. This fallback keeps only monotonic
    article-like numbered headings, which filters common false positives such
    as decimal values and table row numbers.
    """
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    accepted: list[tuple[re.Match[str], tuple[int, ...], str]] = []
    previous: tuple[int, ...] | None = None
    for match in _generic_numbered_heading_candidates(text):
        number = _heading_number_tuple(match.group("number"))
        if not _is_next_numbered_heading(previous, number):
            continue
        title = _trim_inline_heading_title(match.group("head"))
        if len(title) < 4 or len(title) > 120:
            continue
        accepted.append((match, number, f"{match.group('number').rstrip('.')} {title}"))
        previous = number
    if len(accepted) < 2:
        return []
    sections: list[dict[str, Any]] = []
    for index, (match, number, title) in enumerate(accepted):
        next_start = accepted[index + 1][0].start() if index + 1 < len(accepted) else len(text)
        body = text[match.end():next_start].strip()
        sections.append({
            "index": index,
            "level": f"h{min(2 + len(number) - 1, 6)}",
            "title": title,
            "text": body,
            "inlineFallback": True,
        })
    return sections


def split_inline_numbered_sections(text: str) -> list[dict[str, Any]]:
    """Recover sections when a publisher page flattens numbered headings inline."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    alternatives = "|".join(re.escape(title) for title in sorted(INLINE_SECTION_HEADINGS, key=len, reverse=True))
    pattern = re.compile(
        rf"(?<!\d)(?P<title>(?P<number>\d+(?:\.\d+)*\.?)\s+(?P<head>{alternatives}))(?=\s|[A-Z0-9])",
        flags=re.IGNORECASE,
    )
    matches = list(pattern.finditer(text))
    if len(matches) < 2:
        return []
    sections: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        title = re.sub(r"\s+", " ", match.group("title")).strip()
        body = text[match.end():next_start].strip()
        level = "h3" if "." in match.group("number").rstrip(".") else "h2"
        sections.append({
            "index": index,
            "level": level,
            "title": title,
            "text": body,
            "inlineFallback": True,
        })
    return sections


def _dedupe_section_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    numbered_by_title: dict[str, int] = {}
    numbered_by_number: dict[tuple[int, ...], int] = {}
    previous_number: tuple[int, ...] | None = None
    for block in blocks:
        title = re.sub(r"\s+", " ", str(block.get("title") or "")).strip()
        text = re.sub(r"\s+", " ", str(block.get("text") or "")).strip()
        key = (title.lower(), text[:180].lower())
        if not title or key in seen:
            continue
        numbered_key = title.lower() if re.match(r"^\d+(?:\.\d+)*\.?\s+\S", title) else ""
        number_match = re.match(r"^(?P<number>\d+(?:\.\d+)*\.?)\s+", title)
        number_tuple = _heading_number_tuple(number_match.group("number")) if number_match else ()
        if number_tuple and number_tuple in numbered_by_number:
            existing = deduped[numbered_by_number[number_tuple]]
            existing_title = str(existing.get("title") or "")
            if len(title) < len(existing_title):
                existing["title"] = title
            if len(text) > len(str(existing.get("text") or "")):
                existing["text"] = block.get("text") or ""
            continue
        if number_tuple and not _is_next_numbered_heading(previous_number, number_tuple):
            continue
        if numbered_key and numbered_key in numbered_by_title:
            existing = deduped[numbered_by_title[numbered_key]]
            if len(text) > len(str(existing.get("text") or "")):
                existing["text"] = block.get("text") or ""
            continue
        seen.add(key)
        item = dict(block)
        item["title"] = title
        if numbered_key:
            numbered_by_title[numbered_key] = len(deduped)
            numbered_by_number[number_tuple] = len(deduped)
            previous_number = number_tuple
        deduped.append(item)
    for index, block in enumerate(deduped):
        block["index"] = index
    return deduped


def _truncate_before_inline_sections(text: str, inline_sections: list[dict[str, Any]]) -> str:
    if not text or not inline_sections:
        return text or ""
    first_title = str(inline_sections[0].get("title") or "")
    if not first_title:
        return text
    index = text.find(first_title)
    return text[:index].strip() if index > 0 else text


def normalized_section_blocks(data: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = [b for b in data.get("sectionBlocks") or [] if str(b.get("title") or "").strip()]
    full_text = str(data.get("fullText") or "\n\n".join(str(b.get("text") or "") for b in blocks))
    existing_numbered = [
        b for b in blocks
        if re.match(r"^\d+(?:\.\d+)*\.?\s+\S", str(b.get("title") or "").strip())
    ]
    if len(blocks) >= 5 and len(existing_numbered) < 2:
        return _dedupe_section_blocks(blocks)
    inline_sections = split_inline_numbered_sections(full_text)
    generic_sections = split_generic_numbered_sections(full_text)
    if len(generic_sections) > len(inline_sections):
        inline_sections = generic_sections
    if not inline_sections:
        return _dedupe_section_blocks(blocks)
    existing_titles = {
        re.sub(r"\s+", " ", str(b.get("title") or "")).strip().lower().rstrip(".")
        for b in blocks
    }
    inline_titles = {
        re.sub(r"\s+", " ", str(b.get("title") or "")).strip().lower().rstrip(".")
        for b in inline_sections
    }
    if inline_titles and inline_titles.issubset(existing_titles):
        return _dedupe_section_blocks(blocks)
    if len(existing_numbered) >= len(inline_sections):
        return _dedupe_section_blocks(blocks)
    normalized: list[dict[str, Any]] = []
    inserted = False
    for block in blocks:
        item = dict(block)
        if re.match(r"^(keywords?|abstract)$", str(item.get("title") or "").strip(), flags=re.IGNORECASE):
            item["text"] = _truncate_before_inline_sections(str(item.get("text") or ""), inline_sections)
            normalized.append(item)
            normalized.extend(inline_sections)
            inserted = True
        else:
            normalized.append(item)
    if not inserted:
        normalized.extend(inline_sections)
    return _dedupe_section_blocks(normalized)


def normalize_article_data(data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    blocks = normalized_section_blocks(normalized)
    if blocks:
        normalized["sectionBlocks"] = blocks
        normalized["sections"] = [
            {
                "index": index,
                "level": block.get("level") or "h2",
                "title": block.get("title") or "",
                "id": block.get("id") or "",
            }
            for index, block in enumerate(blocks)
        ]
        structured = clean_block_text("\n\n".join(
            f"{block.get('title') or ''}\n{block.get('text') or ''}".strip()
            for block in blocks
        ))
        if structured:
            normalized["fullText"] = structured
    return normalized


def clean_block_text(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", text or "")).strip()


def read_existing_doi(article_dir: Path) -> str:
    for name in ("metadata.json", "fulltext.json"):
        path = article_dir / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        doi = clean_doi(str(data.get("doi") or data.get("DOI") or ""))
        if doi:
            return doi
    return ""


def remove_existing_for_doi(literature: Path, section: str, doi: str, keep_dir: Path) -> None:
    doi = clean_doi(doi)
    if not doi:
        return
    target_token = doi_token(doi)
    candidate_sections = {canonical_section(section)}
    for section_name in candidate_sections:
        root = literature / section_name
        if not root.exists():
            continue
        for article_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            if article_dir.resolve() == keep_dir.resolve():
                continue
            existing = read_existing_doi(article_dir)
            if existing == doi or target_token in article_dir.name.lower():
                shutil.rmtree(article_dir)


def article_url(doi: str, publisher: str, explicit_url: str = "") -> str:
    if explicit_url:
        return explicit_url
    doi = clean_doi(doi)
    if publisher == "acm":
        return f"https://dl.acm.org/doi/{doi}"
    if publisher == "acs":
        return f"https://pubs.acs.org/doi/{doi}"
    if publisher == "wiley":
        return f"https://onlinelibrary.wiley.com/doi/{doi}"
    if publisher == "springer":
        return f"https://link.springer.com/chapter/{doi}"
    if publisher == "nature":
        article_id = doi.rsplit("/", 1)[-1]
        return f"https://www.nature.com/articles/{article_id}"
    if publisher == "tandfonline":
        return f"https://www.tandfonline.com/doi/full/{doi}"
    if publisher == "elsevier":
        raise ValueError("elsevier_requires_explicit_sciencedirect_url")
    if publisher == "oxford":
        raise ValueError("oxford_requires_explicit_academic_oup_url")
    raise ValueError(f"publisher_requires_explicit_url:{publisher or 'unknown'}")


def publisher_url(url: str, enabled: bool, publisher: str = "") -> str:
    if not enabled:
        return url
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return url
    if publisher in DIRECT_REQUIRED:
        raise ValueError(f"missing_direct_publisher_url_for_host:{parsed.netloc}")
    return url


def read_batch(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if "references" in data:
        return data["references"]
    if "items" in data:
        return data["items"]
    raise ValueError("Batch JSON must be a list or contain references/items")


def section_dir(literature: Path, section: str) -> Path:
    section = canonical_section(section)
    if section.startswith("section-"):
        d = literature / section
        d.mkdir(parents=True, exist_ok=True)
        return d
    number = str(section).zfill(2)
    matches = sorted(literature.glob(f"section-{number}-*"))
    if matches:
        return matches[0]
    d = literature / f"section-{number}-untitled"
    d.mkdir(parents=True, exist_ok=True)
    return d


def article_extraction_script() -> str:
    return """() => {
          const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
          const cleanBlock = s => (s || '').replace(/\\r/g, '').replace(/[ \\t]+\\n/g, '\\n').replace(/\\n[ \\t]+/g, '\\n').replace(/\\n{3,}/g, '\\n\\n').trim();
          const abs = u => {
            try { return u ? new URL(u, location.href).href : ''; } catch { return u || ''; }
          };
          const meta = name => {
            const el = document.querySelector(`meta[name="${name}"], meta[property="${name}"]`);
            return el ? el.getAttribute('content') || '' : '';
          };
          const metas = name => [...document.querySelectorAll(`meta[name="${name}"], meta[property="${name}"]`)].map(x => clean(x.getAttribute('content') || '')).filter(Boolean);
          const badText = /^(reading assistant|outline|cited by\\b.*|metrics\\b.*|figures?\\b.*|tables?\\b.*|recommended articles|related articles|journal of\\b.*|download pdf|sign in|log in|get access|purchase access|advertisement)$/i;
          const stopHeading = title => /^(author information|authors and affiliations|author contributions?|acknowledg|references|bibliography|supporting information|supplementary|data availability|notes|funding|credit authorship|cr?edi?t authorship|compliance with ethics|contributions?|corresponding author|competing interests?|conflict|declaration|publisher'?s note|cite this article|share this article|rights and permissions|about this article|additional information|ethics declarations?|profiles?)\\b/i.test(title);
          const academicHeading = title => /^(abstract|highlights|graphical abstract|keywords?|introduction|background|materials? and methods|methods?|methodology|results?|discussion|conclusions?|conclusion|references|acknowledg|funding|conflict|declaration|supplementary)\\b/i.test(title) || /^\\d+(?:\\.\\d+)*\\.?\\s+\\S/.test(title);
          const noiseSelector = [
            'script','style','noscript','nav','aside','header','footer','form','button',
            '[role="navigation"]','[role="complementary"]','[aria-label*="navigation" i]',
            '[class*="cookie" i]','[class*="login" i]','[class*="signin" i]',
            '[class*="toolbar" i]','[class*="share" i]','[class*="recommend" i]',
            '[class*="related" i]','[class*="advert" i]','[class*="metrics" i]',
            '[class*="cited" i]','[class*="outline" i]'
          ].join(',');
          const rootSelectors = [
            '.c-article-body','[data-test="article-body"]','.articleBody','#article__body',
            '.article_content-left','.article_content-table','.article_content',
            '[data-article-body]','#body','#article','article [class*="body" i]','article','main article','main [class*="article" i]','[role="main"]','main'
          ];
          const roots = rootSelectors.flatMap(sel => Array.from(document.querySelectorAll(sel)));
          const scoreRoot = el => {
            const text = clean(el.innerText || '');
            const headingCount = el.querySelectorAll('h1,h2,h3,h4').length;
            const paraCount = el.querySelectorAll('p').length;
            const classId = `${el.id || ''} ${el.className || ''}`;
            const bodyBonus = /(c-article-body|articleBody|article__body|article_content|article_content-left|article_content-table|article-body|FullText)/i.test(classId) ? 60000 : 0;
            const sidePenalty = /(right|recommended|metrics|cited|footer|abstract|header|license)/i.test(classId) ? 70000 : 0;
            const noiseCount = el.querySelectorAll(noiseSelector + ', [class*="recommended" i], [class*="metrics" i], [class*="cited" i]').length;
            return Math.min(text.length, 300000) + headingCount * 1500 + paraCount * 120 + bodyBonus - sidePenalty - noiseCount * 350;
          };
          const articleRoot = roots.filter(Boolean).sort((a, b) => scoreRoot(b) - scoreRoot(a))[0] || document.body;
          const root = articleRoot.cloneNode(true);
          root.querySelectorAll(noiseSelector).forEach(el => el.remove());
          const cleanHeadingTitle = h => clean(h?.innerText || h?.textContent || '')
            .replace(/Click to copy section link\\s*Section link copied!?/ig, ' ')
            .replace(/Click to copy section link\\s*Section link/ig, ' ')
            .replace(/Section link copied!?/ig, ' ')
            .trim();
          const rawHeadingNodes = [...root.querySelectorAll('h1,h2,h3,h4')].filter(h => {
            const title = cleanHeadingTitle(h);
            const isTitle = h.tagName.toLowerCase() === 'h1' && title === clean(document.querySelector('h1')?.innerText || '');
            const classId = `${h.id || ''} ${h.className || ''} ${h.parentElement?.className || ''}`;
            const figureLike = h.closest('figure, table, [role="figure"], .figure, .table, .Table, .article__inlineFigure, .article__figure, .article-table-content');
            const navLike = /(fig-label|table-label|recommended|related|footer|metric|cited|outline|publication-title|article_header|tags__heading)/i.test(classId);
            const figureOrTableTitle = /^(fig(?:ure)?|table)\\s*\\d+/i.test(title);
            return title && !isTitle && !figureLike && !navLike && !figureOrTableTitle && !badText.test(title) && title.length <= 180;
          });
          const headingNodes = [];
          let inArticleBody = false;
          for (const h of rawHeadingNodes) {
            const title = cleanHeadingTitle(h);
            if (stopHeading(title)) break;
            if (academicHeading(title) || inArticleBody) headingNodes.push(h);
            if (/^(introduction|\\d+(?:\\.\\d+)*\\.?\\s+)/i.test(title)) inArticleBody = true;
          }
          const headings = headingNodes.map((h, i) => ({
            index: i, level: h.tagName.toLowerCase(), title: cleanHeadingTitle(h), id: h.id || ''
          }));
          const rootText = cleanBlock(root.innerText || '');
          let textCursor = 0;
          const textBetweenHeadings = (title, nextTitle) => {
            const startAt = rootText.indexOf(title, textCursor);
            if (startAt < 0) return '';
            const bodyStart = startAt + title.length;
            const endAt = nextTitle ? rootText.indexOf(nextTitle, bodyStart) : -1;
            textCursor = bodyStart;
            return cleanBlock(rootText.slice(bodyStart, endAt > bodyStart ? endAt : undefined));
          };
          const headingRank = el => Number((el.tagName || '').replace(/[^0-9]/g, '')) || 9;
          const blockTextFrom = (start, next) => {
            const textFromRange = () => {
              try {
                const range = document.createRange();
                range.setStartAfter(start);
                if (next) range.setEndBefore(next);
                else range.setEndAfter(root.lastChild || start);
                const frag = range.cloneContents();
                frag.querySelectorAll(noiseSelector + ', figure, table, [role="figure"], .figure, .table, .Table, .article__inlineFigure, .references, [class*="reference" i], [id*="ref" i]').forEach(el => el.remove());
                const text = cleanBlock((frag.textContent || '').split(/\\n+/).filter(line => {
                  const t = clean(line);
                  return t && !badText.test(t) && !stopHeading(t) && !/^(download pdf|view pdf|view article|google scholar|there is no corresponding record)/i.test(t);
                }).join('\\n\\n'));
                if (!badText.test(text)) return text;
              } catch {}
              return '';
            };
            const chunks = [];
            const textNodes = Array.from(root.querySelectorAll('p, li'));
            for (const node of textNodes) {
              const afterStart = !!(start.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING);
              const beforeNext = !next || !!(node.compareDocumentPosition(next) & Node.DOCUMENT_POSITION_FOLLOWING);
              if (!afterStart || !beforeNext) continue;
              if (node.closest(noiseSelector + ', figure, table, [role="figure"], .figure, .table, .Table, .article__inlineFigure, .references, [class*="reference" i], [id*="ref" i]')) continue;
              const t = clean(node.innerText || '');
              if (!t || badText.test(t) || stopHeading(t) || /^(references|bibliography|download:|view pdf|view article|google scholar|there is no corresponding record)/i.test(t)) continue;
              chunks.push(t);
            }
            const rangeText = textFromRange();
            if (rangeText) return rangeText;
            const domText = cleanBlock([...new Set(chunks)].join('\\n\\n'));
            return domText;
          };
          const sectionBlocks = headingNodes.map((h, i) => ({
            index: i,
            level: h.tagName.toLowerCase(),
            title: clean(h.innerText),
            text: blockTextFrom(
              h,
              rawHeadingNodes.slice(rawHeadingNodes.indexOf(h) + 1).find(n => {
                const title = cleanHeadingTitle(n);
                return stopHeading(title) || headingNodes.includes(n);
              }) || null
            ),
            nextTitle: cleanHeadingTitle(rawHeadingNodes.slice(rawHeadingNodes.indexOf(h) + 1).find(n => {
              const title = cleanHeadingTitle(n);
              return stopHeading(title) || headingNodes.includes(n);
            }) || null)
          })).filter(x => x.title && (x.text || !badText.test(x.title)));
          const figureNodes = [...articleRoot.querySelectorAll('figure, [role="figure"], .figure, .article__figure, .article__inlineFigure, .Figure, .c-article-section__figure')];
          const seenFigures = new Set();
          const imagePattern = new RegExp('(\\\\.(png|jpe?g|gif|webp|svg)(\\\\?|$)|/MediaObjects/|/asset/images/|cms/.*/asset/images/)', 'i');
          const imageLike = u => imagePattern.test(u || '');
          const srcsetImageUrl = value => {
            const candidates = String(value || '').split(',').map(s => s.trim()).filter(Boolean);
            for (const candidate of candidates.reverse()) {
              const token = candidate.split(/\\s+/).find(part => imageLike(part));
              if (token) return token;
            }
            return '';
          };
          const cleanFigureCaption = (s, f) => {
            let caption = clean(s || '');
            caption = caption
              .replace(/Open in figure viewer\\s*PowerPoint/ig, ' ')
              .replace(/View in article/ig, ' ')
              .replace(/Full size image/ig, ' ')
              .replace(/Download:?\\s*Download\\s+(high-res|full-size)\\s+image\\s*(\\([^)]*\\))?/ig, ' ')
              .replace(/Download\\s+MS\\s+PowerPoint\\s+Slide/ig, ' ');
            caption = clean(caption);
            if (!caption && f.matches('.article_abstract-img, [class*="abstract-img" i]')) return 'Graphical abstract';
            return caption;
          };
          const figures = figureNodes.map((f) => {
            const img = f.querySelector('img');
            const source = f.querySelector('source[srcset], source[data-srcset]');
            const link = f.querySelector('a[href*="figure"], a[href*="fig"], a[href*="image"], a[href*="gr"], a[href*="/asset/images/"], a[href*="/MediaObjects/"]');
            const hiRes = f.querySelector('a[href*="/large/"], a[href*="download"], a[href*="/MediaObjects/"], a[href*="/asset/images/"]');
            const label = clean(f.querySelector('.figure-label, .caption__label, .Figure-captionLabel, figcaption strong')?.innerText);
            const caption = cleanFigureCaption(f.querySelector('figcaption, .caption, .figure__caption, .Figure-caption, [class*="caption"]')?.innerText || f.innerText, f);
            const srcset = img?.getAttribute('srcset') || img?.getAttribute('data-srcset') || source?.getAttribute('srcset') || source?.getAttribute('data-srcset') || '';
            const srcsetUrl = srcsetImageUrl(srcset);
            const urls = [
              hiRes?.href,
              srcsetUrl,
              img?.currentSrc,
              img?.src,
              img?.getAttribute('data-src'),
              img?.getAttribute('data-original'),
              link?.href,
            ].map(abs).filter(Boolean);
            const image = urls.find(imageLike) || '';
            if (!image || /^table\\s+\\d+/i.test(caption)) return null;
            const decodedImage = decodeURIComponent(image);
            const imageKeyPattern = new RegExp('(?:MediaObjects/|/asset/images/(?:large|medium)/)([^/?#]+)', 'i');
            const imageExtPattern = new RegExp('\\\\.(png|jpe?g|gif|webp|svg)$', 'i');
            const imageQueryPattern = new RegExp('\\\\?.*$', 'i');
            const imageMatch = decodedImage.match(imageKeyPattern);
            const key = imageMatch ? imageMatch[1].replace(imageExtPattern, '') : image.replace(imageQueryPattern, '');
            if (seenFigures.has(key)) return null;
            seenFigures.add(key);
            return { label, caption, image };
          }).filter(Boolean).slice(0, 200).map((x, i) => ({ index: i, ...x }));
          const tableNodes = [...articleRoot.querySelectorAll('table')];
          const tables = tableNodes.slice(0, 100).map((t, i) => {
            const wrapper = t.closest('figure, .table, .Table, .article-table-content, [class*="table" i]') || t;
            const captionNode = wrapper.querySelector('figcaption, .caption, .article-table-caption, [class*="caption" i]');
            return {
              index: i,
              label: clean(wrapper.querySelector('.caption__label, .table-caption__label, .table-label, strong')?.innerText || ''),
              caption: clean(captionNode?.innerText || ''),
              text: clean(t.innerText), html: t.outerHTML,
              rows: Array.from(t.querySelectorAll('tr')).map(tr => Array.from(tr.querySelectorAll('th,td')).map(td => clean(td.innerText || '')))
            };
          });
          const linkedTableKeys = new Set();
          for (const el of Array.from(articleRoot.querySelectorAll('.c-article-table, .article-table'))) {
            if (el.querySelector('table')) continue;
            const href = abs(el.querySelector('a[href*="/tables/"], a[href*="table"]')?.href || '');
            if (!href) continue;
            const caption = clean(el.querySelector('figcaption, .caption, [class*="caption" i]')?.innerText || el.innerText || '').replace(new RegExp('\\\\s*Full size table\\\\s*$', 'i'), '');
            const label = clean((caption.match(new RegExp('^(Table\\\\s+\\\\d+)', 'i')) || [''])[0]);
            const key = href || caption;
            if (!caption || linkedTableKeys.has(key)) continue;
            linkedTableKeys.add(key);
            tables.push({ index: tables.length, label, caption, text: caption, html: '', rows: [], href });
          }
          const refRoots = [
            '#references',
            '#Refs',
            '#Bib1',
            'section[id*="reference" i]',
            'section[id*="bibliography" i]',
            'section[aria-labelledby*="reference" i]',
            'section[aria-labelledby*="bibliography" i]',
            '.references',
            '.References',
            '.bibliography',
            '.Bibliography',
            '.c-article-references',
            '[class*="article-references" i]',
            '[class*="reference-list" i]',
            '[class*="bibliography" i]'
          ].flatMap(sel => {
            try { return Array.from(document.querySelectorAll(sel)); } catch { return []; }
          });
          const referenceTexts = [];
          const seenReferenceTexts = new Set();
          for (const refRoot of refRoots) {
            for (const node of Array.from(refRoot.querySelectorAll('li, p, .c-article-references__item, .c-article-references__text, [class*="reference" i]'))) {
              const text = clean(node.innerText || node.textContent || '');
              if (!text || text.length < 20 || /^(references|bibliography)$/i.test(text)) continue;
              const key = text.slice(0, 180).toLowerCase();
              if (seenReferenceTexts.has(key)) continue;
              seenReferenceTexts.add(key);
              referenceTexts.push(text);
            }
          }
          const references = referenceTexts.length ? referenceTexts : metas('citation_reference');
          const structuredText = cleanBlock(sectionBlocks.map(b => `${b.title}\\n${b.text || ''}`).join('\\n\\n'));
          const structuredAbstract = cleanBlock(sectionBlocks
            .filter(b => /^(abstract|background|aim|aim of review|key scientific concepts)/i.test(b.title || ''))
            .map(b => b.text || '')
            .join('\\n\\n'));
          const abstractFallbackSelectors = [
            '[data-test="abstract"]',
            '[data-test*="abstract" i]',
            '[data-testid="abstract"]',
            '[data-testid*="abstract" i]',
            '#abstract',
            '#abstracts',
            '#Abs1',
            '#Abs1-content',
            '.abstract',
            '.Abstract',
            '.article-abstract',
            '.article__abstract',
            '.article-section__abstract',
            '.c-article-section__abstract',
            '.abstractSection',
            'section[aria-labelledby*="abstract" i]',
            '[class*="abstract" i]',
            '[id*="abstract" i]',
            '[aria-label*="abstract" i]',
            '[aria-labelledby*="abstract" i]'
          ];
          const normalizeAbstract = value => cleanBlock(value)
            .replace(/^(abstract|summary)\\s*/i, '')
            .replace(/^(show more|show less)\\s*/i, '')
            .trim();
          const abstractCandidates = [];
          const seenAbstractCandidates = new Set();
          const abstractTextFromNode = node => {
            if (!node || node.closest('figure, table, [role="figure"], .figure, .table, .Table, [class*="graphical" i], [class*="abstract-img" i]')) return '';
            const clone = node.cloneNode(true);
            clone.querySelectorAll(noiseSelector + ', figure, table, [role="figure"], .figure, .table, .Table, [class*="graphical" i], [class*="abstract-img" i], [class*="keyword" i]').forEach(el => el.remove());
            const text = normalizeAbstract(clone.innerText || clone.textContent || '');
            if (!text || text.length < 40 || badText.test(text) || /^graphical abstract$/i.test(text)) return '';
            return text;
          };
          for (const selector of abstractFallbackSelectors) {
            let nodes = [];
            try { nodes = Array.from(document.querySelectorAll(selector)); } catch {}
            for (const node of nodes) {
              const text = abstractTextFromNode(node);
              if (!text) continue;
              const key = text.slice(0, 240).toLowerCase();
              if (seenAbstractCandidates.has(key)) continue;
              seenAbstractCandidates.add(key);
              abstractCandidates.push({
                selector: selector,
                text: text.slice(0, 3000),
                charCount: text.length
              });
            }
          }
          const selectorAbstract = abstractCandidates.length
            ? abstractCandidates.slice().sort((a, b) => b.charCount - a.charCount)[0].text
            : '';
          const abstractFromBodyText = () => {
            const bodyText = cleanBlock(document.body && document.body.innerText || '');
            if (!bodyText) return '';
            const patterns = [
              /(?:^|\\n|\\b)Abstract\\s+([\\s\\S]{140,3600}?)(?:\\n\\s*(?:Keywords?|Introduction|Graphical Abstract|Download PDF|References|Supporting Information)\\b|$)/i,
              /(?:^|\\n|\\b)Summary\\s+([\\s\\S]{140,3600}?)(?:\\n\\s*(?:Keywords?|Introduction|References|Supporting Information)\\b|$)/i
            ];
            for (const pattern of patterns) {
              const match = bodyText.match(pattern);
              if (!match || !match[1]) continue;
              const text = normalizeAbstract(match[1]);
              if (text && text.length >= 120 && !badText.test(text)) return text;
            }
            const compactText = bodyText.replace(/\\s+/g, ' ').trim();
            const compactPatterns = [
              /(?:^|\\b)Abstract\\s+(.{140,3600}?)(?:\\s+(?:Keywords?|Introduction|Graphical Abstract|Download PDF|References|Supporting Information)\\b|$)/i,
              /(?:^|\\b)Summary\\s+(.{140,3600}?)(?:\\s+(?:Keywords?|Introduction|References|Supporting Information)\\b|$)/i
            ];
            for (const pattern of compactPatterns) {
              const match = compactText.match(pattern);
              if (!match || !match[1]) continue;
              const text = normalizeAbstract(match[1]);
              if (text && text.length >= 120 && !badText.test(text)) return text;
            }
            return '';
          };
          const bodyTextAbstract = abstractFromBodyText();
          if (bodyTextAbstract) {
            const key = bodyTextAbstract.slice(0, 240).toLowerCase();
            if (!seenAbstractCandidates.has(key)) {
              seenAbstractCandidates.add(key);
              abstractCandidates.push({
                selector: 'body_text_abstract_regex',
                text: bodyTextAbstract.slice(0, 3000),
                charCount: bodyTextAbstract.length
              });
            }
          }
          const metaAbstract = meta('citation_abstract');
          const descriptionAbstract = meta('description');
          const finalAbstract = metaAbstract || structuredAbstract || selectorAbstract || bodyTextAbstract || descriptionAbstract || '';
          const abstractExtractionMethod = metaAbstract
            ? 'citation_abstract_meta'
            : structuredAbstract
              ? 'heading_section'
              : selectorAbstract
                ? 'dom_selector'
                : bodyTextAbstract
                  ? 'body_text_abstract_regex'
                  : descriptionAbstract
                  ? 'description_meta'
                  : 'missing';
          return {
            title: clean(document.querySelector('h1')?.innerText) || document.title,
            documentTitle: document.title,
            abstract: finalAbstract,
            abstractExtractionMethod,
            abstractCandidates: abstractCandidates.slice(0, 8),
            doi: meta('citation_doi') || '',
            authors: [...document.querySelectorAll('meta[name="citation_author"]')].map(x => x.content).filter(Boolean),
            journal: meta('citation_journal_title'),
            year: meta('citation_publication_date').slice(0, 4),
            url: location.href,
            capturedAt: new Date().toISOString(),
            fullText: structuredText || cleanBlock(root.innerText || articleRoot.innerText || ''),
            sections: headings,
            sectionBlocks,
            figures,
            tables,
            references,
            keywords: meta('citation_keywords') || meta('keywords') || '',
            supplements: [...document.querySelectorAll('a[href]')].map(a => ({text: clean(a.innerText), href: a.href})).filter(x => /supp|data|appendix/i.test(x.text + ' ' + x.href)).slice(0, 100)
          };
        }"""


async def extract_page(page) -> dict[str, Any]:
    return await page.evaluate(article_extraction_script())


async def enrich_linked_table_pages(context, data: dict[str, Any], settle_ms: int) -> None:
    linked_tables = [
        table
        for table in data.get("tables") or []
        if table.get("href") and not table.get("rows")
    ]
    if not linked_tables:
        return
    page = await context.new_page()
    try:
        for table in linked_tables[:50]:
            href = str(table.get("href") or "")
            try:
                await page.goto(href, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(settle_ms)
                extracted = await page.evaluate(
                    """() => {
                      const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
                      const table = document.querySelector('table');
                      if (!table) return null;
                      const rows = Array.from(table.querySelectorAll('tr')).map(tr =>
                        Array.from(tr.querySelectorAll('th,td')).map(td => clean(td.innerText || ''))
                      ).filter(row => row.some(Boolean));
                      return {
                        title: clean(document.querySelector('h1')?.innerText || document.title || ''),
                        text: clean(table.innerText || ''),
                        html: table.outerHTML || '',
                        rows
                      };
                    }"""
                )
                if not extracted or not extracted.get("rows"):
                    continue
                table["rows"] = extracted.get("rows") or []
                table["text"] = extracted.get("text") or table.get("text") or ""
                table["html"] = extracted.get("html") or ""
                if extracted.get("title") and not table.get("caption"):
                    table["caption"] = extracted["title"]
            except Exception as exc:
                table["fetch_error"] = str(exc)[:240]
    finally:
        await page.close()


def write_markdown(article_dir: Path, data: dict[str, Any]) -> None:
    blocks = normalized_section_blocks(data)
    lines = [
        f"# {data.get('title') or data.get('documentTitle') or 'Untitled'}",
        "",
        f"- DOI: `{data.get('doi') or ''}`",
        f"- URL: {data.get('url') or ''}",
        f"- Captured: {data.get('capturedAt') or ''}",
        "",
        "## Abstract",
        "",
        data.get("abstract") or "",
        "",
    ]
    if blocks:
        lines.extend(["## Structured Full Text", ""])
        for block in blocks:
            title = str(block.get("title") or "").strip()
            text = str(block.get("text") or "").strip()
            if not title:
                continue
            level = str(block.get("level") or "h2").lower()
            prefix = "###" if level in {"h3", "h4"} else "##"
            if title.lower() == "abstract":
                prefix = "##"
            lines.extend([f"{prefix} {title}", ""])
            if text:
                for para in re.split(r"\n{2,}", text):
                    para = para.strip()
                    if para:
                        lines.extend([para, ""])
            else:
                lines.append("")
    else:
        lines.extend(["## Main Text", "", data.get("fullText") or "", ""])
    lines.extend(["## Figures", ""])
    for fig in data.get("figures") or []:
        lines.append(f"- Figure {fig.get('index')}: {fig.get('caption') or fig.get('image') or ''}")
    lines.extend(["", "## Tables", ""])
    for table in data.get("tables") or []:
        lines.append(f"### Table {table.get('index')}")
        lines.append("")
        lines.append(table.get("text") or "")
        lines.append("")
    lines.extend(["## References", ""])
    for ref in data.get("references") or []:
        lines.append(f"- {ref}")
    (article_dir / "fulltext.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_table_exports(article_dir: Path, data: dict[str, Any], write_caption_sidecars: bool = False) -> None:
    tables = data.get("tables") or []
    out = article_dir / "tables"
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("table-*.*"):
        if old.is_file():
            old.unlink()
    (out / "manifest.json").write_text(json.dumps(tables, ensure_ascii=False, indent=2), encoding="utf-8")
    for table in tables:
        idx = table.get("index", 0)
        rows = table.get("rows") or []
        if not rows and table.get("text"):
            rows = [[line.strip()] for line in str(table.get("text") or "").splitlines() if line.strip()]
        with (out / f"table-{idx:02d}.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            csv.writer(handle).writerows(rows or [[""]])
        if write_caption_sidecars and table.get("caption"):
            (out / f"table-{idx:02d}.caption.txt").write_text(table.get("caption") or "", encoding="utf-8")


def write_structure_exports(article_dir: Path, data: dict[str, Any], write_markdown: bool = False) -> None:
    blocks = normalized_section_blocks(data)
    structure = {
        "title": data.get("title") or data.get("documentTitle") or "",
        "doi": data.get("doi") or "",
        "url": data.get("url") or "",
        "capturedAt": data.get("capturedAt") or "",
        "sections": [
            {
                "index": index,
                "level": block.get("level") or "h2",
                "title": block.get("title") or "",
                "id": block.get("id") or "",
                "inlineFallback": bool(block.get("inlineFallback")),
            }
            for index, block in enumerate(blocks)
        ],
        "figures": data.get("figures") or [],
        "tables": data.get("tables") or [],
        "supplements": data.get("supplements") or [],
        "references_count": len(data.get("references") or []),
    }
    (article_dir / "structure.json").write_text(json.dumps(structure, ensure_ascii=False, indent=2), encoding="utf-8")
    if not write_markdown:
        return
    lines = [
        f"# Structure - {structure['title'] or 'Untitled'}",
        "",
        f"- DOI: `{structure['doi']}`",
        f"- URL: {structure['url']}",
        f"- References: {structure['references_count']}",
        "",
        "## Sections",
        "",
    ]
    for section in structure["sections"]:
        lines.append(f"- {section.get('level', '')}: {section.get('title', '')}")
    lines.extend(["", "## Figures", ""])
    for fig in structure["figures"]:
        label = fig.get("label") or f"Figure {fig.get('index')}"
        lines.append(f"- {label}: {fig.get('caption') or fig.get('image') or ''}")
    lines.extend(["", "## Tables", ""])
    for table in structure["tables"]:
        label = table.get("label") or f"Table {table.get('index')}"
        lines.append(f"- {label}: {table.get('caption') or (table.get('text') or '')[:240]}")
    lines.extend(["", "## Supplements", ""])
    for supp in structure["supplements"]:
        lines.append(f"- {supp.get('text') or ''}: {supp.get('href') or ''}")
    (article_dir / "structure.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_reference_exports(article_dir: Path, data: dict[str, Any]) -> None:
    references = data.get("references") or []
    (article_dir / "references.json").write_text(json.dumps(references, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# References", ""]
    if references:
        for index, ref in enumerate(references, start=1):
            lines.append(f"{index}. {ref}")
    else:
        lines.append("None detected.")
    (article_dir / "references.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_figure_manifest(article_dir: Path, data: dict[str, Any]) -> None:
    figures = data.get("figures") or []
    out = article_dir / "figures"
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(figures, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Figures", ""]
    if figures:
        for fig in figures:
            index = int(fig.get("index") or 0)
            file_note = f" [{fig.get('local_file')}]" if fig.get("local_file") else ""
            lines.append(f"- Figure {index}{file_note}: {fig.get('caption') or fig.get('image') or ''}")
    else:
        lines.append("None detected.")
    (out / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _figure_ext_from_content(content_type: str, image_url: str) -> str:
    content_type = (content_type or "").lower()
    if "png" in content_type:
        return ".png"
    if "gif" in content_type:
        return ".gif"
    if "webp" in content_type:
        return ".webp"
    if "svg" in content_type:
        return ".svg"
    if "jpeg" in content_type or "jpg" in content_type:
        return ".jpg"
    path = urlparse(image_url or "").path.lower()
    for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"]:
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"


def write_figure_exports_opencli(session: str, article_dir: Path, data: dict[str, Any]) -> dict[str, int]:
    """Download figure images through the current OpenCLI browser context.

    OpenCLI captures run outside Playwright's request context, so image URLs that
    require Publisher cookies must be fetched from the browser page itself.
    """
    figures = data.get("figures") or []
    out = article_dir / "figures"
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("figure-*.*"):
        if old.is_file() and old.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
            old.unlink()
    stats = {"attempted": 0, "downloaded": 0, "failed": 0}
    for fig in figures:
        idx = int(fig.get("index") or 0)
        image = str(fig.get("image") or "")
        if not image.startswith(("http://", "https://")):
            fig["download_status"] = "no_http_image_url"
            continue
        stats["attempted"] += 1
        script = f"""(async () => {{
          const url = {json.dumps(image)};
          try {{
            const res = await fetch(url, {{credentials: 'include'}});
            const buf = await res.arrayBuffer();
            let binary = '';
            const bytes = new Uint8Array(buf);
            const chunkSize = 0x8000;
            for (let i = 0; i < bytes.length; i += chunkSize) {{
              let chunk = bytes.subarray(i, i + chunkSize);
              binary += String.fromCharCode.apply(null, chunk);
            }}
            return JSON.stringify({{
              ok: res.ok,
              status: res.status,
              contentType: res.headers.get('content-type') || '',
              length: bytes.length,
              bodyBase64: btoa(binary)
            }});
          }} catch (err) {{
            return JSON.stringify({{ok: false, status: 0, error: String(err && err.message || err)}});
          }}
        }})()"""
        try:
            payload = opencli_browser.eval_json(session, script, timeout=120)
            if not isinstance(payload, dict) or not payload.get("ok") or not payload.get("bodyBase64"):
                fig["download_status"] = f"failed:{payload.get('status') if isinstance(payload, dict) else 'invalid'}"
                fig["download_error"] = str(payload.get("error") or "")[:300] if isinstance(payload, dict) else "invalid_payload"
                stats["failed"] += 1
                continue
            ext = _figure_ext_from_content(str(payload.get("contentType") or ""), image)
            filename = f"figure-{idx:02d}{ext}"
            body = base64.b64decode(str(payload.get("bodyBase64") or ""))
            (out / filename).write_bytes(body)
            fig["local_file"] = filename
            fig["download_status"] = "downloaded"
            fig["download_content_type"] = str(payload.get("contentType") or "")
            fig["download_bytes"] = len(body)
            stats["downloaded"] += 1
        except Exception as exc:
            fig["download_status"] = "failed:exception"
            fig["download_error"] = f"{type(exc).__name__}: {exc}"[:300]
            stats["failed"] += 1
    write_figure_manifest(article_dir, data)
    (article_dir / "fulltext.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    write_structure_exports(article_dir, data)
    return stats


def write_article_artifacts(
    article_dir: Path,
    data: dict[str, Any],
    *,
    metadata: dict[str, Any] | None = None,
) -> None:
    data = normalize_article_data(data)
    article_dir.mkdir(parents=True, exist_ok=True)
    merged_metadata = {
        **(metadata or {}),
        "title": data.get("title") or data.get("documentTitle") or (metadata or {}).get("title") or "",
        "doi": data.get("doi") or (metadata or {}).get("doi") or "",
        "url": data.get("url") or (metadata or {}).get("url") or "",
        "authors": data.get("authors") or (metadata or {}).get("authors") or [],
        "journal": data.get("journal") or (metadata or {}).get("journal") or "",
        "year": data.get("year") or (metadata or {}).get("year") or "",
    }
    merged_metadata.setdefault("organized_at", datetime.now().isoformat(timespec="seconds"))
    (article_dir / "metadata.json").write_text(json.dumps(merged_metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    (article_dir / "fulltext.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(article_dir, data)
    (article_dir / "captured-fulltext.md").write_text((article_dir / "fulltext.md").read_text(encoding="utf-8"), encoding="utf-8")
    write_structure_exports(article_dir, data)
    write_table_exports(article_dir, data)
    write_figure_manifest(article_dir, data)
    write_reference_exports(article_dir, data)


async def write_figure_exports(
    context,
    article_dir: Path,
    data: dict[str, Any],
    write_sidecars: bool = False,
) -> None:
    figures = data.get("figures") or []
    out = article_dir / "figures"
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(figures, ensure_ascii=False, indent=2), encoding="utf-8")
    if not figures:
        return
    for old in out.glob("figure-*.*"):
        if old.is_file():
            old.unlink()
    for fig in figures:
        idx = int(fig.get("index") or 0)
        caption = fig.get("caption") or ""
        image = fig.get("image") or ""
        if write_sidecars:
            (out / f"figure-{idx:02d}.json").write_text(json.dumps(fig, ensure_ascii=False, indent=2), encoding="utf-8")
        if write_sidecars and caption:
            (out / f"figure-{idx:02d}.caption.txt").write_text(caption, encoding="utf-8")
        if not image or not str(image).startswith(("http://", "https://")):
            continue
        try:
            response = await context.request.get(image, timeout=30000)
            if not response.ok:
                continue
            content_type = (response.headers.get("content-type") or "").lower()
            ext = ".jpg"
            if "png" in content_type:
                ext = ".png"
            elif "gif" in content_type:
                ext = ".gif"
            elif "webp" in content_type:
                ext = ".webp"
            elif "svg" in content_type:
                ext = ".svg"
            (out / f"figure-{idx:02d}{ext}").write_bytes(await response.body())
        except Exception:
            continue


async def capture_one(
    context,
    item: dict[str, Any],
    literature: Path,
    publisher: bool,
    strict: bool,
    settle_ms: int,
    scroll_rounds: int,
    scroll_wait_ms: int,
    replace_existing: bool,
    write_snapshot_html: bool = False,
) -> dict[str, Any]:
    doi = clean_doi(str(item.get("doi") or item.get("DOI") or ""))
    publisher = str(item.get("publisher") or "").strip().lower()
    section = canonical_section(str(item.get("section") or item.get("section_id") or "00"))
    title = str(item.get("title") or item.get("unstructured") or doi or "untitled")
    if strict and publisher not in SUPPORTED:
        return {"status": "manual_pending", "reason": "unsupported_publisher", "doi": doi, "publisher": publisher, "title": title}
    try:
        base_url = article_url(doi, publisher, str(item.get("url") or ""))
        url = publisher_url(base_url, publisher and publisher not in OPEN_FIRST, publisher)
    except Exception as exc:
        return {
            "status": "manual_pending",
            "reason": str(exc),
            "doi": doi,
            "publisher": publisher,
            "title": title,
            "url": str(item.get("url") or ""),
        }
    provisional_name = f"{token(title)}__{doi_token(doi)}" if doi else token(title)
    provisional_dir = section_dir(literature, section) / provisional_name
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(settle_ms)
        for _ in range(scroll_rounds):
            await page.mouse.wheel(0, 1800)
            await page.wait_for_timeout(scroll_wait_ms)
        await page.wait_for_timeout(settle_ms)
        data = await extract_page(page)
        if doi and not data.get("doi"):
            data["doi"] = doi
        if not data.get("authors"):
            data["authors"] = infer_authors_from_text(data.get("fullText") or "", data.get("title") or title)
        if item.get("title"):
            data["title"] = str(item.get("title"))
        if item.get("authors"):
            data["authors"] = item.get("authors")
        if item.get("journal"):
            data["journal"] = str(item.get("journal"))
        if item.get("year"):
            data["year"] = str(item.get("year"))
        if not data.get("year"):
            data["year"] = infer_year_from_text(data.get("fullText") or "")
        await enrich_linked_table_pages(context, data, settle_ms)
        data["inputPublisher"] = publisher
        data["inputSection"] = section
        article_name = article_folder_name(data, title, data.get("doi") or doi)
        article_dir = section_dir(literature, section) / article_name
        if replace_existing:
            remove_existing_for_doi(literature, section, data.get("doi") or doi, article_dir)
        article_dir.mkdir(parents=True, exist_ok=True)
        replace_dir(provisional_dir, article_dir)
        (article_dir / "fulltext.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        if write_snapshot_html:
            (article_dir / "snapshot.html").write_text(await page.content(), encoding="utf-8")
        (article_dir / "metadata.json").write_text(json.dumps({
            "section": section,
            "source_publisher_folder": publisher,
            "title": data.get("title") or title,
            "doi": data.get("doi") or doi,
            "url": data.get("url") or url,
            "authors": data.get("authors") or [],
            "journal": data.get("journal") or "",
            "year": data.get("year") or "",
            "organized_at": datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        write_markdown(article_dir, data)
        write_structure_exports(article_dir, data)
        write_table_exports(article_dir, data)
        await write_figure_exports(context, article_dir, data)
        return {"status": "captured", "doi": doi, "publisher": publisher, "title": title, "article_dir": str(article_dir), "chars": len(data.get("fullText") or "")}
    except Exception as exc:
        return {"status": "manual_pending", "reason": type(exc).__name__, "doi": doi, "publisher": publisher, "title": title, "url": url, "error": str(exc)[:500]}
    finally:
        await page.close()


def capture_one_opencli(item: dict[str, Any], literature: Path, args: argparse.Namespace) -> dict[str, Any]:
    doi = clean_doi(str(item.get("doi") or item.get("DOI") or ""))
    publisher = str(item.get("publisher") or "").strip().lower()
    section = canonical_section(str(item.get("section") or item.get("section_id") or "00"))
    title = str(item.get("title") or item.get("unstructured") or doi or "untitled")
    if args.strict_publishers and publisher not in SUPPORTED:
        return {"status": "manual_pending", "reason": "unsupported_publisher", "doi": doi, "publisher": publisher, "title": title}
    try:
        base_url = article_url(doi, publisher, str(item.get("url") or ""))
        url = publisher_url(base_url, args.publisher and publisher not in OPEN_FIRST, publisher)
    except Exception as exc:
        return {"status": "manual_pending", "reason": str(exc), "doi": doi, "publisher": publisher, "title": title, "url": str(item.get("url") or "")}
    try:
        reached_url = opencli_browser.open_url_allow_redirect(args.opencli_session, url, timeout=90)
        opencli_browser.settle_article_page(
            args.opencli_session,
            initial_wait_ms=int(getattr(args, "article_open_wait_ms", None) or getattr(args, "settle_ms", 5000) or 5000),
            scroll_rounds=int(getattr(args, "scroll_rounds", 0) or 0),
            scroll_wait_ms=int(getattr(args, "scroll_wait_ms", 1000) or 1000),
        )
        data = opencli_browser.eval_json(args.opencli_session, article_extraction_script(), timeout=120)
        if not isinstance(data, dict):
            raise RuntimeError("opencli_invalid_article_payload")
        if doi and not data.get("doi"):
            data["doi"] = doi
        if reached_url and not data.get("url"):
            data["url"] = reached_url
        if not data.get("authors"):
            data["authors"] = infer_authors_from_text(data.get("fullText") or "", data.get("title") or title)
        for field in ["title", "authors", "journal", "year"]:
            if item.get(field):
                data[field] = item[field]
        if not data.get("year"):
            data["year"] = infer_year_from_text(data.get("fullText") or "")
        data["inputPublisher"] = publisher
        data["inputSection"] = section
        article_name = article_folder_name(data, title, data.get("doi") or doi)
        article_dir = section_dir(literature, section) / article_name
        if args.replace_existing:
            remove_existing_for_doi(literature, section, data.get("doi") or doi, article_dir)
        metadata = {
            "section": section,
            "source_publisher_folder": publisher,
            "organized_at": datetime.now().isoformat(timespec="seconds"),
        }
        write_article_artifacts(article_dir, data, metadata=metadata)
        return {"status": "captured", "doi": data.get("doi") or doi, "publisher": publisher, "title": data.get("title") or title, "article_dir": str(article_dir), "chars": len(data.get("fullText") or "")}
    except Exception as exc:
        return {"status": "manual_pending", "reason": type(exc).__name__, "doi": doi, "publisher": publisher, "title": title, "url": url, "error": str(exc)[:500]}


def run(args: argparse.Namespace) -> list[dict[str, Any]]:
    batch = read_batch(args.batch.resolve())
    literature = args.literature_root.resolve()
    literature.mkdir(parents=True, exist_ok=True)
    rows = [capture_one_opencli(item, literature, args) for item in batch]
    try:
        opencli_browser.close(args.opencli_session)
    except Exception:
        pass
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("batch", type=Path)
    parser.add_argument("--literature-root", type=Path, required=True)
    parser.add_argument("--opencli-session", default="lit-capture")
    parser.add_argument("--publisher", action="store_true")
    parser.add_argument("--strict-publishers", action="store_true", default=True)
    parser.add_argument("--settle-ms", type=int, default=5000)
    parser.add_argument("--article-open-wait-ms", type=int, default=5000)
    parser.add_argument("--scroll-rounds", type=int, default=8)
    parser.add_argument("--scroll-wait-ms", type=int, default=1000)
    parser.add_argument("--replace-existing", action="store_true")
    args = parser.parse_args()
    rows = run(args)
    admin = args.literature_root.resolve() / "_admin"
    admin.mkdir(exist_ok=True)
    report = admin / f"capture-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    report.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"captured={sum(1 for r in rows if r['status'] == 'captured')}")
    print(f"manual_pending={sum(1 for r in rows if r['status'] != 'captured')}")
    print(f"report={report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
