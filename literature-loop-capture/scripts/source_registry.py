"""Source and publisher registry for publisher discovery."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from urllib.parse import quote_plus, urlparse


@dataclass(frozen=True)
class SourceAdapter:
    key: str
    kind: str
    domains: list[str]
    landing_patterns: list[str] = field(default_factory=list)
    identifier_types: list[str] = field(default_factory=list)
    metadata_extractors: list[str] = field(default_factory=list)
    abstract_extractors: list[str] = field(default_factory=list)
    pdf_link_patterns: list[str] = field(default_factory=list)
    advanced_search: dict[str, object] = field(default_factory=dict)
    default_access_route: str = "direct_oa"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


REGISTRY: dict[str, SourceAdapter] = {
    "pubmed": SourceAdapter(
        key="pubmed",
        kind="bibliographic_index",
        domains=["pubmed.ncbi.nlm.nih.gov"],
        landing_patterns=["https://pubmed.ncbi.nlm.nih.gov/{PMID}/"],
        identifier_types=["pmid", "pmcid", "doi"],
        metadata_extractors=["pubmed_meta", "citation_meta"],
        abstract_extractors=["pubmed_abstract", "citation_abstract"],
        advanced_search={
            "page": "https://pubmed.ncbi.nlm.nih.gov/advanced/",
            "result_template": "https://pubmed.ncbi.nlm.nih.gov/?term={query_plus}+AND+%28%22{year_start}%22%5BDate+-+Publication%5D+%3A+%22{year_end}%22%5BDate+-+Publication%5D%29",
            "fields": ["Search Term", "Field Selector", "Start Date", "End Date"],
        },
    ),
    "arxiv": SourceAdapter(
        key="arxiv",
        kind="preprint_repository",
        domains=["arxiv.org"],
        landing_patterns=["https://arxiv.org/abs/{ARXIV_ID}"],
        identifier_types=["arxiv_id", "doi"],
        metadata_extractors=["arxiv_meta"],
        abstract_extractors=["arxiv_abstract"],
        pdf_link_patterns=["/pdf/"],
        advanced_search={
            "page": "https://arxiv.org/search/advanced",
            "result_template": "https://arxiv.org/search/advanced?advanced=&terms-0-operator=AND&terms-0-term={query_plus}&terms-0-field=all&date-filter_by=date_range&date-from_date={year_start}&date-to_date={year_end}&date-date_type=submitted_date&abstracts=show&size=50&order=-announced_date_first",
            "fields": ["Search term", "Field to search", "Subject", "Date range", "Date type", "Show abstracts"],
        },
    ),
    "sciencedirect": SourceAdapter(
        key="sciencedirect",
        kind="publisher_platform",
        domains=["sciencedirect.com", "www.sciencedirect.com"],
        landing_patterns=["https://www.sciencedirect.com/science/article/pii/{PII}"],
        identifier_types=["pii", "doi"],
        metadata_extractors=["citation_meta", "json_ld", "page_heading"],
        abstract_extractors=["citation_abstract", "json_ld_abstract", "abstract_section"],
        pdf_link_patterns=["/science/article/pii/", "/pdfft"],
        advanced_search={
            "page": "https://www.sciencedirect.com/search",
            "result_template": "https://www.sciencedirect.com/search?tak={query_quote}&years={year_csv}",
            "fields": ["Find articles with these terms", "Year(s)", "Title, abstract or author-specified keywords"],
        },
        default_access_route="publisher_authenticated",
    ),
    "elsevier": SourceAdapter(
        key="elsevier",
        kind="publisher_platform",
        domains=["sciencedirect.com", "www.sciencedirect.com"],
        landing_patterns=["https://www.sciencedirect.com/science/article/pii/{PII}"],
        identifier_types=["pii", "doi"],
        metadata_extractors=["citation_meta", "json_ld", "page_heading"],
        abstract_extractors=["citation_abstract", "json_ld_abstract", "abstract_section"],
        pdf_link_patterns=["/science/article/pii/", "/pdfft"],
        advanced_search={
            "page": "https://www.sciencedirect.com/search",
            "result_template": "https://www.sciencedirect.com/search?tak={query_quote}&years={year_csv}",
            "fields": ["Find articles with these terms", "Year(s)", "Title, abstract or author-specified keywords"],
        },
        default_access_route="publisher_authenticated",
    ),
    "acs": SourceAdapter(
        key="acs",
        kind="publisher_platform",
        domains=["pubs.acs.org"],
        landing_patterns=["https://pubs.acs.org/doi/{DOI}"],
        identifier_types=["doi"],
        metadata_extractors=["citation_meta", "json_ld", "page_heading"],
        abstract_extractors=["citation_abstract", "json_ld_abstract", "abstract_section"],
        pdf_link_patterns=["/doi/pdf/", "/doi/epdf/"],
        advanced_search={
            "page": "https://pubs.acs.org/search/advanced",
            "result_template": "https://pubs.acs.org/action/doSearch?AllField={query_plus}&AfterYear={year_start}&BeforeYear={year_end}",
            "fields": ["AllField", "Keyword", "AfterYear", "BeforeYear"],
        },
        default_access_route="publisher_authenticated",
    ),
    "springer": SourceAdapter(
        key="springer",
        kind="publisher_platform",
        domains=["link.springer.com"],
        landing_patterns=["https://link.springer.com/article/{DOI}"],
        identifier_types=["doi"],
        metadata_extractors=["citation_meta", "json_ld", "page_heading"],
        abstract_extractors=["citation_abstract", "json_ld_abstract", "abstract_section"],
        pdf_link_patterns=["/content/pdf/"],
        advanced_search={
            "page": "https://link.springer.com/advanced-search",
            "result_template": "https://link.springer.com/search?advancedSearch=true&sortBy=relevance&query={query_plus}&title=&contributor=&journal=&date=custom&dateFrom={year_start}&dateTo={year_end}",
            "fields": ["Keywords", "Date published", "Start year", "End year"],
        },
        default_access_route="publisher_authenticated",
    ),
    "wiley": SourceAdapter(
        key="wiley",
        kind="publisher_platform",
        domains=["onlinelibrary.wiley.com"],
        landing_patterns=["https://onlinelibrary.wiley.com/doi/{DOI}"],
        identifier_types=["doi"],
        metadata_extractors=["citation_meta", "json_ld", "page_heading"],
        abstract_extractors=["citation_abstract", "json_ld_abstract", "abstract_section"],
        pdf_link_patterns=["/doi/pdf/", "/doi/epdf/"],
        advanced_search={
            "page": "https://onlinelibrary.wiley.com/search/advanced",
            "result_template": "https://onlinelibrary.wiley.com/action/doSearch?AllField={query_plus}&AfterYear={year_start}&BeforeYear={year_end}",
            "fields": ["AllField", "Keyword", "AfterYear", "BeforeYear"],
        },
        default_access_route="publisher_authenticated",
    ),
    "science": SourceAdapter(
        key="science",
        kind="publisher_journal_platform",
        domains=["science.org", "www.science.org"],
        landing_patterns=["https://www.science.org/doi/{DOI}"],
        identifier_types=["doi"],
        metadata_extractors=["citation_meta", "json_ld", "page_heading"],
        abstract_extractors=["citation_abstract", "json_ld_abstract", "abstract_section"],
        pdf_link_patterns=["/doi/pdf/"],
        advanced_search={
            "page": "https://www.science.org/search/advanced",
            "result_template": "https://www.science.org/action/doSearch?field1=AllField&text1={query_plus}&AfterYear={year_start}&BeforeYear={year_end}",
            "fields": ["Field", "Search Term", "Publication Date"],
        },
        default_access_route="publisher_authenticated",
    ),
    "nature": SourceAdapter(
        key="nature",
        kind="publisher_journal_platform",
        domains=["nature.com", "www.nature.com"],
        landing_patterns=["https://www.nature.com/articles/{ARTICLE_ID}"],
        identifier_types=["doi"],
        metadata_extractors=["citation_meta", "json_ld", "page_heading"],
        abstract_extractors=["citation_abstract", "json_ld_abstract", "abstract_section"],
        pdf_link_patterns=[".pdf"],
        advanced_search={
            "page": "https://www.nature.com/search/advanced",
            "result_template": "https://www.nature.com/search?q={query_quote}&date_range={year_start}-{year_end}",
            "fields": ["Terms", "Publication date start year", "Publication date end year"],
        },
        default_access_route="publisher_authenticated",
    ),
}


def get_adapter(key: str) -> SourceAdapter | None:
    return REGISTRY.get((key or "").strip().lower())


def infer_source_key(url: str) -> str:
    host = urlparse(url or "").netloc.lower()
    for key, adapter in REGISTRY.items():
        if any(host == domain or host.endswith("." + domain) for domain in adapter.domains):
            return key
    return "unknown"


def enabled_targets_from_plan(plan_item: dict) -> list[SourceAdapter]:
    targets = []
    for bucket in ["source_targets", "publisher_targets"]:
        for target in plan_item.get(bucket) or []:
            if not isinstance(target, dict) or target.get("enabled") is False:
                continue
            adapter = get_adapter(str(target.get("key") or ""))
            if adapter:
                targets.append(adapter)
    seen = set()
    result = []
    for adapter in targets:
        if adapter.key not in seen:
            seen.add(adapter.key)
            result.append(adapter)
    return result


def advanced_search_url(key: str, query: str, year_start: int, year_end: int) -> str:
    adapter = get_adapter(key)
    if not adapter:
        return ""
    template = str((adapter.advanced_search or {}).get("result_template") or "")
    if not template:
        return ""
    year_csv = ",".join(str(year) for year in range(year_end, year_start - 1, -1))
    return template.format(
        query_plus=quote_plus(query),
        query_quote=quote_plus(query).replace("+", "%20"),
        year_start=year_start,
        year_end=year_end,
        year_csv=quote_plus(year_csv),
    )
