# Publisher Direct URL Patterns

Use direct publisher URLs in the OpenCLI-controlled Chrome profile. The user is
responsible for logging into each publisher or institution-supported account in
that profile before discovery or capture.

## ScienceDirect / Elsevier

- Search:
  `https://www.sciencedirect.com/search?qs=sample+query&date=2021-2026&articleTypes=FLA%2CREV`
- Pagination:
  `offset=25`, `offset=50`, `offset=75`
- Article:
  `https://www.sciencedirect.com/science/article/pii/<PII>`

## ACS

- Search:
  `https://pubs.acs.org/action/doSearch?field1=AllField&text1=sample+query&AfterYear=2021&BeforeYear=2026&pageSize=20`
- Pagination:
  first page has no `startPage`; later pages use `startPage=1`,
  `startPage=2`, with `pageSize=20`.
- Article:
  `https://pubs.acs.org/doi/<DOI>`

## Wiley

- Search:
  `https://onlinelibrary.wiley.com/action/doSearch?field1=AllField&text1=sample+query&AfterYear=2021&BeforeYear=2026&pageSize=20`
- Pagination:
  first page has no `startPage`; later pages use `startPage=1`,
  `startPage=2`, with `pageSize=20`.
- Article:
  `https://onlinelibrary.wiley.com/doi/<DOI>`

## Springer

- Search:
  `https://link.springer.com/search?advancedSearch=true&sortBy=relevance&query=sample+query&date=custom&dateFrom=2021&dateTo=2026`
- Pagination:
  `page=2`, `page=3`, and so on.
- Article:
  `https://link.springer.com/article/<DOI>`,
  `https://link.springer.com/chapter/<DOI>`, or
  `https://link.springer.com/protocol/<DOI>`.

## Screening Principle

Publisher result pages are lead sources only. A candidate needs title/snippet
triage, abstract preview, and explicit capture approval before full-text
capture.
