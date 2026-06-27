#!/usr/bin/env python3
"""publisher-authenticated publisher discovery helpers for literature capture."""

from __future__ import annotations

import json
import os
import re
import ssl
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

Page = Any


SCIENCEDIRECT_HOST = "www.sciencedirect.com"
ALLOWED_PUBLISHER_HOSTS = {
    "www.sciencedirect.com",
    "sciencedirect.com",
    "pubs.acs.org",
    "onlinelibrary.wiley.com",
    "link.springer.com",
}
PUBLISHER_QUERY_MAX_WORDS = 6


def trusted_https_context() -> ssl.SSLContext | None:
    try:
        import certifi  # type: ignore
    except Exception:
        return None
    try:
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


@dataclass
class QueryPlan:
    round_index: int
    query_family: str
    claim_subquestion: str
    queries: list[str]
    subquestion_id: str = ""
    subquestion_slug: str = ""
    group_slug: str = "general"
    group_title: str = "General"
    concept_groups: list[dict[str, Any]] | None = None
    boolean_query: str = ""
    publisher_queries: dict[str, str] | None = None
    query_provenance: list[dict[str, Any]] | None = None


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def unique_ordered(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        item = normalize_ws(item)
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _dedup_value(value: Any) -> str:
    return normalize_ws(str(value or "")).lower()


def _normalized_doi(value: Any) -> str:
    text = _dedup_value(value)
    if not text:
        return ""
    if text.startswith("doi:"):
        text = text[4:].strip()
    parsed = urlparse(text)
    if parsed.netloc.lower() in {"doi.org", "dx.doi.org"}:
        text = parsed.path.lstrip("/")
    match = re.search(r"\b(10\.\d{4,9}/[^\s\"'<>?#]+)", text, flags=re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).rstrip(".,;")


def _normalized_url(value: Any) -> str:
    text = normalize_ws(str(value or ""))
    if not text:
        return ""
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        return text.lower().rstrip("/")
    path = parsed.path.lower().rstrip("/")
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", ""))


def _candidate_dedup_keys(row: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for value in [row.get("doi"), row.get("article_url"), row.get("url"), row.get("href")]:
        doi = _normalized_doi(value)
        if doi:
            keys.append(f"doi:{doi}")
    for value in [row.get("article_url"), row.get("url"), row.get("href")]:
        url = _normalized_url(value)
        if url:
            keys.append(f"url:{url}")
    title = _dedup_value(row.get("title"))
    if title:
        keys.append(f"title:{title}")
    return unique_ordered(keys)


def _history_kind(row: dict[str, Any]) -> str:
    explicit = _dedup_value(row.get("_history_kind") or row.get("history_kind"))
    if explicit in {"captured", "seen"}:
        return explicit
    duplicate_status = _dedup_value(row.get("duplicate_status"))
    if duplicate_status == "seen_before":
        return "seen"
    if duplicate_status == "captured_before":
        return "captured"
    if row.get("stage"):
        return "seen"
    status = _dedup_value(row.get("status"))
    if status:
        return "captured" if status == "captured" else "seen"
    return "captured"


def _history_priority(kind: str) -> int:
    if kind == "captured":
        return 2
    if kind == "seen":
        return 1
    return 0


def mark_duplicate_candidates(candidates: list[dict[str, Any]], previous_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    previous_keys: dict[str, str] = {}
    for row in previous_rows:
        if isinstance(row, dict):
            kind = _history_kind(row)
            for key in _candidate_dedup_keys(row):
                if _history_priority(kind) > _history_priority(previous_keys.get(key, "")):
                    previous_keys[key] = kind

    marked: list[dict[str, Any]] = []
    for candidate in candidates:
        row = dict(candidate)
        duplicate_of = next((key for key in _candidate_dedup_keys(row) if key in previous_keys), "")
        if duplicate_of:
            row["duplicate_of"] = duplicate_of
            if previous_keys[duplicate_of] == "captured":
                row["duplicate_status"] = "captured_before"
                row["screening_priority"] = "skip_duplicate"
            else:
                row["duplicate_status"] = "seen_before"
                row["screening_priority"] = "review_low_priority"
        else:
            if not _dedup_value(row.get("duplicate_status")):
                row["duplicate_status"] = "new"
            if not _dedup_value(row.get("duplicate_of")):
                row["duplicate_of"] = ""
            if not _dedup_value(row.get("screening_priority")):
                row["screening_priority"] = "normal"
        marked.append(row)
    return marked


def safe_slug(text: str, fallback: str = "subquestion", limit: int = 70) -> str:
    text = normalize_ws(text)
    text = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", text, flags=re.UNICODE)
    text = text.strip("._")
    return (text[:limit].strip("._") or fallback)


def assign_subquestion_ids(plans: list[QueryPlan]) -> list[QueryPlan]:
    for idx, plan in enumerate(plans, start=1):
        slug = plan.subquestion_slug or safe_slug(plan.query_family or plan.claim_subquestion, f"subquestion-{idx:02d}")
        plan.subquestion_slug = slug
        plan.subquestion_id = plan.subquestion_id or f"{idx:02d}_{slug}"
        plan.group_slug = plan.group_slug or "general"
        plan.group_title = plan.group_title or plan.group_slug.replace("-", " ").title()
    return plans


def extract_year(text: str) -> str:
    years = re.findall(r"\b(20[0-9]\d|19\d{2})\b", text or "")
    return years[0] if years else ""


def is_direct_sciencedirect_search(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.netloc.lower() == SCIENCEDIRECT_HOST and parsed.path.lower().rstrip("/") == "/search"


def is_direct_sciencedirect_article(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.netloc.lower() == SCIENCEDIRECT_HOST and bool(
        re.search(r"/science/article/pii/[^/]+/?$", parsed.path.lower())
    )


def ensure_direct(url: str) -> None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_PUBLISHER_HOSTS):
        raise ValueError(f"not_supported_publisher_url:{url}")
    if "doi.org" in parsed.netloc or "doi.org" in parsed.path:
        raise ValueError(f"doi_resolver_route_blocked:{url}")


def ensure_allowed_search_url(url: str) -> None:
    parsed = urlparse(url)
    if "doi.org" in parsed.netloc or "doi.org" in parsed.path:
        raise ValueError(f"doi_resolver_route_blocked:{url}")
    if is_direct_sciencedirect_search(url):
        return
    ensure_direct(url)


def ensure_allowed_article_url(url: str) -> None:
    parsed = urlparse(url)
    if "doi.org" in parsed.netloc or "doi.org" in parsed.path:
        raise ValueError(f"doi_resolver_route_blocked:{url}")
    if is_direct_sciencedirect_article(url):
        return
    ensure_direct(url)


def publisher_key(url: str) -> str:
    """Infer a stable generic publisher bucket from a direct publisher URL."""
    parsed = urlparse(url)
    label = parsed.netloc.lower().split(".")[-2] if "." in parsed.netloc else parsed.netloc.lower()
    label = re.sub(r"[^a-z0-9_-]+", "", label.lower())
    return label or "publisher"


def infer_publisher(url: str) -> str:
    parsed = urlparse(url)
    haystack = f"{parsed.netloc}{parsed.path}".lower()
    if "sciencedirect" in haystack:
        return "elsevier"
    if "acs" in haystack or "pubs-acs" in haystack:
        return "acs"
    if "wiley" in haystack or "onlinelibrary-wiley" in haystack:
        return "wiley"
    if "springer" in haystack or "springerlink" in haystack or "/com/springer/link/" in haystack:
        return "springer"
    generic_key = publisher_key(url)
    if generic_key:
        return generic_key
    return "unknown"


def abstract_source_for_publisher(publisher: str) -> str:
    key = normalize_ws(publisher).lower()
    if key in {"elsevier", "sciencedirect"}:
        return "sciencedirect_search_result_abstract"
    if key == "acs":
        return "acs_search_result_abstract"
    if key == "wiley":
        return "wiley_search_result_abstract"
    if key == "springer":
        return "springer_search_result_snippet"
    return "publisher_search_page"


def update_query(url: str, updates: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key, value in updates.items():
        query[key] = [value]
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query, doseq=True), parsed.fragment))


def page_url_for(search_url: str, publisher: str, page_num: int) -> str:
    if page_num == 1:
        return search_url
    if publisher == "elsevier":
        return update_query(search_url, {"offset": str((page_num - 1) * 25)})
    if publisher in {"acs", "wiley"}:
        return update_query(search_url, {"startPage": str(page_num - 1), "pageSize": "20"})
    if publisher == "springer":
        return update_query(search_url, {"page": str(page_num)})
    return search_url


def article_href_allowed(href: str, publisher: str) -> bool:
    low = href.lower()
    if publisher == "elsevier" and is_direct_sciencedirect_article(href):
        return True
    if any(token in low for token in ["/pdf", "pdfft", "/epdf", "download", "pdfdownload", "viewpdf"]):
        return False
    if publisher == "elsevier":
        return bool(re.search(r"/science/article/pii/[^/]+/?$", urlparse(href).path.lower()))
    if publisher == "acs":
        return "/doi/" in low and "/action/" not in low and "/toc/" not in low
    if publisher == "wiley":
        return "/doi/" in low and "/doi/book/" not in low and "/action/" not in low and "/toc/" not in low
    if publisher == "springer":
        return bool(re.search(r"/(?:article|chapter|protocol)/10\.", low)) and "/search" not in low
    blocked_tokens = [
        "/search", "/action/", "/toc/", "/login", "/sign", "/account", "/user",
        "/about", "/help", "/journal", "/issue", "/browse", "/topic",
    ]
    if any(token in low for token in blocked_tokens):
        return False
    return bool(
        re.search(r"10\.\d{4,9}", low)
        or any(token in low for token in ["/doi/", "/article", "/document/", "/abs/", "/abstract/", "/content/"])
    )


def core_terms(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}|[\u4e00-\u9fff]{2,}", text or "")
    stop = {
        "the", "and", "for", "with", "from", "into", "about", "what", "how",
        "which", "this", "that", "major", "paper", "papers", "review", "reviews", "article", "study", "research", "existing", "resources",
        "resource", "has", "have", "had", "not", "are", "was", "were", "been",
        "being", "through", "using", "used", "based", "large-scale", "should",
        "construct", "constructed", "build", "built",
    }
    terms: list[str] = []
    for word in words:
        low = word.lower()
        if low in stop or len(low) < 3:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]+", word) and any(token in word for token in ["哪些", "什么", "方法", "新方法", "有哪些"]):
            continue
        terms.append(word)
    return unique_ordered(terms)[:12]


def phrase_terms(text: str) -> list[str]:
    low = (text or "").lower()
    phrases: list[str] = []
    known_phrases = [
        "clinical decision support",
        "decision support",
        "knowledge graph",
        "retrieval augmented generation",
        "large language model",
        "semantic web",
        "machine learning",
        "deep learning",
    ]
    for phrase in known_phrases:
        if phrase in low:
            phrases.append(phrase)
    words = core_terms(text)
    stop = {
        "should", "constructed", "construct", "build", "built", "using", "used",
        "improve", "improves", "improving", "enhance", "enhances", "enhancing",
    }
    filtered = [word for word in words if word.lower() not in stop]
    known_lows = {phrase.lower() for phrase in phrases}
    for size in (3, 2):
        for index in range(0, max(0, len(filtered) - size + 1)):
            parts = filtered[index:index + size]
            if len({part.lower() for part in parts}) != len(parts):
                continue
            phrase = " ".join(parts)
            low_phrase = phrase.lower()
            if any(low_phrase in known or known in low_phrase for known in known_lows):
                continue
            if low_phrase not in known_lows:
                phrases.append(phrase)
                known_lows.add(low_phrase)
            if len(phrases) >= 6:
                break
        if len(phrases) >= 4:
            break
    return unique_ordered(phrases)[:6]


def domain_query_expansions(claim: str) -> list[str]:
    low = claim.lower()
    expansions: list[str] = []
    if "rag" in low or "retrieval augmented generation" in low or "retrieval-augmented generation" in low:
        expansions.extend([
            "retrieval augmented generation",
            "RAG",
        ])
        if any(token in claim for token in ["新", "新方法", "哪些", "方法", "进展"]):
            expansions.extend([
                "new methods",
                "recent advances",
            ])
    return unique_ordered(expansions)


def claim_topic_phrase(claim: str) -> str:
    """Return a compact topic phrase from the claim without query-family focus words."""
    phrases = [
        phrase for phrase in phrase_terms(claim)
        if not any(
            re.search(rf"\b{token}\b", phrase.lower())
            for token in ["knowledge", "graph", "ontology", "semantic", "web", "method", "model", "application"]
        )
    ]
    if phrases:
        return phrases[0]
    stop = {
        "how", "should", "what", "which", "when", "where", "why", "does",
        "used", "using", "use", "study", "studied", "construct", "constructed",
        "construction", "build", "built", "establish", "established",
        "knowledge", "graph", "graphs", "ontology", "ontologies", "semantic", "web",
    }
    words = [word for word in core_terms(claim) if word.lower() not in stop]
    return normalize_ws(" ".join(words[:3]))


def claim_search_topic_phrase(claim: str) -> str:
    """Build the compact topic phrase used in publisher queries."""
    domain = claim_topic_phrase(claim)
    framework_phrases = [
        phrase for phrase in phrase_terms(claim)
        if phrase.lower() in {
            "knowledge graph",
            "semantic web",
            "retrieval augmented generation",
            "large language model",
            "machine learning",
            "deep learning",
        }
    ]
    if framework_phrases and domain:
        return normalize_ws(f"{domain} {framework_phrases[0]}")
    return domain or (framework_phrases[0] if framework_phrases else "")


def claim_domain_terms(claim: str) -> list[str]:
    framework = {
        "knowledge", "graph", "graphs", "ontology", "ontologies", "semantic",
        "web", "retrieval", "augmented", "generation", "large", "language",
        "model", "models", "machine", "learning", "deep",
    }
    generic = {
        "construct", "constructed", "construction", "build", "built", "should",
        "improve", "application", "applications", "used", "using",
    }
    return [
        term for term in core_terms(claim)
        if term.lower() not in framework and term.lower() not in generic
    ][:5]


def claim_framework_terms(claim: str) -> list[str]:
    terms: list[str] = []
    low = claim.lower()
    phrase_terms_by_framework = {
        "knowledge graph": ["graph"],
        "semantic web": ["semantic"],
        "ontology": ["ontology"],
        "retrieval augmented generation": ["retrieval", "generation"],
        "large language model": ["language", "model"],
        "machine learning": ["learning"],
        "deep learning": ["learning"],
    }
    for phrase, phrase_terms_ in phrase_terms_by_framework.items():
        if phrase in low:
            terms.extend(phrase_terms_)
    return unique_ordered(terms)


def openalex_work_matches_claim(joined_text: str, domain_terms: list[str], framework_terms: list[str]) -> bool:
    joined = joined_text.lower()
    if domain_terms and not any(term.lower() in joined for term in domain_terms):
        return False
    if not framework_terms:
        return True
    high_signal_terms = {
        "graph", "ontology", "semantic", "database", "dataset",
        "benchmark", "corpus", "embedding", "schema", "model", "method",
        "algorithm", "workflow", "pipeline",
    }
    return any(term.lower() in joined for term in framework_terms) or any(term in joined for term in high_signal_terms)


def openalex_grounding_concept_hints(claim: str) -> list[dict[str, Any]]:
    """Build broad, non-final terminology blocks for agent OpenAlex grounding."""
    topic = claim_topic_phrase(claim) or normalize_ws(" ".join(claim_domain_terms(claim)[:3]))
    low = claim.lower()

    def with_topic(terms: list[str]) -> list[str]:
        out: list[str] = []
        for term in terms:
            if "{topic}" in term:
                if topic:
                    out.append(term.format(topic=topic))
            else:
                out.append(term)
        return unique_ordered(out)

    resource_terms = with_topic([
        "{topic} database",
        "{topic} dataset",
        "{topic} resource",
        "domain database",
        "knowledge graph data source",
    ])
    schema_terms = with_topic([
        "{topic} ontology",
        "{topic} schema",
        "{topic} knowledge graph",
        "entity relation schema",
        "semantic annotation",
    ])
    method_terms = [
        "graph construction",
        "relation extraction",
        "graph embedding",
        "representation learning",
        "link prediction",
        "graph completion",
    ]
    evaluation_terms = with_topic([
        "{topic} recommendation",
        "{topic} clustering",
        "{topic} benchmark",
        "graph evaluation",
        "validation metric",
    ])
    if "food" in low or "flavor" in low or "flavour" in low:
        resource_terms = unique_ordered([
            "flavor molecule",
            "flavor compound",
            "flavor molecule database",
            "ingredient",
            "natural source",
            "sensory response",
            "physicochemical property",
            *resource_terms,
        ])
        schema_terms = unique_ordered([
            "food chemical graph",
            "recipe ingredient graph",
            "food ontology",
            "chemical food relation",
            *schema_terms,
        ])
        method_terms = unique_ordered([
            "food chemical graph embedding",
            "recipe relation extraction",
            "metapath2vec",
            *method_terms,
        ])
        evaluation_terms = unique_ordered([
            "food pairing",
            "food pairing recommendation",
            "food representation",
            "food clustering",
            *evaluation_terms,
        ])
    return [
        {
            "label": "resource vocabulary",
            "terms": resource_terms[:10],
            "purpose": "Broaden metadata grounding around data sources, entities, attributes, and reusable resources.",
        },
        {
            "label": "schema vocabulary",
            "terms": schema_terms[:10],
            "purpose": "Expose ontology, schema, entity, relation, and semantic-model terminology for the agent.",
        },
        {
            "label": "method vocabulary",
            "terms": method_terms[:10],
            "purpose": "Expose construction, extraction, embedding, completion, and representation-learning terminology.",
        },
        {
            "label": "application and evaluation vocabulary",
            "terms": evaluation_terms[:10],
            "purpose": "Expose downstream task, validation, benchmark, and evaluation terminology.",
        },
    ]


def openalex_grounding_probe_queries(claim: str, limit: int = 14) -> list[str]:
    """Return broad OpenAlex metadata probes; these are not final publisher queries."""
    core = core_terms(claim)
    low = claim.lower()
    probes = [
        claim,
        " ".join(core[:6]),
        " ".join(core[:4]),
    ]
    if "food" in low or "flavor" in low or "flavour" in low:
        probes.extend([
            "flavor molecule database",
            "food chemical graph",
            "food pairing recommendation",
            "recipe ingredient graph",
        ])
    for hint in openalex_grounding_concept_hints(claim):
        for term in hint.get("terms") or []:
            text = normalize_ws(str(term))
            if not text or text.lower() in {"domain database"}:
                continue
            probes.append(text)
            if len(probes) >= limit:
                return unique_ordered(probes)[:limit]
    return unique_ordered(probes)[:limit]


def ranked_query_terms(terms: list[str], claim: str, limit: int) -> list[str]:
    """Keep claim-bearing terms ahead of generic review vocabulary."""
    low_claim = claim.lower()
    claim_words = {word.lower() for word in core_terms(claim)}
    generic_penalty = {
        "data", "method", "model", "models", "large-scale", "application",
        "applications", "analysis", "evidence", "validation", "review",
    }

    def score(term: str) -> tuple[int, int, int]:
        low = term.lower()
        if low in claim_words or low in low_claim:
            priority = 0
        elif low in generic_penalty:
            priority = 2
        else:
            priority = 1
        in_claim = 0 if low in low_claim else 1
        return (priority, in_claim, terms.index(term))

    ranked = sorted(unique_ordered(terms), key=score)
    out: list[str] = []
    seen: set[str] = set()
    for term in ranked:
        low = term.lower()
        if "rag" in low_claim and re.search(r"\brag-[12]\b|rag1|rag2|deficient mice", low):
            continue
        key = low[:-1] if len(low) > 4 and low.endswith("s") else low
        if key in seen:
            continue
        seen.add(key)
        out.append(term)
        if len(out) >= limit:
            break
    return out


def quote_boolean_term(term: str) -> str:
    term = normalize_ws(term)
    if not term:
        return ""
    if term.startswith('"') and term.endswith('"'):
        return term
    if re.search(r"\s", term):
        return f'"{term}"'
    return term


def boolean_group(terms: list[str]) -> str:
    cleaned = [quote_boolean_term(term) for term in unique_ordered(terms) if term]
    cleaned = [term for term in cleaned if term]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    return "(" + " OR ".join(cleaned) + ")"


def query_words(query: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9-]+|[\u4e00-\u9fff]+", normalize_ws(query))


def is_simple_publisher_query(query: str, max_words: int = PUBLISHER_QUERY_MAX_WORDS) -> bool:
    query = normalize_ws(query)
    if not query:
        return False
    if re.search(r"\b(?:AND|OR|NOT)\b", query, flags=re.IGNORECASE):
        return False
    if any(token in query for token in "()[]{}"):
        return False
    return 0 < len(query_words(query)) <= max_words


def publisher_query_candidates(candidates: list[str], max_words: int = PUBLISHER_QUERY_MAX_WORDS) -> list[str]:
    return [
        query for query in unique_ordered(candidates)
        if is_simple_publisher_query(query, max_words=max_words)
    ]


FAMILY_FOCUS_TERMS: dict[str, list[str]] = {
    "definition-landscape": ["terminology", "taxonomy", "field map", "framework", "overview"],
    "data-resources": ["dataset", "database", "benchmark", "corpus", "resource"],
    "methods-models": ["method", "model", "algorithm", "workflow", "pipeline"],
    "evaluation-benchmarks": ["evaluation", "validation", "benchmark", "comparison", "metric"],
    "applications-cases": ["application", "use case", "case study", "deployment", "practice"],
    "limitations-gaps": ["limitation", "challenge", "gap", "future direction", "open problem"],
    "general-evidence": ["review", "method", "evidence", "application", "challenge"],
}

GENERIC_QUERY_SUFFIXES = {
    "review", "survey", "overview", "landscape", "framework", "terminology",
    "taxonomy", "field", "map", "method", "methods", "model", "models",
    "algorithm", "workflow", "pipeline", "dataset", "database", "benchmark",
    "corpus", "resource", "resources", "evaluation", "validation",
    "comparison", "metric", "application", "applications", "case", "study",
    "deployment", "practice", "limitation", "challenge", "gap", "future",
    "direction", "problem", "evidence",
}


def canonical_query_family(label: str) -> str:
    low = (label or "").lower()
    if "evaluation-benchmarks" in low:
        return "evaluation-benchmarks"
    if "data-resources" in low:
        return "data-resources"
    if "methods-models" in low:
        return "methods-models"
    if "applications-cases" in low:
        return "applications-cases"
    if "limitations-gaps" in low:
        return "limitations-gaps"
    if "definition-landscape" in low:
        return "definition-landscape"
    if any(token in low for token in ["definition", "landscape", "overview", "broad"]):
        return "definition-landscape"
    if any(token in low for token in ["evaluation", "validation", "limitation"]):
        return "evaluation-benchmarks"
    if any(token in low for token in ["resource", "data", "dataset", "database", "benchmark", "corpus"]):
        return "data-resources"
    if any(token in low for token in ["method", "model", "workflow", "algorithm", "construction"]):
        return "methods-models"
    if any(token in low for token in ["application", "use", "case", "deployment"]):
        return "applications-cases"
    if any(token in low for token in ["gap", "future", "challenge", "direction"]):
        return "limitations-gaps"
    return "general-evidence"


def openalex_work_evidence_terms(works: list[dict[str, Any]] | None, limit: int = 12) -> list[str]:
    primary_terms: list[str] = []
    detail_terms: list[str] = []
    for work in works or []:
        work_terms: list[str] = []
        title = str(work.get("title") or "")
        for token in re.findall(r"\b[A-Z][A-Za-z0-9_-]{2,}\b", title):
            if token.lower() not in {"the", "and", "for"}:
                work_terms.append(token)
        work_terms.extend(phrase_terms(title)[:3])
        for field in ["keywords", "topics"]:
            for value in work.get(field) or []:
                if value:
                    value_terms = phrase_terms(str(value)) or core_terms(str(value))
                    work_terms.extend(value_terms[:2])
        ordered_work_terms = unique_ordered(work_terms)
        if ordered_work_terms:
            primary_terms.append(ordered_work_terms[0])
            detail_terms.extend(ordered_work_terms[1:4])
    return unique_ordered(primary_terms + detail_terms)[:limit]


def build_concept_groups(
    claim: str,
    family: str,
    subquestion: str,
    ranked_terms: list[str],
    openalex_evidence_terms: list[str] | None = None,
) -> list[dict[str, Any]]:
    claim_terms = unique_ordered(phrase_terms(claim) + core_terms(claim))
    topic_terms = ranked_query_terms(unique_ordered(claim_terms + ranked_terms), claim, 7)[:4]
    for term in reversed(claim_terms[-5:]):
        if term.lower() not in {t.lower() for t in topic_terms}:
            topic_terms.append(term)
        if len(topic_terms) >= 5:
            break
    subquestion_terms = ranked_query_terms(core_terms(subquestion), subquestion, 6)[:4]
    family_terms = FAMILY_FOCUS_TERMS.get(canonical_query_family(family), FAMILY_FOCUS_TERMS["general-evidence"])
    groups = [
        {"label": "topic", "terms": topic_terms[:4] or core_terms(claim)[:4]},
        {"label": "focus", "terms": family_terms[:5]},
        {"label": "subquestion", "terms": [term for term in subquestion_terms if term.lower() not in {t.lower() for t in topic_terms}][:4]},
        {"label": "openalex_evidence", "terms": unique_ordered(openalex_evidence_terms or [])[:6]},
    ]
    return [group for group in groups if group["terms"]]


def build_boolean_query(concept_groups: list[dict[str, Any]], max_groups: int = 3) -> str:
    parts = [boolean_group([str(term) for term in group.get("terms", [])]) for group in concept_groups[:max_groups]]
    parts = [part for part in parts if part]
    return " AND ".join(parts)


def boolean_query_variants(boolean_query: str, concept_groups: list[dict[str, Any]], max_queries: int) -> list[str]:
    variants = [boolean_query]
    topic = boolean_group([str(term) for term in (concept_groups[0].get("terms", []) if concept_groups else [])[:4]])
    focus = boolean_group([str(term) for term in (concept_groups[1].get("terms", []) if len(concept_groups) > 1 else [])[:4]])
    if topic and focus:
        variants.append(f"{topic} AND {focus}")
    if topic:
        variants.append(topic)
    return unique_ordered(variants)[:max_queries or None]


def simple_keyword_query(terms: list[str], max_terms: int = 14) -> str:
    out: list[str] = []
    for term in unique_ordered([str(term) for term in terms]):
        out.append(quote_boolean_term(term))
        if len(out) >= max_terms:
            break
    return " ".join(out)


def simple_keyword_query_variants(concept_groups: list[dict[str, Any]], max_queries: int) -> list[str]:
    groups = [[str(term) for term in group.get("terms", [])] for group in concept_groups]
    variants: list[str] = []
    if groups:
        variants.append(simple_keyword_query([term for group in groups[:3] for term in group]))
    if len(groups) >= 2:
        variants.append(simple_keyword_query(groups[0] + groups[1]))
    if len(groups) >= 3:
        variants.append(simple_keyword_query(groups[0] + groups[2]))
    if groups:
        variants.append(simple_keyword_query(groups[0]))
    return unique_ordered([variant for variant in variants if variant])[:max_queries or None]


def compact_query(text: str, max_words: int = PUBLISHER_QUERY_MAX_WORDS) -> str:
    words = query_words(text)
    return normalize_ws(" ".join(words[:max_words]))


def query_base_key(query: str) -> str:
    terms = [
        token.lower()
        for token in query_words(query)
        if token.lower() not in GENERIC_QUERY_SUFFIXES
    ]
    return " ".join(terms)


def claim_query_bases(claim: str, concept_groups: list[dict[str, Any]]) -> list[str]:
    groups_by_label = {
        str(group.get("label", "")): [str(term) for term in group.get("terms", [])]
        for group in concept_groups
    }
    topic_terms = groups_by_label.get("topic") or []
    evidence_terms = groups_by_label.get("openalex_evidence") or groups_by_label.get("evidence") or []
    phrases = phrase_terms(claim)
    core = core_terms(claim)
    ngrams: list[str] = []
    for size in (3, 2):
        for index in range(0, max(0, len(core) - size + 1)):
            ngrams.append(" ".join(core[index:index + size]))
    candidates = unique_ordered([
        claim_search_topic_phrase(claim),
        *evidence_terms[:4],
        *phrases,
        *ngrams,
        *topic_terms,
        " ".join(core[:3]),
        " ".join(core[:2] + core[-1:]),
    ])
    blocked = {
        "review", "survey", "overview", "landscape", "method", "model",
        "algorithm", "workflow", "dataset", "database", "benchmark",
        "evaluation", "validation", "application", "limitation", "challenge",
    }
    bases: list[str] = []
    for candidate in candidates:
        query = compact_query(candidate, max_words=5)
        if not query or not is_simple_publisher_query(query, max_words=5):
            continue
        words = [word.lower() for word in query_words(query)]
        if words and all(word in blocked for word in words):
            continue
        bases.append(query)
    return unique_ordered(bases)


def evidence_terms_from_groups(concept_groups: list[dict[str, Any]], claim: str) -> list[str]:
    claim_lows = {term.lower() for term in core_terms(claim) + phrase_terms(claim)}
    terms: list[str] = []
    for group in concept_groups:
        label = str(group.get("label") or "")
        if label not in {"openalex_evidence", "evidence"}:
            continue
        for term in group.get("terms") or []:
            low = str(term).lower()
            if low and low not in claim_lows:
                terms.append(str(term))
    return unique_ordered(terms)


def build_query_provenance(
    query: str,
    plan: QueryPlan,
    concept_groups: list[dict[str, Any]],
    claim: str,
) -> dict[str, Any]:
    groups_by_label = {
        str(group.get("label", "")): [str(term) for term in group.get("terms", [])]
        for group in concept_groups
    }
    evidence_terms = evidence_terms_from_groups(concept_groups, claim)
    matched_evidence = [
        term for term in evidence_terms
        if term.lower() in query.lower()
    ]
    source_items = [
        f"{label}:{term}"
        for label, terms in groups_by_label.items()
        for term in terms
        if str(term).lower() in query.lower()
    ][:8]
    return {
        "query": query,
        "query_intent": plan.query_family,
        "subquestion_fit": f"{plan.claim_subquestion} Query terms are selected for the {plan.query_family} evidence task.",
        "concept_blocks": {
            "topic_block": groups_by_label.get("topic", []),
            "subquestion_block": groups_by_label.get("subquestion", []),
            "evidence_block": evidence_terms,
            "focus_block": groups_by_label.get("focus", []),
        },
        "evidence_terms_used": matched_evidence,
        "source_items": source_items,
        "why_not_generic": "Uses a distinct topic/evidence base plus the current subquestion focus instead of only changing review/survey-style suffixes.",
    }


def concise_keyword_query_variants(
    claim: str,
    family: str,
    concept_groups: list[dict[str, Any]],
    max_queries: int,
) -> list[str]:
    """Build short recall-oriented publisher queries from the concept map.

    The concept groups are deliberately richer than a publisher keyword query.
    Search pages behave better with a few domain-bearing terms, while the
    broader concept map remains available for agent review and refinement.
    """
    groups_by_label = {
        str(group.get("label", "")): [str(term) for term in group.get("terms", [])]
        for group in concept_groups
    }
    focus_terms = groups_by_label.get("focus") or []
    subquestion_terms = groups_by_label.get("subquestion") or []
    bases = claim_query_bases(claim, concept_groups)
    variants: list[str] = []
    seen_base_keys: set[str] = set()
    modifiers = unique_ordered(focus_terms + subquestion_terms)
    for index, base in enumerate(bases):
        modifier = modifiers[index % len(modifiers)] if modifiers else ""
        candidate = compact_query(f"{base} {modifier}", max_words=6)
        base_key = query_base_key(candidate)
        if not candidate or base_key in seen_base_keys:
            continue
        seen_base_keys.add(base_key)
        variants.append(candidate)
        if len(variants) >= max_queries:
            break
    if len(variants) < max_queries:
        for modifier in modifiers:
            for base in bases:
                candidate = compact_query(f"{base} {modifier}", max_words=6)
                base_key = query_base_key(candidate)
                if candidate and base_key not in seen_base_keys:
                    seen_base_keys.add(base_key)
                    variants.append(candidate)
                if len(variants) >= max_queries:
                    break
            if len(variants) >= max_queries:
                break
    if not variants and bases:
        variants.append(bases[0])
    return publisher_query_candidates(variants, max_words=6)[:max_queries or None]


def dedupe_queries_across_plans(plans: list[QueryPlan], max_queries: int) -> list[QueryPlan]:
    seen: set[str] = set()
    for plan in plans:
        unique_queries: list[str] = []
        for query in plan.queries:
            key = normalize_ws(query).lower()
            if not key or key in seen or not is_simple_publisher_query(query):
                continue
            seen.add(key)
            unique_queries.append(query)
            if len(unique_queries) >= max_queries:
                break
        if not unique_queries and plan.queries:
            base_words = query_words(plan.queries[0])[: max(1, PUBLISHER_QUERY_MAX_WORDS - 1)]
            for focus in FAMILY_FOCUS_TERMS.get(canonical_query_family(plan.query_family), []):
                fallback = normalize_ws(" ".join([*base_words, focus]))
                key = fallback.lower()
                if key and key not in seen and is_simple_publisher_query(fallback):
                    seen.add(key)
                    unique_queries.append(fallback)
                    break
        plan.queries = unique_queries
    return plans


def attach_query_provenance(plans: list[QueryPlan], claim: str) -> list[QueryPlan]:
    for plan in plans:
        plan.query_provenance = [
            build_query_provenance(query, plan, plan.concept_groups or [], claim)
            for query in plan.queries
        ]
    return plans


def keyword_query_for_publisher(boolean_query: str, max_terms: int = 14) -> str:
    """Convert a planner Boolean query into a publisher-friendly keyword string."""
    phrases = re.findall(r'"([^"]+)"', boolean_query or "")
    without_phrases = re.sub(r'"[^"]+"', " ", boolean_query or "")
    words = re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}|[\u4e00-\u9fff]{2,}", without_phrases)
    skip = {"AND", "OR", "NOT"}
    terms = unique_ordered(phrases + [word for word in words if word.upper() not in skip])
    out: list[str] = []
    for term in terms:
        out.append(quote_boolean_term(term))
        if len(out) >= max_terms:
            break
    return " ".join(out) or boolean_query


def publisher_query_urls(
    query: str,
    year_start: int,
    year_end: int,
    sciencedirect_route: str = "direct",
    sciencedirect_article_types: str = "FLA,REV",
    include_springer: bool = True,
) -> dict[str, str]:
    urls = search_urls_for_query(
        query,
        year_start,
        year_end,
        sciencedirect_route,
        sciencedirect_article_types,
        include_springer,
    )
    return {infer_publisher(url): url for url in urls}


def abstract_from_openalex_index(index: Any) -> str:
    if not isinstance(index, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, offsets in index.items():
        if not isinstance(offsets, list):
            continue
        for offset in offsets:
            if isinstance(offset, int):
                positions.append((offset, str(word)))
    return " ".join(word for _offset, word in sorted(positions))


def openalex_grounding_collect(claim: str, max_terms: int = 12, max_works: int = 16) -> dict[str, Any]:
    if not os.environ.get("OPENALEX_API_KEY"):
        return {
            "terms": [],
            "works": [],
            "probe_queries": openalex_grounding_probe_queries(claim),
            "concept_hints": openalex_grounding_concept_hints(claim),
        }
    claim_terms = {term.lower() for term in core_terms(claim)}
    domain_terms = claim_domain_terms(claim)
    framework_terms = claim_framework_terms(claim)
    seed_queries = openalex_grounding_probe_queries(claim)
    broad_claim_queries = set(unique_ordered([
        claim,
        " ".join(core_terms(claim)[:6]),
        " ".join(core_terms(claim)[:4]),
    ]))
    terms: list[str] = []
    works: list[dict[str, Any]] = []
    seen_work_keys: set[str] = set()
    request_errors: list[str] = []
    for query in unique_ordered(seed_queries):
        if not query:
            continue
        data = openalex_request({"search": query, "per-page": "8"})
        if data.get("_error"):
            request_errors.append(str(data.get("_error")))
            continue
        for work in data.get("results") or []:
            abstract = abstract_from_openalex_index(work.get("abstract_inverted_index"))
            text_parts = [
                str(work.get("display_name") or ""),
                abstract,
            ]
            primary_topic = work.get("primary_topic") or {}
            if primary_topic.get("display_name"):
                text_parts.append(str(primary_topic.get("display_name")))
            for topic in work.get("topics") or []:
                if topic.get("display_name"):
                    text_parts.append(str(topic.get("display_name")))
            for keyword in work.get("keywords") or []:
                if isinstance(keyword, dict):
                    value = keyword.get("display_name") or keyword.get("name") or keyword.get("keyword")
                    if value:
                        text_parts.append(str(value))
                elif keyword:
                    text_parts.append(str(keyword))
            joined = " ".join(text_parts).lower()
            overlap = sum(1 for term in claim_terms if term in joined)
            min_overlap = min(2, len(claim_terms)) if query in broad_claim_queries else 1
            if claim_terms and overlap < min_overlap:
                continue
            if not openalex_work_matches_claim(joined, domain_terms, framework_terms):
                continue
            work_key = str(work.get("id") or work.get("doi") or work.get("display_name") or "").lower()
            if work_key and work_key in seen_work_keys:
                continue
            if work_key:
                seen_work_keys.add(work_key)
            terms.extend(core_terms(" ".join(text_parts)))
            if len(works) < max_works:
                host_venue = work.get("primary_location") or {}
                source = host_venue.get("source") if isinstance(host_venue.get("source"), dict) else {}
                authorships = work.get("authorships") or []
                authors = []
                for authorship in authorships[:6]:
                    author = authorship.get("author") if isinstance(authorship, dict) else {}
                    name = author.get("display_name") if isinstance(author, dict) else ""
                    if name:
                        authors.append(str(name))
                works.append({
                    "query": query,
                    "id": work.get("id") or "",
                    "title": work.get("display_name") or "",
                    "year": work.get("publication_year") or "",
                    "doi": work.get("doi") or "",
                    "cited_by_count": work.get("cited_by_count") or 0,
                    "authors": authors,
                    "venue": source.get("display_name") if isinstance(source, dict) else "",
                    "source_display_name": source.get("display_name") if isinstance(source, dict) else "",
                    "publisher": source.get("host_organization_name") if isinstance(source, dict) else "",
                    "host_organization_name": source.get("host_organization_name") if isinstance(source, dict) else "",
                    "landing_page_url": host_venue.get("landing_page_url") if isinstance(host_venue, dict) else "",
                    "primary_topic": primary_topic.get("display_name") or "",
                    "topics": [
                        topic.get("display_name")
                        for topic in (work.get("topics") or [])[:5]
                        if isinstance(topic, dict) and topic.get("display_name")
                    ],
                    "keywords": [
                        (keyword.get("display_name") or keyword.get("name") or keyword.get("keyword"))
                        for keyword in (work.get("keywords") or [])[:8]
                        if isinstance(keyword, dict) and (keyword.get("display_name") or keyword.get("name") or keyword.get("keyword"))
                    ],
                    "abstract_excerpt": normalize_ws(abstract)[:800],
                })
    result: dict[str, Any] = {
        "terms": unique_ordered(terms)[:max_terms],
        "works": works,
        "probe_queries": seed_queries,
        "concept_hints": openalex_grounding_concept_hints(claim),
    }
    if request_errors and not terms and not works:
        result["error"] = "; ".join(unique_ordered(request_errors)[:3])
    return result


def openalex_grounding_terms(claim: str, max_terms: int = 12) -> list[str]:
    if not os.environ.get("OPENALEX_API_KEY"):
        return []
    return list(openalex_grounding_collect(claim, max_terms=max_terms).get("terms") or [])
    return unique_ordered(terms)[:max_terms]


def openalex_grounding_audit(claim: str, requested: bool = True, max_terms: int = 12) -> dict[str, Any]:
    api_key_present = bool(os.environ.get("OPENALEX_API_KEY"))
    audit: dict[str, Any] = {
        "requested": bool(requested),
        "api_key_present": api_key_present,
        "status": "disabled",
        "terms": [],
        "error": "",
    }
    if not requested:
        return audit
    if not api_key_present:
        audit["status"] = "missing_api_key"
        return audit
    try:
        grounding = openalex_grounding_collect(claim, max_terms=max_terms)
    except Exception as exc:
        audit["status"] = "error"
        audit["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return audit
    terms = list(grounding.get("terms") or [])
    audit["terms"] = terms
    audit["works"] = list(grounding.get("works") or [])
    audit["probe_queries"] = list(grounding.get("probe_queries") or openalex_grounding_probe_queries(claim))
    audit["concept_hints"] = list(grounding.get("concept_hints") or openalex_grounding_concept_hints(claim))
    if grounding.get("error"):
        audit["status"] = "error"
        audit["error"] = str(grounding.get("error"))[:500]
        return audit
    audit["status"] = "ok" if terms else "no_terms"
    return audit


def dynamic_subquestion_specs(claim: str, ranked_terms: list[str], rounds: int) -> list[tuple[str, str, str, str]]:
    specs = [
        (
            "definition-landscape",
            "What major papers, reviews, resources, terms, and evidence clusters define this topic?",
            "landscape",
            "Landscape and Definitions",
        ),
        (
            "data-resources",
            "Which datasets, databases, benchmarks, corpora, tools, or reusable resources define the evidence base?",
            "resources",
            "Resources and Data",
        ),
        (
            "methods-models",
            "Which methods, models, algorithms, workflows, or construction processes are used to address this topic?",
            "methods",
            "Methods and Models",
        ),
        (
            "evaluation-benchmarks",
            "Which evaluation evidence, validation methods, benchmarks, comparisons, or metrics support confidence?",
            "evaluation",
            "Evaluation and Benchmarks",
        ),
        (
            "applications-cases",
            "Which applications, use cases, deployments, or domain practices show how this topic is used?",
            "applications",
            "Applications and Cases",
        ),
        (
            "limitations-gaps",
            "Which limitations, open problems, contradictions, and future directions remain unresolved?",
            "limitations",
            "Limitations and Gaps",
        ),
    ]
    return specs[: max(1, rounds)]


def build_query_plans(
    claim: str,
    explicit_queries: list[str],
    max_queries: int,
    rounds: int,
    use_openalex_grounding: bool = True,
    openalex_terms: list[str] | None = None,
    openalex_works: list[dict[str, Any]] | None = None,
) -> list[QueryPlan]:
    def make_plan(
        round_index: int,
        query_family: str,
        claim_subquestion: str,
        group_slug: str,
        group_title: str,
    ) -> QueryPlan:
        family = query_family
        concept_groups = build_concept_groups(claim, family, claim_subquestion, ranked_terms, evidence_terms)
        boolean_query = build_boolean_query(concept_groups)
        return QueryPlan(
            round_index,
            family,
            claim_subquestion,
            concise_keyword_query_variants(claim, family, concept_groups, max(max_queries * 3, max_queries + 3)),
            group_slug=group_slug,
            group_title=group_title,
            concept_groups=concept_groups,
            boolean_query=boolean_query,
        )

    if explicit_queries:
        raw_queries = unique_ordered(explicit_queries)[:max_queries or None]
        plans = [
            QueryPlan(
                index,
                f"user-provided-{index:02d}",
                f"User-provided search query: {query}",
                [query],
                group_slug=f"{index:02d}_user_provided",
                group_title=f"User Provided {index:02d}",
                concept_groups=[{"label": "user-provided", "terms": [query]}],
                boolean_query=query,
            )
            for index, query in enumerate(raw_queries, start=1)
        ]
        return attach_query_provenance(assign_subquestion_ids(plans), claim)

    terms = core_terms(claim)
    expanded_terms = domain_query_expansions(claim)
    if expanded_terms:
        terms = unique_ordered(expanded_terms + terms)
    if use_openalex_grounding:
        evidence_terms = openalex_work_evidence_terms(openalex_works)
        terms = unique_ordered(terms + (openalex_terms if openalex_terms is not None else openalex_grounding_terms(claim)) + evidence_terms)
    else:
        evidence_terms = []
    ranked_terms = ranked_query_terms(terms, claim, 12)
    plans = [
        make_plan(index, family, subquestion, f"{index:02d}_{group_slug}", group_title)
        for index, (family, subquestion, group_slug, group_title)
        in enumerate(dynamic_subquestion_specs(claim, ranked_terms, rounds), start=1)
    ]
    return attach_query_provenance(assign_subquestion_ids(dedupe_queries_across_plans(plans[: max(1, rounds)], max_queries)), claim)


def search_urls_for_query(
    query: str,
    year_start: int,
    year_end: int,
    sciencedirect_route: str,
    sciencedirect_article_types: str,
    include_springer: bool,
) -> list[str]:
    keyword_query = keyword_query_for_publisher(query)
    keyword_plus = quote_plus(keyword_query)
    date_range = f"{year_start}-{year_end}"
    urls: list[str] = []
    article_type_part = f"&articleTypes={quote_plus(sciencedirect_article_types)}" if sciencedirect_article_types else ""
    urls.append(
        f"https://www.sciencedirect.com/search?qs={keyword_plus}&date={date_range}{article_type_part}"
    )
    urls.append(
        f"https://pubs.acs.org/action/doSearch?field1=AllField&text1={keyword_plus}&ConceptID=&ConceptID=&publication=&accessType=allContent&Earliest=&AfterMonth=12&AfterYear={year_start}&BeforeMonth=12&BeforeYear={year_end}&pageSize=20"
    )
    urls.append(
        f"https://onlinelibrary.wiley.com/action/doSearch?field1=AllField&text1={keyword_plus}&field2=AllField&text2=&field3=AllField&text3=&publication=&Ppub=&AfterMonth=12&AfterYear={year_start}&BeforeMonth=12&BeforeYear={year_end}&pageSize=20"
    )
    if include_springer:
        urls.append(
            f"https://link.springer.com/search?advancedSearch=true&sortBy=relevance&query={keyword_plus}&title=&contributor=&journal=&date=custom&dateFrom={year_start}&dateTo={year_end}"
        )
    return urls


def build_search_jobs(
    claim: str,
    explicit_queries: list[str],
    max_queries: int,
    rounds: int,
    year_start: int,
    year_end: int,
    sciencedirect_route: str,
    sciencedirect_article_types: str,
    include_springer: bool,
    use_openalex_grounding: bool = True,
) -> tuple[list[dict[str, Any]], list[QueryPlan]]:
    plans = build_query_plans(claim, explicit_queries, max_queries, rounds, use_openalex_grounding)
    jobs: list[dict[str, Any]] = []
    for plan in plans:
        if plan.queries:
            plan.publisher_queries = publisher_query_urls(
                plan.queries[0],
                year_start,
                year_end,
                sciencedirect_route,
                sciencedirect_article_types,
                include_springer,
            )
        for query in plan.queries[: max_queries or None]:
            for url in search_urls_for_query(query, year_start, year_end, sciencedirect_route, sciencedirect_article_types, include_springer):
                jobs.append({"url": url, "query": query, "plan": plan})
    return jobs, plans


async def extract_search_results(page: Page, search_url: str, publisher: str) -> list[dict[str, str]]:
    script = """({searchUrl, publisher}) => {
      const out = [];
      const seen = new Set();
      const abs = href => {
        try { return new URL(href, location.href).toString(); } catch { return ""; }
      };
      const clean = text => String(text || '').replace(/\\s+/g, ' ').trim();
      const abstractSource = () => {
        if (publisher === 'elsevier' || publisher === 'sciencedirect') return 'sciencedirect_search_result_abstract';
        if (publisher === 'acs') return 'acs_search_result_abstract';
        if (publisher === 'wiley') return 'wiley_search_result_abstract';
        if (publisher === 'springer') return 'springer_search_result_snippet';
        return 'publisher_search_page';
      };
      const articleAllowed = href => {
        const low = href.toLowerCase();
        if (['/pdf', 'pdfft', '/epdf', 'download', 'pdfdownload', 'viewpdf'].some(token => low.includes(token))) return false;
        const path = (() => { try { return new URL(href).pathname.toLowerCase(); } catch { return ''; } })();
        if (publisher === 'elsevier') return /\\/science\\/article\\/pii\\/[^/]+\\/?$/.test(path) && low.includes('sciencedirect.com');
        if (publisher === 'acs') return low.includes('/doi/') && !low.includes('/doi/abs/') && !low.includes('/doi/full/') && !low.includes('/action/') && !low.includes('/toc/');
        if (publisher === 'wiley') return low.includes('/doi/') && !low.includes('/doi/book/') && !low.includes('/action/') && !low.includes('/toc/');
        if (publisher === 'springer') return (low.includes('/article/10.') || low.includes('/chapter/10.') || low.includes('/protocol/10.')) && !low.includes('/search');
        if (['/search', '/action/', '/toc/', '/login', '/sign', '/account', '/user', '/about', '/help', '/journal', '/issue', '/browse', '/topic'].some(token => low.includes(token))) return false;
        return /10\\.\\d{4,9}/.test(low) || ['/doi/', '/article', '/document/', '/abs/', '/abstract/', '/content/'].some(token => low.includes(token));
      };
      const abstractFromBox = (box, title, boxText) => {
        const candidates = [];
        const selectors = [
          '[class*="abstract" i]',
          '[id*="abstract" i]',
          '[data-testid*="abstract" i]',
          '[aria-label*="abstract" i]',
          'section',
          'p',
          'div'
        ];
        const push = value => {
          const text = clean(value)
            .replace(/^Abstract\\s*/i, '')
            .replace(/^Graphical Abstract\\s*/i, '');
          if (text.length < 80) return;
          const low = text.toLowerCase();
          const titleLow = clean(title).toLowerCase();
          if (titleLow && low === titleLow) return;
          if (/^(view pdf|download|export|figures?|extracts?|graphical abstract|abstract)$/i.test(text)) return;
          if (/\\b(view pdf|download selected|set search alert|cookie settings)\\b/i.test(text)) return;
          candidates.push(text);
        };
        for (const selector of selectors) {
          let nodes = [];
          try { nodes = Array.from(box.querySelectorAll(selector)); } catch { nodes = []; }
          for (const node of nodes.slice(0, 80)) {
            push(node.innerText || node.textContent || '');
          }
        }
        if (!candidates.length) {
          const match = clean(boxText).match(/(?:^|\\b)Abstract\\s*(?:Graphical Abstract\\s*)?(?:Extracts\\s*)?(?:Figures\\s*)?(?:Export\\s*)?(.{80,2500})/i);
          if (match) push(match[1]);
        }
        candidates.sort((a, b) => b.length - a.length);
        return (candidates[0] || '').slice(0, 2500);
      };
      for (const a of Array.from(document.querySelectorAll('a[href]'))) {
        const href = abs(a.getAttribute('href') || '');
        if (!articleAllowed(href) || seen.has(href)) continue;
        const box = a.closest('li, article, .result, .search-result, .issue-item, .card, .publication, .item') || a.parentElement || a;
        const boxText = clean(box.innerText || '');
        const title = clean(a.innerText || a.getAttribute('title') || '');
        if (!title || title.length < 8) continue;
        if (/\\b(pdf|download)\\b/i.test(title)) continue;
        const abstract = abstractFromBox(box, title, boxText);
        seen.add(href);
        out.push({
          title,
          href,
          context: boxText.slice(0, 1400),
          abstract,
          abstract_source: abstract ? abstractSource() : '',
          searchUrl
        });
      }
      return out;
    }"""
    last_error: Exception | None = None
    for _attempt in range(3):
        try:
            return await page.evaluate(script, {"searchUrl": search_url, "publisher": publisher})
        except Exception as exc:
            last_error = exc
            await page.wait_for_timeout(1500)
    if last_error:
        raise last_error
    return []


async def expand_search_page_abstracts(
    page: Page,
    publisher: str,
    *,
    max_buttons: int = 20,
    wait_ms: int = 700,
) -> int:
    """Best-effort expansion of result-page abstract widgets before DOM extraction."""
    script = """({publisher, maxButtons}) => {
      const clean = text => String(text || '').replace(/\\s+/g, ' ').trim();
      const controls = Array.from(document.querySelectorAll('button, a[role="button"], a[href], summary'));
      const candidates = [];
      const seen = new Set();
      for (const el of controls) {
        const text = clean(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '');
        if (!text) continue;
        if (!/\\babstract\\b/i.test(text)) continue;
        if (/graphical abstract/i.test(text)) continue;
        const key = `${el.tagName}:${text}:${el.getAttribute('href') || ''}:${el.getAttribute('aria-expanded') || ''}`;
        if (seen.has(key)) continue;
        seen.add(key);
        const expanded = String(el.getAttribute('aria-expanded') || '').toLowerCase();
        if (expanded === 'true') continue;
        candidates.push(el);
      }
      let clicked = 0;
      for (const el of candidates.slice(0, maxButtons)) {
        try {
          el.scrollIntoView({block: 'center', inline: 'nearest'});
          el.click();
          clicked += 1;
        } catch {}
      }
      return clicked;
    }"""
    clicked = 0
    try:
        clicked = int(await page.evaluate(script, {"publisher": publisher, "maxButtons": max_buttons}) or 0)
    except Exception:
        return 0
    if clicked and wait_ms > 0:
        await page.wait_for_timeout(wait_ms)
    return clicked


def openalex_request(params: dict[str, str]) -> dict[str, Any]:
    api_key = os.environ.get("OPENALEX_API_KEY", "")
    if api_key:
        params = {**params, "api_key": api_key}
    url = "https://api.openalex.org/works?" + urlencode(params)
    request = Request(url, headers={"User-Agent": "literature-loop-capture/1.0"})
    kwargs: dict[str, Any] = {"timeout": 30}
    context = trusted_https_context()
    if context is not None:
        kwargs["context"] = context
    try:
        with urlopen(request, **kwargs) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return {"_error": f"HTTPError {exc.code}"}
    except (URLError, TimeoutError, OSError) as exc:
        return {"_error": f"{type(exc).__name__}: {str(exc)[:200]}"}


def crossref_work_by_doi(doi: str) -> dict[str, Any]:
    if not doi:
        return {}
    url = "https://api.crossref.org/works/" + quote_plus(doi)
    request = Request(url, headers={"User-Agent": "literature-loop-capture/1.0"})
    try:
        with urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data.get("message") or {}
    except (HTTPError, URLError, TimeoutError, OSError):
        return {}


def default_year_start(today: date | None = None) -> int:
    today = today or date.today()
    return today.year - 4
