---
name: literature-loop-capture
description: Use when a user wants an OpenAlex-grounded scholarly literature review loop with OpenCLI-authenticated publisher discovery/capture, query-plan HTML review, subquestion iteration, exact-target verification, PDF/MinerU fallback, reading notes, synthesis, and LLM Wiki export.
---

# Literature Loop Capture

Use this skill to run an auditable scholarly literature loop. The normal path
is: OpenAlex metadata grounding, agent-authored query plan, user review in an
HTML page, OpenCLI discovery through an already logged-in publisher browser
profile, title/snippet/abstract screening, approved full-text capture, reading
notes, coverage review, iteration when gaps remain, and final LLM Wiki export.

This skill is publisher-login generic. It assumes the user has selected an
OpenCLI-controlled Chrome profile and logged into the publisher platforms they
are authorized to access. Scripts never handle credentials.

## Activation

One sentence from the user should be enough to start:

```text
Use literature-loop-capture to review "<topic>", years <start>-<end>,
publishers Elsevier, ACS, Wiley, Springer. Run doctor first, create the
OpenAlex-grounded query plan, open the query-plan review page in the agent
built-in browser or localhost page, stop for my approval, then continue with
OpenCLI discovery, screening, capture, coverage, and verify.
```

Treat the quoted topic as the broad review claim. Extract the year range and
supported publisher scope. Record unsupported publishers as `manual_hold`
unless the user explicitly expands the route set.

## Preconditions

- Use a project-local Python environment and install requirements.
- Run `opencli doctor`.
- Select the intended Chrome profile with `opencli profile use <name>` when
  more than one profile exists.
- Log into needed publisher sites in that Chrome profile before discovery or
  capture.
- Set `OPENALEX_API_KEY` for normal query-plan grounding.
- Set `MINERU_API_KEY` only when normalizing user-supplied PDFs.
- Keep API keys in the shell or a local secret manager, never in tracked files.
- Supported structured publisher routes are direct publisher URLs for
  ScienceDirect/Elsevier, ACS, Wiley, and Springer.

Default OpenCLI sessions are generic:

- `lit` for normal orchestration
- `lit-preview` for abstract preview
- `lit-capture` for article capture

Common blocker/status language:

- `publisher_login_required`
- `publisher_auth_blocked`
- `publisher_robot_blocked`
- `publisher_page_unavailable`
- `publisher_route_unsupported`
- `manual_hold`
- `metadata_blocked`

## Primary Commands

Prefer the loop orchestrator as the control plane:

```bash
.venv/bin/python literature-loop-capture/scripts/literature_loop.py doctor
.venv/bin/python literature-loop-capture/scripts/literature_loop.py plan "Review <research topic>, years <start>-<end>, publishers Elsevier, ACS, Wiley, Springer." --rounds 3 --year-start <start> --year-end <end>
.venv/bin/python literature-loop-capture/scripts/literature_loop.py review-plan "LiteratureCaptures/<run-folder>"
.venv/bin/python literature-loop-capture/scripts/literature_loop.py resume "LiteratureCaptures/<run-folder>"
.venv/bin/python literature-loop-capture/scripts/literature_loop.py verify "LiteratureCaptures/<run-folder>"
```

The orchestrator writes `loop-state.json`, `loop-run-log.jsonl`, and `STATE.md`
inside each run folder. It is assisted, not unattended: it stops at query-plan
approval, query-refinement, reading-note/coverage, overview, publisher login
blockers, attempt caps, and budget caps. It records only API-key presence, not
secret values.

## Query Plan Review

After `plan` succeeds, create the browser-review artifact:

```bash
.venv/bin/python literature-loop-capture/scripts/literature_loop.py review-plan "LiteratureCaptures/<run-folder>"
```

Serve the run directory and open `query-plan-review.html` in the agent's
built-in browser or a localhost review page, not the OpenCLI Chrome profile:

```bash
python3 -m http.server 8765 --bind 127.0.0.1 --directory "LiteratureCaptures/<run-folder>"
```

Open:

```text
http://127.0.0.1:8765/query-plan-review.html
```

Do not open query-plan review pages in the user's publisher-login Chrome
profile. That profile should remain reserved for OpenCLI publisher sessions.

Use `review-plan` to record comments against a subquestion, query family, or
plan section. `--severity note` is informational. `--severity correction`
blocks approval until it is resolved:

```bash
.venv/bin/python literature-loop-capture/scripts/literature_loop.py review-plan "LiteratureCaptures/<run-folder>" --target sq-01 --severity correction --note "This subquestion is too broad; split resources from methods."
.venv/bin/python literature-loop-capture/scripts/literature_loop.py review-plan "LiteratureCaptures/<run-folder>" --resolve qpr-001 --resolution "Split sq-01 into resource and method subquestions."
```

When the user rejects or questions a plan element, do not revise from intuition
alone. Record the comment as a `correction`, run targeted OpenAlex-only
grounding around that target, inspect returned titles, abstracts, topics,
keywords, venues, DOI/year/citation signals, then update
`agent-query-plan.json`. Re-run validation, URL building, and `review-plan`.
Refresh the browser page and explain which OpenAlex seeds, gaps, or missing
concepts caused the revision.

## Full Loop Order

Run the flow in this order:

```text
research question or claim
-> translate/normalize the question into English
-> broad OpenAlex metadata grounding + agent concept synthesis
-> grounding-notes.md + exploration-sources.csv/json + agent-query-plan-packet.md
-> agent-authored agent-query-plan.json
-> validated query-plan-preview.md/json
-> query-plan-review.html opened for user approval
-> OpenCLI publisher discovery for approved query-plan rows only
-> discovery-audit.csv/json with result-page titles, snippets, and abstracts when available
-> discovery dedup marks candidates already captured or seen-before
-> query-refinement reviews publisher search-page titles/snippets/context only
-> abstract preview reuses saved search-page abstracts, then OpenAlex metadata, then publisher detail page when needed
-> abstract_capture_review combines abstract preview with earlier title/snippet/context
-> apply_query_decisions.py writes next-simple-queries.csv and capture-queue.csv
-> capture_decision_queue.py captures only agent-approved article URLs
-> each captured article lands under subquestions/<group>/<id>/sources/<source>/articles/<article>
-> run-summary.csv/json/md
-> responsible subquestion agent writes reading-note-zh.md
-> reading notes extract high-value seeds, reference candidates, gaps, and next query actions
-> subquestion_coverage_review.py writes coverage-review/subquestion-coverage-review.*
-> responsible subquestion agent decides sufficient/iterate/stop_with_gaps/blocked
-> if iterate_query: generate query_iteration_plan and query-plan-amendment.json
-> open iteration review page for user comments/corrections
-> after approval, repeat only approved broad_discovery rows for non-terminal subquestions
-> terminal subquestions stop broad discovery but keep exact targets and references for follow-up
-> after all primary subquestions are terminal, aggregate verified exact targets and recommended references
-> route supported follow-up captures, manual holds, and user-supplied PDFs
-> build/refresh _knowledge/
-> normalize user-supplied PDFs with MinerU API when needed
-> update reading notes, coverage review, and subquestion summaries
-> main agent writes overview.md from all summaries and article notes
-> llm_wiki_export.py writes lightweight wiki pages plus raw source article.md files
```

## Loop Contract

Treat each atomic subquestion as a closed loop with explicit state, not as a
linear checklist. The responsible subquestion agent owns the loop until the
subquestion is marked:

- `sufficient`
- `stop_with_gaps`
- `blocked`

Keep two phases separate:

- Primary query loop: runs only for non-terminal subquestions. Its only
  executable input for iteration 2+ is approved `broad_discovery` rows. These
  rows restart discovery -> abstract preview -> capture -> reading note ->
  coverage review.
- Terminal follow-up: starts only after every primary subquestion is terminal.
  It aggregates all rounds' agent-verified exact targets, seed ledgers,
  reading-note recommended references, and manual-hold ledgers, then routes
  supported captures, supplemental PDFs, and user-supplied PDFs before final
  overview.

Terminal subquestions are excluded from later broad query plans, broad search
queues, and primary coverage scoring unless the user explicitly reopens that
subquestion. Their exact targets and recommended references remain available
for terminal follow-up.

Across rounds, merge ledgers by subquestion rather than replacing them. Dedup
by DOI, OpenAlex ID, arXiv ID, normalized title, and, when needed, normalized
target/resource name. Preserve provenance fields showing which round, article,
reference index, note, gap, or user comment produced the row.

## Evidence Maturity Gate

`rcs_0_to_10` is staged relevance triage only. It is used differently before
and after abstract preview:

- In `search_page_triage`, RCS is deliberately lenient and can choose abstract
  preview, query iteration, or branch stop. Even RCS 8-10 cannot approve
  full-text capture from title/snippet/search-page evidence alone.
- In `abstract_capture_review`, the agent combines the abstract preview with
  earlier title/snippet/context and uses stricter RCS to approve capture,
  iterate, or stop.

RCS interpretation:

- 0-1: off-topic
- 2-3: tangential
- 4-5: partial
- 6-7: highly relevant
- 8-10: foundational or seminal

A high RCS means a candidate is worth screening or capture at the current
evidence stage. It does not mean the subquestion is covered. Title, snippet,
and abstract evidence can never prove subquestion sufficiency. Only captured
full text, purpose-specific reading notes, typed seed ledger review,
reference/gap review, and `coverage_score_0_to_5` can support
`coverage_decision=sufficient`.

If `coverage_score_0_to_5` is around 4.0 or the agent records important gaps,
high-value seeds, or unresolved reference leads, set
`coverage_stage_status=needs_iteration_review`. In that state the loop is not
terminal. Build an iteration review page, open it in the agent browser, and
wait for user approval or correction before launching the next OpenCLI
discovery round.

## Query Iteration Gate

Iteration 2+ query planning has a mandatory reasoning gate before any Python
amendment is generated. The responsible subquestion agent, or the main agent
in an explicitly recorded fallback, must first write:

```text
loop-state/<subquestion>/iteration-NN/query-rationale-review.md
loop-state/<subquestion>/iteration-NN/query-rationale-review.json
```

The rationale review must explain, for each proposed next query:

- which seed or gap it probes
- why that evidence is not already covered by captured full text
- whether the query is an exact target or a broad discovery query
- why it is expected to retrieve useful new evidence

Python scripts may not infer or optimize next queries from raw gap prose on
their own. They may validate, deduplicate, route, export, and visualize queries
already present in the rationale review or explicit user corrections.

From iteration 2 onward, query planning must split next work into two buckets:

- `broad_discovery`: short English queries that can enter publisher result-page
  discovery after user approval.
- `exact_openalex_target`: exact article titles, resource names, method names,
  database names, benchmark names, ontology names, or high-value paper names.
  These are verified through OpenAlex and routed only after agent approval.

Only `broad_discovery` rows can launch new publisher searches. Exact targets
never become broad discovery queries automatically.

## Exact Targets

Exact targets come from:

- reading notes
- high-value seeds
- selected references
- coverage gaps
- query rationale
- user corrections
- repeated named databases, resources, methods, benchmarks, ontologies, or
  paper titles within a subquestion

Python must not turn a short or polysemous label into an OpenAlex capture
target by itself. This is especially important for names that are also common
words, abbreviations, product names, organisms, foods, materials, or multiple
unrelated resources.

For every exact target, the rationale review must record:

- `original_seed_text`
- `agent_verified_exact_query`
- `entity_type` (`paper`, `database`, `resource`, `method`, `benchmark`,
  `ontology`, or `dataset`)
- `expected_title_terms`
- `required_context_terms`
- known DOI/authors/venue when available
- `negative_senses`
- `openalex_verification_status`

After OpenAlex returns candidates, the agent must verify the chosen candidate's
title, DOI, venue, year, authors, abstract, topics, and context against the
target. A row may enter a capture queue only when it records:

```text
agent_openalex_verified=true
```

If verification is ambiguous, route it to `manual_hold` or ask the user for a
clarifying target. Do not capture it.

## Publisher Discovery And Capture

Use direct publisher URLs for structured routes:

- ScienceDirect/Elsevier
- ACS
- Wiley
- Springer

Use `references/publisher-direct-url-patterns.md` for URL details. Do not use
public repository PDF/XML routes as the default path for these publishers.
Open article pages through OpenCLI using the logged-in browser profile and
capture HTML full text, section structure, figures, and tables when available.

Capture only after agent approval. Do not do first-N full-text capture for new
runs unless the user explicitly requests bounded debugging.

When a publisher page asks for login, shows an authorization block, trips a
robot/CAPTCHA page, or is unavailable, record the blocker. Do not bypass it.
Use the generic statuses:

- `publisher_login_required`
- `publisher_auth_blocked`
- `publisher_robot_blocked`
- `publisher_page_unavailable`

Unsupported publishers remain `manual_hold` until the route set is extended.

## Reading Notes

Python may extract and route. It must not write the final intellectual note.
The responsible agent writes `reading-note-zh.md` from captured full text,
figures, tables, methods, results, limitations, and references.

Use a multi-pass reading process:

1. First pass: title, abstract, introduction, headings, conclusion, and
   references. Identify the paper category, research problem, background,
   contribution, article structure, and relevance to the subquestion.
2. Second pass: inspect figures, diagrams, tables, methods, data, experiments,
   evidence, and limitations.
3. Third pass for important papers: challenge assumptions, reconstruct the
   argument, record missing citations or weak methods, and extract reusable
   ideas for the current subquestion.

Every reading note should support:

- high-value seeds
- reference candidates
- remaining gaps
- next broad discovery queries, if needed
- exact target candidates, if justified

## References And Terminal Follow-up

Reference follow-up starts after primary subquestions are terminal. Python may
dedupe, route, and build queues, but the reading-note recommendation is the
intellectual approval.

For each subquestion:

1. Rank cited references from captured papers.
2. Let the responsible agent select the final high-value references.
3. Route supported references to direct publisher capture.
4. Put unsupported or ambiguous references on manual hold.
5. Do not run deeper reference chasing unless the user explicitly requests a
   bounded deeper pass.

Supported follow-up captures land under:

```text
subquestions/<group>/<id>/references/<source>/articles/<article>
```

Use `capture_depth=2` for these captures.

## PDF And MinerU Fallback

The default path for the four core publishers is webpage full text capture, not
batch PDF download. Webpages usually preserve heading hierarchy, abstract,
figure captions, tables, and references in a form that is easier for agents to
read and extract. Batch PDF downloading is more likely to hit download limits,
CAPTCHA, or temporary blocking. Treat PDFs as supplemental.

Use PDFs when:

- publisher webpage capture cannot obtain full text
- the article is only practical through PDF
- the user manually supplies a PDF
- a terminal follow-up reference requires manual evidence

PDF handoff:

1. Build or refresh `_knowledge/`.
2. Put user-supplied PDFs in:

   ```text
   _knowledge/subquestions/<id>/manual_pdf_dropbox/
   ```

3. Sync PDFs into canonical article folders.
4. Normalize with MinerU API.
5. Continue reading notes and coverage review from normalized Markdown.

Do not copy PDFs into the LLM Wiki export when normalized Markdown already
exists.

This skill does not include external batch PDF retrieval. If the user chooses
to use an outside route such as `Rimagination/scansci-pdf`, treat the resulting
PDFs as user-supplied files and route them through the same `_knowledge` and
MinerU API normalization path.

## Knowledge Staging

Use `_knowledge/` as the human-facing information layer. It should expose:

- canonical captured papers
- reading notes
- important seeds
- recommended references
- query/search journeys
- coverage artifacts
- DOI-backed manual PDF dropboxes grouped by subquestion

`manual_pdf_to_download.csv` is the short action list.
`manual_pdf_download_list.csv` is the full DOI-backed audit list.
Rows without DOI stay in metadata-blocked audit files.

After new PDFs are normalized or new reference captures are completed, refresh
`_knowledge/` so the new articles, notes, figures, tables, seeds, and
references appear beside earlier captured evidence.

## Overview Synthesis

`overview.md` is written by the main agent after reading:

- all `subquestion-summary-zh.md` files
- all reading notes
- run summary files
- coverage reviews
- seed/reference/gap ledgers
- manual hold ledgers

It should synthesize the user's main question, not merely concatenate
subquestion summaries. Include:

- current state of the problem
- major method/resource/evidence families
- new methods and emerging directions
- unresolved limitations and bottlenecks
- concrete next capture or research opportunities

## LLM Wiki Export

After `overview.md`, subquestion summaries, reading notes, and coverage gates
are complete, run:

```bash
.venv/bin/python literature-loop-capture/scripts/llm_wiki_export.py "LiteratureCaptures/<run-folder>" --output-root "<export-root>"
```

The export keeps `wiki/` lightweight:

- `overview.md`
- `index.md`
- `log.md`
- `subquestions/`
- `queries/`
- `ledgers/`

Article-level source pages are generated by LLM Wiki ingest from:

```text
raw/sources/articles/**/article.md
```

Figures and tables live under `raw/assets/`. Source PDFs stay in canonical
capture folders because normalized Markdown is the LLM Wiki ingest source.

## Validation

Before claiming the workflow is complete for a run:

```bash
.venv/bin/python literature-loop-capture/scripts/literature_loop.py verify "LiteratureCaptures/<run-folder>"
```

For package validation:

```bash
.venv/bin/python -m unittest discover literature-loop-capture/tests -v
.venv/bin/python /Users/liuya/.codex/skills/.system/skill-creator/scripts/quick_validate.py literature-loop-capture
```

The public package must not contain private institutional access terms. If a
forbidden-string test fails, remove the term from public docs/scripts/tests.

## References

- Direct publisher URL details: `references/publisher-direct-url-patterns.md`
