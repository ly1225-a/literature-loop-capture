---
name: literature-loop-capture
description: Use when a user wants OpenAlex-grounded scholarly literature discovery, OpenCLI publisher-search screening through an already logged-in Chrome profile, structured full-text capture for Elsevier/ScienceDirect, ACS, Wiley, and Springer, subquestion-specific query iteration, Crossref metadata reconciliation, overview synthesis, supplemental PDF/MinerU fallback, and LLM Wiki project export.
---

# Literature Loop Capture

Use this skill to run an evidence-grounded literature capture workflow with
OpenAlex-grounded query planning, automated OpenCLI publisher-search
screening, and full-text capture. Discovery and full-text capture both use the
already-authenticated visible connected Chrome/OpenCLI profile and write interruption-safe
artifacts. ScienceDirect/Elsevier, ACS, Wiley, and Springer are the supported
structured full-text routes. Other purchased publisher resources are recorded
as `manual_hold` until the user explicitly expands the supported route set.

## Workflow

Prefer the L2-assisted loop orchestrator on macOS validation runs. It is the
single control plane for `doctor`, `plan`, `run`, `resume`, `status`, and
`verify`, while the older stage scripts remain the underlying executors:

```bash
.venv/bin/python literature-loop-capture/scripts/literature_loop.py doctor
.venv/bin/python literature-loop-capture/scripts/literature_loop.py plan "Review <research topic>, years <start>-<end>, publishers Elsevier, ACS, Wiley, Springer." --rounds 3 --year-start <start> --year-end <end>
.venv/bin/python literature-loop-capture/scripts/literature_loop.py review-plan "LiteratureCaptures/<run-folder>"
.venv/bin/python literature-loop-capture/scripts/literature_loop.py resume "LiteratureCaptures/<run-folder>"
.venv/bin/python literature-loop-capture/scripts/literature_loop.py verify "LiteratureCaptures/<run-folder>"
```

One-sentence activation should be enough in a fresh agent conversation:

```text
Use literature-loop-capture to review "<topic>", years <start>-<end>, publishers Elsevier, ACS, Wiley, Springer. Run doctor first, create the OpenAlex-grounded query plan, open the query-plan review page in the agent built-in browser or localhost review page, stop for my approval, then continue with OpenCLI discovery, subagent/RCS screening, capture, coverage, and verify.
```

When a user provides that sentence, treat the quoted topic as the broad review
claim, extract the supported publisher scope, record unsupported publishers as
`manual_hold`, and begin with `literature_loop.py doctor` then
`literature_loop.py plan "<original request>"`. Do not require the user to
manually prepare `--claim` unless they ask for exact CLI control.

Year limits in the user's sentence are valid input, but they are structure, not
query language. If the request says `years 2021-2026`, `year 2021 to 2026`, or
similar, extract that into `--year-start 2021 --year-end 2026` and remove the
`year(s)` words from the English claim and all publisher query text. A query
such as `machine learning battery recycling years` is invalid; year filtering belongs
only in year fields and publisher URL parameters.

After `plan` succeeds, write the browser-review artifact before asking for
approval:

```bash
.venv/bin/python literature-loop-capture/scripts/literature_loop.py review-plan "LiteratureCaptures/<run-folder>"
```

Then open `query-plan-review.html` in the agent built-in browser or localhost
review page, not the user's Google Chrome/OpenCLI profile. Do this by serving
the run directory on localhost and opening the localhost URL in the agent
browser:

```bash
python3 -m http.server 8765 --bind 127.0.0.1 --directory "LiteratureCaptures/<run-folder>"
```

Open `http://127.0.0.1:8765/query-plan-review.html` in the agent built-in
browser. Do not use `review-plan --open`, `open`, or Python `webbrowser` for
query-plan review pages, because those can open the user's Google Chrome and
interfere with the authenticated OpenCLI publisher browser state.

Use `review-plan` to record user comments against a subquestion, query family,
or plan section. `--severity note` is informational. `--severity correction`
blocks `approve query_plan_approval` until it is resolved:

```bash
.venv/bin/python literature-loop-capture/scripts/literature_loop.py review-plan "LiteratureCaptures/<run-folder>" --target sq-01 --severity correction --note "This subquestion bundles two evidence needs; split them into separate agent-justified subquestions."
.venv/bin/python literature-loop-capture/scripts/literature_loop.py review-plan "LiteratureCaptures/<run-folder>" --resolve qpr-001 --resolution "Split sq-01 into two evidence-specific subquestions grounded in the revised OpenAlex packet."
```

When the user rejects or questions a plan element, do not revise the query plan
from intuition alone. Record the comment as a `correction`, run a targeted
OpenAlex-only grounding pass around that target, inspect the returned titles,
abstracts, topics, keywords, venues, DOI/year/citation signals, then update
`agent-query-plan.json`. Re-run `validate_agent_query_plan.py`,
`build_publisher_urls.py`, and `review-plan`, then refresh the agent browser
page. The refreshed review page should explain which OpenAlex seeds,
gaps, or missing concepts caused the revision.

The orchestrator writes `loop-state.json`, `loop-run-log.jsonl`, and `STATE.md`
inside each run folder. It is L2-assisted, not unattended: it stops at query-plan
approval, query-refinement, reading-note/coverage, overview, publisher login/auth
blockers, attempt caps, and budget caps. It records only API-key presence, never
secret values.

Run the flow in this order:

```text
research question or claim
-> translate/normalize the question into English
-> broad OpenAlex metadata grounding + agent concept synthesis
-> grounding-notes.md + exploration-sources.csv/json + agent-query-plan-packet.md
-> agent-authored agent-query-plan.json + validated query-plan-preview.md/json
-> user approval of English subquestions and query rounds
-> OpenCLI publisher discovery for approved query-plan rows only
-> discovery-audit.csv/json with result-page titles, snippets, and abstracts when available
-> discovery dedup marks candidates as already captured or seen-before before query-refinement and abstract screening
-> query-refinement subagent reviews publisher search-page titles/snippets/context only; saved search-page abstract text is hidden until abstract preview
-> subagent requests abstract previews, query iteration, or stop/move-on; it must not approve full-text capture from search-page evidence alone
-> abstract_preview.py reuses saved search-page abstracts, otherwise checks OpenAlex metadata first, and opens the queued publisher detail URL through OpenCLI only when metadata still cannot provide an abstract
-> subagent reviews abstract previews together with the earlier title/snippet evidence, then uses stricter RCS to choose capture/iterate/stop
-> apply_query_decisions.py writes next-simple-queries.csv and capture-queue.csv
-> capture_decision_queue.py captures only agent-approved article URLs, interleaving publishers one article at a time instead of batch-opening many pages from the same publisher
-> each captured article immediately becomes subquestions/<group>/<id>/sources/<source>/articles/<article>
-> run-summary.csv/json/md
-> one subquestion agent per subquestions/<group>/<id>/agent-brief.md writes reading-note-zh.md
-> reading notes extract high-value seeds, reference candidates, gaps, and next query actions
-> subquestion_coverage_review.py writes coverage-review/subquestion-coverage-review.* from full text, notes, seeds, and gaps
-> subquestion agent decides sufficient/iterate/stop_with_gaps for the whole subquestion
-> if iterate_query: the responsible agent first writes query-rationale-review.json from the seed ledger, cumulative full-text evidence, missing evidence, unresolved references, gaps, and user review; Python then validates/routes that rationale into query_iteration_plan and query-plan-amendment.json; open it with review-plan for user comments/corrections; wait for query_iteration_plan_approval
-> after approval, repeat only the approved broad_discovery rows for non-terminal subquestions through OpenCLI publisher discovery, title/snippet/context triage, abstract preview from saved abstracts then OpenAlex then OpenCLI detail pages for unresolved abstracts, capture decision, full-text capture, reading notes, seed/reference/gap extraction, and coverage review using cumulative evidence from all prior rounds
-> for worth-close-reading primary articles, the subagent writes recommended-references.csv/json/md but does not trigger reference capture yet
-> exact targets, high-value seeds, recommended references, and manual holds accumulate in cumulative ledgers by subquestion while any primary subquestion remains non-terminal
-> after every primary subquestion is sufficient, stop_with_gaps, or blocked, Python aggregates only agent-verified exact-target ledgers and references already recommended in reading notes, dedupes them, checks DOI and bibliographic metadata with Crossref when needed, and writes the reference/supplemental ledgers and capture queues
-> the earlier reading-note recommendation is the intellectual approval; the reference-follow-up stage only routes supported captures, manual holds, metadata mismatches, and unsupported publishers
-> supported references are then captured in one centralized reference-follow-up phase under subquestions/<group>/<id>/references/<source>/articles/<article> with capture_depth=2
-> unsupported or non-four-publisher references are written to manual-reference-hold.csv/json or manual-pdf-hold.csv/json for human capture
-> knowledge_staging.py builds/refreshes _knowledge/ as the human-facing information layer: canonical captured papers, reading notes, important seeds, recommended references, query/search journeys, coverage, and DOI-backed manual PDF dropboxes grouped by subquestion
-> the user places manual PDFs only in _knowledge/subquestions/<id>/manual_pdf_dropbox/; manual_pdf_to_download.csv is the short action list, manual_pdf_download_list.csv is the full DOI-backed audit list, and rows without DOI stay in metadata-blocked audit files
-> ingest/sync newly supplied source.pdf files into their canonical references/pdf/manual article folders
-> MinerU API normalizes newly supplied PDFs into the same article structure used by publisher captures
-> reference captures receive reading-note-zh.md
-> refresh _knowledge/ so the newly normalized PDF articles, notes, figures, tables, seeds, and references appear beside earlier captured evidence
-> rerun/update cumulative coverage review and subquestion summaries with primary, exact-target, reference, supplemental PDF, and manual-blocker evidence
-> subagent writes subquestion-summary-zh.md
-> main agent verifies completion gates
-> main agent writes overview.md from all subquestion summaries and article notes
-> optional organize_outputs.py creates a short-name export copy
-> optional llm_wiki_export.py writes _knowledge plus final overview/summaries, captured article ingest files, reading notes embedded in article.md, copied figures/tables, query history, and lightweight wiki navigation/process pages as a LLM Wiki project export; source PDFs stay in canonical capture folders because normalized Markdown is the LLM Wiki ingest source
```

## Loop Contract

Treat each atomic subquestion as a closed loop with explicit state, not as a
linear checklist. The responsible subquestion agent owns the loop until the
subquestion is marked `sufficient`, `stop_with_gaps`, or `blocked`.

### Primary Loop vs Terminal Follow-up Gate

Keep two phases separate:

- Primary query loop: runs only for subquestions that are not terminal. Its only
  executable input for iteration 2+ is approved `broad_discovery` rows. These
  rows restart the normal discovery -> abstract preview -> capture -> reading
  note -> coverage cycle.
- Terminal follow-up: starts only after every primary subquestion is terminal.
  It aggregates all rounds' agent-verified exact OpenAlex targets, seed
  ledgers, reading-note recommended references, and manual-hold ledgers, then
  routes supported captures, supplemental PDFs, and user-supplied PDFs before
  final overview.

Terminal subquestions are excluded from later broad query plans, later broad
search queues, and later primary coverage scoring unless the user explicitly
reopens that subquestion. Their exact targets and recommended references remain
available for the terminal follow-up phase.

Across rounds, merge ledgers by subquestion rather than replacing them. Dedup by
DOI, OpenAlex ID, arXiv ID, normalized title, and, when needed, normalized
target/resource name. Preserve provenance fields showing which round, article,
reference index, note, gap, or user comment produced the row.

## Strict Evidence Maturity Gate

`rcs_0_to_10 is staged relevance triage only`: it is used differently before
and after abstract preview. In `search_page_triage`, RCS is deliberately
lenient and chooses abstract preview, query iteration, or branch stop; even RCS
8-10 cannot approve full-text capture from title/snippet/search-page evidence
alone. In `abstract_capture_review`, the subagent combines the abstract preview
with the earlier title/snippet/context and uses stricter RCS to approve capture,
iterate, or stop. RCS 0-1 is off-topic, 2-3 tangential, 4-5 partial, 6-7 highly
relevant, and 8-10 foundational or seminal. A high RCS means a candidate is
worth screening or capture at the current evidence stage; it does not mean the
subquestion is covered. Title/snippet/abstract evidence can never prove
subquestion sufficiency. Only captured full text, purpose-specific reading
notes, typed seed ledger review, reference/gap review, and
`coverage_score_0_to_5` can support `coverage_decision=sufficient`.

`coverage_decision=sufficient` is not always final. If
`coverage_score_0_to_5` is exactly around 4.0 or the subagent records important
gaps, high-value seeds, or unresolved reference leads, the coverage artifact
must also set `coverage_stage_status=needs_iteration_review`. In that state the
loop is not terminal. Build
`loop-state/<subquestion>/iteration-NN/iteration-review.html`, open it in the
agent built-in browser or localhost review page, and wait for user approval or correction before launching
the next OpenCLI discovery round.

The iteration review page must show previous round evidence, captured articles,
remaining gaps, high-value seeds, proposed short queries, and duplicates
already seen or captured. From iteration 2 onward, query planning must split
next work into two explicitly labeled buckets:

Iteration 2 and later query planning has a mandatory reasoning gate before any
Python amendment is generated. The responsible subquestion agent, or the
main agent in an explicitly recorded fallback, must first write
`loop-state/<subquestion>/iteration-NN/query-rationale-review.md` and
`query-rationale-review.json` from the local reading notes, coverage gaps,
seed ledger, reference marks, and prior query outcomes. This rationale review
must explain, for each proposed next query, which seed or gap it probes, why
that evidence is not already covered by captured full text, whether the query
is an exact target or a broad discovery query, and why it is expected to
retrieve useful new evidence. Python scripts may not infer, summarize, or
optimize next queries from raw gap prose on their own. They may only validate,
deduplicate, route, export, and visualize queries that are already present in
the rationale review or explicit user corrections.

- `exact_openalex_target`: unlimited exact article titles, resource names,
  method names, database names, benchmark names, or high-value paper names from
  reading notes, reference picks, seed ledgers, gaps, and user comments. The
  responsible subagent/main agent must disambiguate and approve the exact
  OpenAlex query before any script sends it to OpenAlex. For every exact
  target, the rationale review must record `original_seed_text`,
  `agent_verified_exact_query`, `entity_type` (`paper`, `database`,
  `resource`, `method`, `benchmark`, `ontology`, or `dataset`),
  `expected_title_terms`, `required_context_terms`, known DOI/authors/venue
  when available, `negative_senses`, and
  `openalex_verification_status`. Short or polysemous labels are not valid
  exact queries by themselves when they are also common words, abbreviations, or
  multiple resources; expand them with title terms, resource/database context,
  DOI, citation text, author, or venue evidence first. After OpenAlex returns
  candidates, the subagent/main agent must verify the chosen candidate's title,
  abstract/topic terms, DOI/year/venue/publisher, and authors against those
  anchors. Token overlap alone is not a match. If the candidate is a wrong
  sense, generic word match, outside the anchors, outside the year/route scope,
  or otherwise ambiguous, mark it `semantic_mismatch` or
  `needs_agent_disambiguation`, keep it in manual/rejected audit ledgers, and
  do not route it to capture. Python may fetch OpenAlex candidates and record
  metadata, but it must not choose the semantic match for an ambiguous exact
  target or promote rows unless `agent_openalex_verified=true`.
  Agent-verified exact targets are recorded with
  title/DOI/year/venue/publisher and routed only to the OpenAlex-detected
  structured publisher route (Elsevier/ScienceDirect, ACS, Wiley, Springer).
  Do not run an exact target against all publishers by default. If the verified
  OpenAlex work has an unsupported publisher, no publisher, no match, or a
  route outside the current scope, write it to the iteration `manual.csv`
  instead of the capture plan. Exact targets must be atomic, but atomic does
  not mean short. A single bibliographic reference, citation string,
  DOI-bearing reference, or one paper title may remain a long exact target
  because it identifies one work. Split only rows that bundle several named
  things into one seed: table summaries, comma- or slash-separated lists of
  multiple databases, methods, benchmarks, resources, or multiple paper
  titles/references. Mark the bundled row `needs_atomic_split`, split it into
  one rationale-reviewed exact target per named item, and carry the original
  row as provenance. Python may mechanically flag likely bundled rows, but the
  subagent/main-agent rationale review or explicit user correction owns the
  split, the disambiguation anchors, and the OpenAlex match verification, and
  must check overlap with already captured article titles and local
  reading-note evidence before adding the atomic targets to the verified
  ledger.
  Review pages and machine queues must not present a bundled source row as an
  OpenAlex query after it has been split. Preserve bundled rows as provenance
  in the JSON/CSV audit trail or inside the relevant atomic target's expandable
  details when useful, but do not render a separate large "Bundled Source
  Provenance" section on the query-review page. The review page should stay
  focused on broad discovery queries and the exact-target ledger that will
  later be re-grounded in OpenAlex. That ledger should contain only atomic named
  targets and single-work long references/titles.
- `broad_discovery`: bounded short keyword queries, normally 2-4 words, that
  can match multiple papers and are suitable for the default supported
  publisher search pages.

Only `broad_discovery` rows are executable by default in later OpenCLI
discovery rounds. `exact_openalex_target` rows are evidence ledgers: keep their
OpenAlex title/DOI/year/venue/publisher/route metadata and manual-hold status,
but do not add them to the automatic search plan unless the user explicitly
requests targeted manual capture for a named item.

The query-iteration loop continues only while a primary subquestion is not
terminal and the user-approved iteration budget remains. A subquestion is
terminal only when the coverage gate records `coverage_decision=sufficient`
without `coverage_stage_status=needs_iteration_review`, or records
`stop_with_gaps`/`blocked` with the remaining gaps or blocker. If the gate
records `coverage_decision=iterate_query` or
`coverage_stage_status=needs_iteration_review`, the only items that flow into
the next loop are the approved `broad_discovery` queries. Those broad queries
restart the full evidence cycle: discovery, title/snippet triage, abstract
preview, capture decision, full-text capture or blocker, reading notes,
seed/reference/gap extraction, and coverage scoring. Exact OpenAlex targets,
manual holds, and recommended references stay in their ledgers during this
loop; they are not searched as broad publisher queries and do not satisfy
coverage by themselves.

After all primary subquestions are terminal, the flow changes. At that point,
the exact-target ledger and agent-picked recommended references are aggregated
for reference follow-up and supplemental PDF follow-up. They do not re-enter
the primary query-iteration loop unless the user explicitly reopens a
subquestion and approves another broad discovery round.

`manual_hold` and `manual_pdf_hold` are residual routing states, not quality
judgments. A manual row may mean unsupported publisher, missing/uncertain DOI,
metadata mismatch, access blocker, low-confidence route, duplicate provenance,
or a target that requires the user's PDF. It does not mean the row is
irrelevant, unreviewed, or automatically required for download. Before asking
the user to download anything, subtract rows already captured or normalized by
another route, duplicate DOI/title/source rows, and obvious low-value or
metadata-mismatched rows; present only the remaining needed manual PDFs, with
optional and skip/already-captured rows separated.

For iteration 2 and later, prior query-plan phrases are style/context only.
They may be shown in the review page's "More details" panel to explain the
earlier search space, but they must not be copied verbatim, used as fallback
queries, or used to fill the executable `broad_discovery` queue. If the current
seed/gap/reference evidence yields fewer than the nominal broad-query limit,
show fewer queries instead of padding with previous-round phrases.

Before writing the next executable broad queue, normalize short queries and
remove duplicates by exact text and by token-set equivalence. For example,
`term method evaluation` and `method evaluation term` are the same search
intent and only one may be queued. This duplicate filter also applies against
previous-round broad queries so a word-order variant of an already searched
phrase is not sent to OpenCLI again.

Reject mechanically invalid broad phrases: non-English text, explanatory gap
sentences, operation/access terms, publisher/file artifacts, exact-looking
single resources or paper titles, and fragments that were created only by
truncating prose. A valid broad query must be an explicit short query proposed
by the query-rationale review or an explicit user correction, with a recorded
relationship to the seed or gap it is meant to probe. Do not encode
review-topic-specific allowlists or denylists in this common skill or its
scripts. If query quality is uncertain, keep the item visible for review/manual
follow-up instead of silently converting it into an executable publisher
search.

Do not turn operational resource-audit needs such as download, API, license,
access, endpoint, GitHub/code, or supplementary-file checks into publisher
literature queries. Preserve those as gap/manual audit evidence, and if a
useful exact resource name remains after removing operational terms, ground the
resource name itself in OpenAlex.

Every subquestion prompt must include `subquestion_reading_lens`. The lens
turns a common query family into a reading purpose: resource subquestions read
for databases, schema/entity/relation types, coverage, and reusable resources;
method subquestions read for workflows and algorithms; evaluation subquestions
read for metrics, validation, baselines, and failure cases. Do not let a
subagent write a generic paper summary when the subquestion asks for a specific
evidence layer.

Reading notes must feed seed-driven iteration. Each note should update a typed
seed ledger containing named resources, datasets/benchmarks,
methods/models/workflows, evaluation terms/metrics, cited seed papers,
gaps/blockers, and proposed next simple queries. A high-value seed either gets
searched in a later iteration, is queued for reference/full-text follow-up, is
marked out of scope, or is recorded as blocked with a reason.

For every subquestion and every iteration, maintain auditable state in the
subquestion folder or linked query-refinement packet:

```text
iteration_id:
queries:
discovery audit:
title/snippet decisions:
abstract previews:
capture queue:
captured articles:
reading notes:
high-value seed ledger:
reference marks:
gap list:
coverage review:
blockers:
```

Run every loop iteration in this fixed order:

```text
OpenCLI publisher discovery -> title/snippet/context triage -> abstract preview from saved search-page abstracts -> OpenAlex metadata for missing abstracts -> OpenCLI detail-page preview only for still-missing abstracts -> capture decision -> structured publisher full-text capture
-> reading-note-zh.md -> high-value seed/reference/gap extraction
-> coverage review -> sufficient/iterate_query/stop_with_gaps/blocked
```

When OpenCLI opens article/detail pages, process the queue one page at a time.
For multi-publisher queues, interleave publishers in the supported order
Elsevier/ScienceDirect -> ACS -> Wiley -> Springer, then repeat, so the time
spent on other publishers creates spacing before returning to the same domain.
For single-publisher queues, keep the same one-at-a-time behavior and rely on
the article-page wait/scroll cadence rather than opening multiple articles
quickly.

Loop invariants:

- Each iteration must produce a durable, inspectable artifact for every stage it
  reaches. Missing stages require a concrete blocker entry.
- Each iteration must monotonically add evidence or record a blocker. Evidence
  can be new full text, reading-note content, a seed, a reference mark, a gap
  clarification, or a failed-access/blocker record.
- Do not repeat the same query without a new reason from reading notes, high-value
  seeds, reference marks, gaps, or user feedback.
- Even with a new reason, do not resend an executable broad query that exactly
  matches or only reorders a previous broad query. The new reason belongs in
  the review-page details or gap ledger unless it produces a genuinely new
  English short phrase.
- `next-simple-queries.csv` is only a queue of candidate next queries. Run
  `continue_query_iteration.py` only after the responsible subquestion agent
  reads the cumulative full text and notes, records
  `coverage_decision=iterate_query`, writes `query-rationale-review.json`, and
  the user approves the generated `query-plan-amendment.json`.
- Do not run `query_iteration_review.py`, `continue_query_iteration.py`, or
  any amendment builder for iteration 2+ until
  `query-rationale-review.md/json` exists for that subquestion. The amendment
  builder is not an authoring agent. It must reject missing rationale review,
  and it must not create broad queries by compressing reading-note prose,
  coverage-gap sentences, seed-ledger labels, or previous query-plan phrases.
- When `continue_query_iteration.py` runs an approved iteration amendment, it
  must execute only `query_mode=broad_discovery` rows by default. Exact targets
  and manual holds stay visible in `exact-targets.*` and `manual.csv` and are
  not treated as missed automation work.
- After query iteration starts, first generate `query_iteration_plan` and
  `query-plan-amendment.json` from the seed ledger, cumulative full-text
  evidence, missing evidence, reference leads, gaps, and user review; review
  them through `review-plan`. For iteration 2 and later, the amendment must
  retain only agent-verified OpenAlex exact targets as ledgers/export files in
  the review/amendment context, plus `manual.csv` for out-of-scope,
  ungrounded, ambiguous, or semantically mismatched exact targets. The
  amendment builder may format and carry exact-target metadata, but it must not
  invent the exact query, pick the OpenAlex match, or turn an unverified match
  into a capture route. Only bounded `broad_discovery` rows are executable by
  default.
  Unresolved correction notes block `query_iteration_plan_approval`.
  After approval, repeat the full evidence loop. Do not only
  re-search, only inspect abstracts, or jump directly from query results to
  subquestion sufficiency.
- `rcs_0_to_10` is staged triage for preview/capture/branch stop decisions.
  Search-page RCS can only queue abstract preview; capture RCS requires a
  later abstract-review pass. `coverage_score_0_to_5` is produced only after full text,
  reading notes, high-value seeds, reference marks, and gaps have been reviewed.
- Coverage decisions must use all historical evidence for that subquestion
  across every prior iteration, not just the newest search page or abstracts.
- After each coverage packet is written or edited, run
  `run_subquestion_loop.py` for the affected subquestion. This script is the
  machine gate for the loop: `needs_coverage_scoring` means the subagent still
  has not supplied a real coverage judgment, `needs_openalex_grounding_audit`
  means the next iteration lacks an OpenAlex grounding audit, and
  `ready_for_query_iteration` is the only state that may proceed to
  `continue_query_iteration.py`.
- The L2 orchestrator's default validation budget is one controlled query
  iteration unless the user sets another bound. When the budget is exhausted,
  record `stop_with_gaps` or `blocked`; do not continue an implicit unbounded
  loop.

## Completion Gate

Do not write the final `overview.md`, call the run complete, or move to
the terminal llm_wiki project export until every gate below is satisfied or explicitly marked
`blocked` with evidence:

- `query-refinement-recommendations.json` exists for every discovery iteration
  and every query group has a decision, `rcs_0_to_10`, `rcs_reasoning`, and a
  capture/preview/iterate/stop rationale.
- Each subquestion has a `coverage-review` decision and a
  `subquestion-summary-zh.md` that records `coverage_score_0_to_5`,
  `coverage_decision`, evidence used, and remaining gaps.
- `run_subquestion_loop.py` has been run for every subquestion after the latest
  coverage edit. It must return `sufficient`, `stop_with_gaps`, or `blocked`
  before final synthesis, or `ready_for_query_iteration` before another query
  round. Do not treat `needs_coverage_scoring`, `needs_reading_notes`,
  `needs_openalex_grounding_audit`, or `needs_next_queries` as complete.
- Any weak subquestion (`coverage_score_0_to_5 < 4` or
  `coverage_decision=iterate_query`) has a next full loop iteration with
  auditable artifacts for search, title/snippet review, abstract preview,
  capture decision, full-text capture or blocker, reading notes, high-value
  seeds, reference marks, gaps, and coverage review, unless the user-specified
  iteration budget is exhausted and the subagent records `stop_with_gaps` or
  `blocked`.
- Any high-value seed paper, database, method, benchmark, model, ontology, or
  resource name found during reading notes, references, user feedback, or
  overview drafting is either converted by agent rationale into a new approved
  `broad_discovery` query for a non-terminal subquestion, retained as an exact
  target/reference/manual ledger row for terminal follow-up, or recorded in a
  `not-searched-yet`/gap list with the reason.
- `recommended-references.*` exists for every close-read primary article that
  has usable references, or the article note explains why no reference was
  selected.
- Subagent usage is auditable: each subquestion and query-refinement packet has
  a saved prompt/input file and a saved response/decision file. If no subagent
  tool is available, the main agent must record `main_agent_fallback` instead
  of claiming subagent review.
- `reference_followup.py` has run only after all primary subquestions are
  terminal (`sufficient`, `stop_with_gaps`, or `blocked`). In the default
  `agent-picked` mode, references recommended while writing reading notes are
  already approved for follow-up; do not run a second relevance-selection pass
  over `final-reference-selection.*`. Python dedupes, reconciles metadata, and
  routes only. Elsevier/ScienceDirect, ACS, Wiley, and Springer stay on the
  structured reference route; Science/Nature/arXiv rows may use supplemental PDF
  follow-up; unsupported rows remain in manual hold artifacts with a concrete
  capture reason.
- Every approved supported reference has either been captured under
  `references/<source>/articles/` with `capture_depth=2`, or is marked blocked
  by publisher/access/PDF/MinerU constraints.
- After supported reference and supplemental PDF captures are normalized, rerun
  the cumulative evidence review for affected subquestions before final
  overview. This second scoring pass uses primary captures, exact-target
  captures, reference captures, PDF/MinerU captures, and remaining manual holds;
  it is not a new broad-discovery approval gate unless the user explicitly
  reopens a weak subquestion.
- Before asking the user to manually download residual PDFs, run
  `knowledge_staging.py` and inspect `_knowledge/` so the user sees one
  subquestion-grouped dropbox, short DOI-backed action lists, canonical paper
  cards, duplicate reports, figures/tables, important seeds, recommended
  references, and query journeys. After the user supplies PDFs and they are
  ingested/MinerU-normalized/read by subagents, run `knowledge_staging.py`
  again before final coverage and overview so the knowledge dossier reflects
  both earlier structured captures and later manual/PDF evidence.
- `capture_depth=2` means second-level reference follow-up from a captured
  primary article. It is not a domain-specific limit; third-level chasing is
  disabled by default only to keep broad reviews bounded unless the user asks
  to dig deeper.
- The final overview cites both primary captures and approved/captured
  reference-follow-up evidence. If no references were captured, it must say why
  the reference gate was blocked.

The preferred broad-question path is agent-controlled search iteration, not
automatic first-N capture. Python saves every search page candidate and prepares
review packets; a dedicated literature-search subagent decides search-page
relevance, article picks for abstract preview, next keywords, and when to stop
or continue that query branch. It must not decide that a whole subquestion is
explained from search-page triage or abstract-preview evidence alone, and it
must not approve full-text capture until a later abstract-review pass. Full article capture should normally run only from
`capture-queue.csv`, which contains agent-approved article URLs. agent writes
the final reading notes after the bounded capture batch completes. For broad
work, the main agent owns decomposition, tool execution, task dispatch, and
final overview. Search-page triage may be batched for efficiency, but the
subagent prompt must still preserve the subquestion-specific reading lens for
each group. For abstract capture review, reading notes, coverage, and reference
selection, dispatch by atomic subquestion because each subquestion needs
different expertise: resource/data questions read for databases and entity
fields, schema questions read for ontology/relation design, and evaluation
questions read for embeddings, recommendation tasks, metrics, baselines, and
failure cases. Each subagent owns exactly one subquestion or one search
iteration packet and should not edit sibling subquestion folders except when the
main agent explicitly asks for cross-subquestion synthesis. For subquestion
work after full-text capture, reuse the same subquestion agent across reading
notes, seed extraction, reference marking, gap review, coverage scoring, and
next-query suggestions. Abstract-capture scoring is an exception: it is a clean,
stateless worker task over a fixed packet, not a continuation of the main loop
conversation.
For primary full-text stages, "reuse the same subquestion agent" means one
subquestion owner should carry the local reasoning loop for that subquestion:
read each captured primary article, write article reading notes, recommend
references from close-read articles, update gaps/seeds, and then score primary
coverage for the whole subquestion. After all primary subquestions reach a
terminal coverage state, the same subquestion owner reviews/corrects the
centralized reference selection for its subquestion, reads any captured
second-level references, and updates the subquestion summary. The main agent
and scripts still execute browser work, capture queues, validation, and stage
transitions. The subquestion owner reasons from local artifacts, not from the
full main-agent chat history. Dispatch the worker with only the approved
query-plan basis, the subquestion reading lens, article folders, reading notes,
recommended/reference files, coverage packet, and local subquestion loop-state
exports. Do not include the main conversation transcript, run-level audit logs,
old failed worker responses, unrelated sibling subquestions, browser logs, or
fallback/tool discovery instructions.
Do not create a new subagent for each full-text task stage; run subquestions sequentially:
finish the current subquestion's loop gate or blocker before opening the next
subquestion agent.

Subquestion reading workers need a machine-checked artifact contract, not just
natural-language trust. Before dispatch, the main agent must identify the exact
subquestion folder and article count under `sources/*/articles/*`. The worker
gets only that folder, the local brief/prompt, and the required output list. It
must write `reading-note-zh.md` inside every primary article folder before it
writes final coverage claims. A `subquestion-summary-zh.md` without per-article
notes is an invalid placeholder, not progress.

After the worker returns, or after a bounded wait if it appears stalled, run:

```bash
.venv/bin/python literature-loop-capture/scripts/validate_subquestion_reading_gate.py \
  "LiteratureCaptures/<run>/subquestions/<group>/<id>" \
  --require-response \
  --require-summary
```

If the validator reports `missing_reading_note`, `missing_required_markers`,
`invalid_or_empty_subagent_response`, or
`invalid_or_placeholder_subquestion_summary`, do not proceed to coverage
scoring. Interrupt the same worker once with the validator output and exact
missing files. If the second validation still fails, close that worker and
record `blocked_subagent_no_artifacts` or use an explicit
`main_agent_fallback` artifact; do not keep waiting indefinitely and do not
call the folder subagent-complete.

Every reading note must include the literal markers checked by the validator:
`five cs`, `图表检查 / figure table check`, `worth_close_reading:`,
`worth_close_reading_score_0_to_5:`, `对 subquestion coverage 的影响 / coverage
impact`, `high-value seed ledger`, `reference pick / selected reference`, `gap
list`, and `proposed next query`.

## Subagent Accountability

Use actual subagent/multi-agent tooling when it is available. Do not merely say
"the subagent decided" inside the main-agent narration. For every subquestion
and every query-refinement iteration, save an auditable packet:

```text
query-refinement/iteration-NN/subagent-prompt.md
query-refinement/iteration-NN/subagent-response.md
subquestions/<group>/<id>/subagent-prompt.md
subquestions/<group>/<id>/subagent-response.md
subquestions/<group>/<id>/sources/<source>/articles/<article>/recommended-references.*
```

If the platform has no callable subagent tool, write the same files with
`review_mode: main_agent_fallback` and state that no independent subagent was
available. Before using this fallback, actively check the available tools or
deferred tool discovery for subagent/multi-agent capability. If a tool such as
`spawn_agent` is available, use it for query-refinement, subquestion reading,
and coverage review instead of claiming fallback. In fallback
mode, record the failed tool-discovery result in the prompt/response artifact
and do not describe the result as subagent-validated.

Every subagent/fallback response must be machine-checkable. A valid subagent
response includes `review_mode: subagent`, a concrete `agent_id`, the reviewed
artifact paths, and the decision evidence. A valid fallback response includes
`review_mode: main_agent_fallback` and `fallback_reason`. Empty responses,
generic prose without provenance, and script-fabricated "agent reviewed" claims
are invalid; `apply_query_decisions.py`, `run_subquestion_loop.py`, and
`literature_loop.py verify` reject them.

For `abstract_capture_review`, build clean worker packets before dispatch:

```bash
python literature-loop-capture/scripts/build_abstract_capture_review_packets.py \
  "LiteratureCaptures/<run>"
```

Do not fork the full main-agent conversation into the reviewer. The reviewer
gets only `abstract-capture-review-input-<subquestion>.json`, the matching
worker prompt, the query-plan basis for that subquestion, the RCS rubric, the
expected row count, and the exact output path. Do not include the whole skill,
loop history, prompt audit files, old invalid reviews, browser logs, previous
main-agent messages, or fallback/tool-discovery instructions in the worker
prompt. The reviewer is a scoring worker, not the orchestrator: it must not run
`abstract_preview.py`, open browsers, spawn/delegate to other agents, inspect
tool availability, update loop state, or start capture. It must score every row
in its packet and write only the requested
`abstract-capture-review-full-<subquestion>.json/md`. Use no target article
count; all RCS >= 7 supported-publisher rows with direct evidence should be
captured.

Before starting full-text capture, validate every full abstract-capture review:

```bash
python literature-loop-capture/scripts/validate_abstract_capture_review.py \
  "LiteratureCaptures/<run>/abstract-preview/abstract-capture-review-full-<subquestion>.json" \
  --abstract-preview "LiteratureCaptures/<run>/abstract-preview/abstract-preview.csv"
```

The validator must pass for all subquestions. It rejects wrong provenance,
missing `reviewed_count`, incomplete row coverage, duplicate rows, generic
repeated reasoning, and capture rows with RCS below 7.

Subquestion prompts must be specific to the subquestion and current evidence.
They must include:

- the exact subquestion text and coverage target
- article folders to read, not just titles
- figures/tables/references to inspect when available
- query groups and RCS scores already assigned
- remaining gaps and candidate next queries
- reference-selection instructions tied to the subquestion

A generic prompt reused across all subquestions is invalid. Reading-note prompts
must change with the article content: methods papers should ask for method and
evaluation extraction; resource papers should ask for schema/data/source
extraction; review papers should ask for landscape, taxonomy, and gap
extraction.

Reading-note, reference-selection, and coverage-scoring workers must not be
given the earlier main conversation as memory. They read the captured full text
and local artifacts themselves. Their output must cite reviewed artifact paths,
not "as discussed earlier" or other chat-history evidence. They must not run
publisher discovery, abstract preview, browser navigation, full-text capture, or
delegate to another agent; the main agent handles tools and state transitions.

Publisher identity must not change the search-stage reasoning loop. Whether
the user searches ScienceDirect, Wiley, ACS, Springer, Science, IEEE, or any
other school-purchased direct publisher resource, keep the same loop: OpenAlex metadata
grounding, atomic subquestions, short subquestion-specific query phrases, one
result page, subagent title/snippet scoring with RCS, abstract preview for
mid-RCS candidates, capture only after explicit approval, coverage review, and
query iteration until the subquestion is reasonably explained or the iteration
budget is exhausted. Publisher-specific behavior begins only after an approved
article is ready for full-text capture.

On Windows, keep all generated path segments short. The capture script uses
short subquestion IDs and numeric article working folders such as `primary_001`
and `ref_001`; titles, DOI values, and final short names live in metadata and
organize/export manifests. Do not lengthen those path segments in custom
commands or downstream scripts.

## Mandatory Preflight Gate

Do not start structured publisher validation, browser capture, or publisher searches until
this preflight is complete:

1. Translate or normalize the user's research question into English yourself.
   Users may ask in Chinese or any other language; do not ask them to rewrite
   the question in English.
2. Use agent baseline knowledge plus broad OpenAlex works metadata to identify
   the topic frame, synonyms, industry terms, major resources, method families,
   applications, and known limitations. The agent must inspect titles,
   abstracts, topics, keywords, venues, DOI/year/citation signals,
   claim-vocabulary hints, and supported-publisher focus evidence before
   finalizing subquestions and publisher queries. `probe_queries` are retained
   only in machine-readable JSON for audit and validator checks; do not use
   them as human-facing planning prompts or executable search strings.
3. Run OpenAlex grounding with `OPENALEX_API_KEY` and record it as an
   `--exploration-source`. OpenAlex is metadata grounding only; it is not a
   full-text source.
4. Run `scripts/openalex_grounding.py` with the English question.
5. Read `agent-query-plan-packet.md` and author `agent-query-plan.json`.
   Python must not fabricate the broad-question subquestions, query families,
   group folders, or keyword plan. Do not reuse a previous project's
   subquestion outline unless the current OpenAlex metadata and user review
   goal independently justify it.
6. Run `scripts/validate_agent_query_plan.py` and
   `scripts/build_publisher_urls.py` to write `query-plan-preview.md/json`.
7. Show the user the English big question, agent-authored subquestions, query
   families, concept anchors, simple keyword queries, publisher-specific search
   URLs, and why the decomposition is appropriate.
8. Wait for user approval before running `incremental_capture.py`.
9. Pass the approved `query-plan-preview.json` back into root discovery with
   `--approved-query-plan`. New root discovery is blocked without it.

The approved query plan must contain an `openalex_grounding` audit with
`api_key_present=true`, `status=ok`, and non-empty `terms`. Do not treat a plain
`OpenAlex|https://openalex.org|metadata grounding` exploration-source label as
proof that OpenAlex actually ran. If the audit says `missing_api_key`,
`no_terms`, `error`, or is absent, stop before structured publisher discovery and fix the
OpenAlex grounding step.

This gate is mandatory for broad questions. The only exception is when the user
has already provided a concrete English query plan or explicit English
`--query` values in the same turn and asks to run exactly those.

Do not map the user's thesis, claim, or topic line to `--query`. A prompt such
as `论点: <中文研究主题>` is the broad `--claim`; it must go through
OpenAlex metadata grounding, agent concept synthesis, and dynamic subquestion
planning. `--query` is an advanced exact-query override only. Every `--query`
value is treated as a finished user-provided subquestion and will produce
`user-provided-XX` query families.

All query-planning and capture inputs passed to scripts must be English, but
the user-facing request may be Chinese. The agent must translate and normalize
the research question, show the English version for approval as part of the
preflight, and then run scripts with the English `--claim` and `--query` values.
Do not pass Chinese `--claim` or `--query` values to capture.
`incremental_capture.py` rejects CJK claim/query text unless
`--allow-non-english-queries` is explicitly used for debugging.

Treat one atomic subquestion as one query round. The agent decides the
subquestions after reading OpenAlex titles, abstracts, topics, keywords, venues,
and the user's review goal. Do not import a fixed outline from a previous
project or combine several unrelated review points into one subquestion. If the
agent's topic map contains more useful atomic subquestions than the user's
`--rounds` budget, show the full list, explain why each candidate exists, state
which will be included, and ask whether to increase `--rounds` or proceed with
the selected subset.

For filesystem organization, atomic subquestions may be grouped under broader
composite folder themes. This grouping is only structural; it must not merge
multiple atomic query rounds into one search task. Use paths like
`subquestions/<short-group>/<short-subquestion-id>/` so browsing the output
still reflects a coherent review outline without exceeding Windows path limits.
The JSON/CSV metadata retains the full subquestion text, query family, concept
groups, concept-map trace, and publisher query URLs.

## Requirements

- The user must authenticate the required publisher sites in the Chrome profile
  connected to OpenCLI before publisher discovery, abstract preview, or
  capture. Scripts never handle credentials.
- Before OpenCLI publisher discovery or full-text capture starts, confirm that
  `opencli doctor` passes, the intended Chrome profile is selected, and the
  user has logged into the needed publisher platforms in that profile. If a
  publisher page redirects to a login/authentication page, authorization wall,
  robot challenge, or blank page, stop and ask the user to authenticate in the
  connected Chrome/OpenCLI profile before continuing. Do not proceed into
  publisher URLs while this preflight is blocked.
- Run OpenCLI browser scripts sequentially for a given literature run. Use the
  default sessions `lit`, `lit-preview`, and `lit-capture`, or pass
  `--opencli-session` explicitly when a different session is needed.
- If an authenticated connected Chrome/OpenCLI GUI is already open with the target profile,
  do not terminate it from agent or from a script, and do not copy the profile
  to bypass the lock. This state means authentication may still be valid and URLs
  can be opened in the same GUI profile, but a script cannot automatically read
  DOM from that GUI unless it was started with managed/CDP attach support. Record
  this as `profile_active_gui_only`, not as an auth failure. For abstract preview
  rows, the script may open the queued URL in the GUI profile for user-visible
  confirmation, then use OpenAlex metadata fallback if DOM automation is not
  attachable and DOI/title metadata is available. For discovery and full-text
  capture, use managed/CDP attach or close/relaunch the GUI under script control;
  do not pretend the GUI-only page was machine-read.
- During publisher discovery or capture, if the browser remains blank after
  launch, lands on a login/authentication page, redirects away from the
  expected direct publisher route, or the run logs `search-page-started`
  followed by repeated zero-candidate discovery for publisher pages, stop
  treating the run as a normal capture. Open the relevant publisher home or
  article page in the same connected Chrome/OpenCLI profile when possible, tell
  the user to re-authenticate, ask them to manually close that login window,
  then rerun the bounded structured publisher capture. Do not keep silently
  polling or continue an empty structured publisher run.
- The capture script enforces this behavior at runtime: blank/new-tab pages,
  login/auth pages, off-direct publisher redirects, and four consecutive zero-result
  publisher pages raise a clear `publisher_*` error and write a log event.
- If a publisher page shows a robot/CAPTCHA challenge, the script records
  `blocked_robot`, prints a warning, waits the configured manual-resolution
  window, and rechecks. If the page is still blocked, stop the browser step and
  use OpenAlex metadata fallback for abstract screening when DOI/title metadata
  is available. The script must not solve or bypass CAPTCHA challenges.
- Use direct publisher URLs for all supported structured routes: ScienceDirect/Elsevier,
  ACS, Wiley, and Springer.
- Search URLs for this OpenCLI validation route are limited to the four
  supported structured publishers. Other authenticated resources are not part of
  the automatic capture path and must be recorded as `manual_hold` unless the
  user explicitly expands scope later.
- Do not use DOI resolver routes as search or capture entrypoints.
- For ScienceDirect/Elsevier, ACS, Wiley, and Springer, prefer the existing
  structured HTML/full-text capture code and do not replace it with publisher
  PDF downloads. This is especially important for Elsevier/ScienceDirect,
  where repeated PDF downloads can trigger risk controls.
- Purchased publishers not yet modeled for structured capture are out of scope
  for this route. Record them as `manual_hold` with the best URL/DOI evidence
  and do not run PDF or MinerU fallback automatically.
- Use OpenAlex/Crossref only for metadata context. They are not full-text
  sources.
- MinerU and PDF extraction are outside the OpenCLI structured-capture
  validation route. They are allowed only in the later Supplemental PDF
  Follow-up route after primary subquestion coverage is terminal and
  reading-note recommended references have been aggregated/routed.
- Treat `--max-results-per-page` as the per-source, per-query structured publisher discovery
  cap. Candidate capture is not first-N; the subagent selects from the page.
  For large broad questions, lower it before
  launching capture instead of stopping a large run after it has created many
  article folders.
- Enforce the requested year window twice: search URLs should include year
  filters when the publisher supports them, and candidate/article metadata
  should be filtered again after discovery because some publisher search
  pages do not reliably honor year parameters.
- Python may write `auto-extract-note.md` and `NOTE_REQUIRES_AGENT.md`, but
  final `reading-note-zh.md` must be written by agent after reading the captured
  article files.
- Reference follow-up selection is agent-first by default. While writing
  `reading-note-zh.md`, agent/subagents should write
  `recommended-references.csv/json/md` only for articles worth close reading.
  This records future reference candidates only; it does not trigger immediate
  reference capture. After all primary subquestions are terminal,
  `reference_followup.py` aggregates those agent-picked references, dedupes
  them, checks DOI and bibliographic metadata with Crossref for the bounded
  pool, and writes the reference capture queue. If reading notes did not
  produce recommendations, send the relevant article folders back to the
  responsible agent for a better reading-note/reference pass instead of using
  Python ranking as a substitute.
- Reference follow-up capture is second-level only by default. In
  `agent-picked` mode, reading-note recommendations are already approved; the
  queue action is determined by metadata validation and publisher/PDF route
  support. OpenCLI-unsupported references remain `manual_hold`. Supported
  structured references go through the same OpenCLI publisher discovery and
  OpenAlex-backed abstract screening workflow, then capture queue, structured publisher
  full-text capture, and normal reading notes. Supported Science/Nature/arXiv
  PDF references wait for Supplemental PDF Follow-up, then MinerU API
  normalization, then normal reading notes.
  Do not perform third-level reference chasing unless the user explicitly asks
  to dig deeper.
- Second-level captures must be written back into the same subquestion folder
  under `references/<source>/articles/` with `--existing-run-dir`,
  `--subquestion-id`, `--capture-depth 2`, `--parent-article-dir`, and
  `--parent-reference-index`.
- Final `overview.md` must be written by agent, not by Python. It is a
  literature synthesis over the captured papers and reading notes, not a run
  summary.

## Mac Migration Notes

This skill is portable as a runtime package. For macOS migration, copy only the
skill runtime files (`SKILL.md`, `agents/`, `references/`, and `scripts/`) plus
the repository `README.md`, `MIGRATION_MAC.md`, and package `MANIFEST.txt`.
Do not copy `.git/`, `.pytest_cache/`, `tests/`, `docs/`, `output/`,
`LiteratureCaptures/`, browser profiles, captured outputs, or credentials.

Install the skill under your agent's skill directory, for example
`<agent-skill-dir>/literature-loop-capture`, and
replace any Windows example paths with local macOS browser/profile paths.
OpenAlex query planning does not require connected Chrome/OpenCLI. OpenCLI publisher
screening and full-text capture both require the authenticated connected Chrome/OpenCLI
profile.

## Question Grounding

Before running publisher capture from a broad question, the agent must
author the subquestions. Do this in a Deep Research-like way: clarify the
research objective, identify the main concepts and neighboring terms, search the
web for current topic framing when useful, inspect broad OpenAlex metadata, then
turn the agent's topic map into query rounds. Grounding is intentionally wide so
it can expose terminology and neighboring concepts; Python only records that
metadata and must not decide the subquestions. The grounding must be saved
before capture and should support:

- Agent-authored atomic subquestions chosen from the user's review goal and
  OpenAlex work metadata. The number and shape of subquestions are not fixed by
  Python or by a canned resources/methods/evaluation template; use the user's
  requested `--rounds` only as a review budget. Common evidence types such as
  definitions, resources, methods, validation, applications, limitations, and
  gaps are a coverage checklist, not a required subquestion outline. Each
  accepted subquestion is one query round and one subagent work package.
- Query families for each subquestion, with concept groups and simple keyword
  queries rather than the raw user sentence alone. Keep the Boolean expression
  only as a concept map/explanatory trace, not as the default publisher query.
- A short record in `query-rounds.json` showing why each query round exists.
- `grounding-notes.md`: agent-written broad exploration notes from baseline
  knowledge and OpenAlex metadata signals. OpenAlex is metadata grounding only;
  it is not full-text evidence and cannot satisfy a subquestion coverage gate.
- `openalex-grounding.md/json`: script-written OpenAlex work metadata samples
  that the agent can inspect before approval, including titles, year, venue,
  DOI, topics/keywords, and abstract excerpts when OpenAlex provides them. Do
  not treat `openalex_grounding.terms` alone as sufficient approval evidence.
  The JSON also records `probe_queries` for audit/validator checks and
  `concept_hints` as claim-vocabulary hints. The readable Markdown and review
  HTML should show claim vocabulary, not broad probes.
- `exploration-sources.csv/json`: URLs or metadata sources used for framing.
- `query-plan-preview.md/json`: validated agent-authored English atomic
  subquestions, query families, concept anchors, simple publisher queries,
  publisher query URLs, and composite folder groups.

The agent uses OpenAlex work metadata from titles, abstracts, `primary_topic`,
`topics`, and `keywords` to write the query plan. Do not use deprecated
OpenAlex Concepts. Normal broad-topic runs must stop if the OpenAlex audit is
missing or empty.

`openalex_grounding.py` writes the machine-readable OpenAlex audit into
`openalex-grounding.md/json` and writes `agent-query-plan-packet.md` for
agent-readable metadata inspection. Machine-readable `probe_queries` are not
approved publisher queries and are not shown as review-page planning prompts.
The agent writes `agent-query-plan.json`;
`validate_agent_query_plan.py` rejects missing evidence, bare strings, and
near-duplicate suffix queries. It also rejects executable query text that
copies an OpenAlex probe query or retains year-filter residue such as `years`
or `2021-2026`; `build_publisher_urls.py` writes the approved
`query-plan-preview.md/json` consumed by discovery.

Generate the grounding packet:

```powershell
python "literature-loop-capture\scripts\openalex_grounding.py" `
  --claim "What is the current research landscape for your topic?" `
  --rounds 3 `
  --grounding-notes "Agent notes from baseline knowledge and OpenAlex metadata inspection." `
  --exploration-source "OpenAlex|https://openalex.org|metadata grounding" `
  --output-dir "LiteratureCaptures\query-plan-preview"
```

After this command, the agent must read `grounding-notes.md`,
`openalex-grounding.md`, `exploration-sources.csv`, and
`agent-query-plan-packet.md`, then write `agent-query-plan.json`. Build the
preview only after validation:

```powershell
python "literature-loop-capture\scripts\validate_agent_query_plan.py" "LiteratureCaptures\query-plan-preview"
python "literature-loop-capture\scripts\build_publisher_urls.py" "LiteratureCaptures\query-plan-preview" `
  --year-start 2021 `
  --year-end 2026
```

Show the user the English research question, agent-authored subquestions, query
families, representative OpenAlex signals, publisher focus summary, and
publisher query summary for approval.

For a new root run, the approved `query-plan-preview.json` is the source of
truth for subquestion IDs, group folders, subquestion text, and publisher
queries. Do not use repeated `--query` values or `--subquestion-id` to simulate
multiple approved subquestions. `--subquestion-id` is only valid with
`--existing-run-dir` when appending a later iteration or reference capture to an
existing run.

The planner builds query rounds from keyword concept groups. Publisher searches
use simple keyword strings with quoted multi-word phrases, not full Boolean
expressions. ScienceDirect uses `qs=<simple keywords>` plus a date range; ACS
and Wiley default to the broader `AllField` search, because their `Keyword`
field is too narrow for abstract/title screening and can hide useful candidates.
Springer uses advanced search query with the configured year window when
explicitly enabled. If results are weak, iterate terms from the first-page
publisher/OpenCLI evidence instead of making the initial query more complex.

When a query iteration, reading note, reference list, user correction, or
overview draft surfaces a high-value named resource or method, preserve that
name as a candidate short query instead of collapsing it into a generic topic.
The agent must build a `query-derivation-ledger` for each iteration, with:

- source artifact: article folder, reference row, abstract preview, or user note
- extracted seed: exact paper/resource/model/database/ontology/method name
- inferred need: why this seed would improve a subquestion
- proposed query: short publisher-search phrase, usually the exact name
- action: searched | queued | out_of_scope | blocked
- evidence: result count, abstract preview, capture decision, or blocker

Do not hardcode domain examples in this skill. Let each run derive its own
resource-name queries from its evidence and the user's feedback. If a named
resource appears outside the configured publisher scope, still run
metadata/discovery where possible and record the publisher/access gap.

## Agent-Controlled Query Iteration

Use this loop when the user wants higher relevance before full capture. A query
iteration is not just another search command; it starts a new full evidence
cycle for the same subquestion:

```text
OpenCLI publisher discovery -> title/snippet/context triage -> abstract preview from saved search-page abstracts -> OpenAlex metadata for missing abstracts -> OpenCLI detail-page preview only for still-missing abstracts -> capture decision -> structured publisher full-text capture
-> subquestion agent reads all captured full text for the current subquestion
-> subquestion agent writes reading notes, high-value seeds, reference picks, and gap list
-> coverage review reads full text metadata, reading-note-zh.md content, seed evidence, selected references, gaps, and blockers
-> if coverage is insufficient and the iteration budget remains, run only approved broad_discovery queries from the user-approved query-plan amendment
-> repeat OpenCLI publisher discovery -> title/snippet/context triage -> abstract preview from saved search-page abstracts -> OpenAlex metadata for missing abstracts -> OpenCLI detail-page preview only for still-missing abstracts -> capture decision -> structured publisher full-text capture
-> repeat reading notes -> high-value seed extraction -> reference marking -> gap list
-> decide coverage again from cumulative full text and notes across all rounds
-> repeat up to the user-specified iteration budget; the L2 orchestrator default is one controlled repeat
-> move to next subquestion only when coverage is sufficient or the budget is exhausted with rationale
```

Run discovery without full capture:

```powershell
python "literature-loop-capture\scripts\incremental_capture.py" `
  --claim "your English research question" `
  --approved-query-plan "LiteratureCaptures\query-plan-preview\query-plan-preview.json" `
  --rounds 5 `
  --max-queries 3 `
  --max-pages 1 `
  --max-results-per-page 20 `
  --discovery-only `
  --discovery-backend opencli `
      --write-query-refinement-packets `
  --query-refinement-total-iterations 3 `
  --include-structured-publishers
```

This writes `discovery-audit.csv/json` and
`query-refinement/iteration-01/query-refinement-agent-brief.md`. Give that
brief to a literature-search subagent. The subagent must fill
`query-refinement-recommendations.json` from the template, with
`review_phase=search_page_triage` and one decision per query group:

- `needs_abstract_preview`: titles look promising but abstracts are needed.
- `iterate_query`: first-page results are not good enough; use proposed simple keywords.
- `stop_low_yield`: stop this query after weak iterations and move on.

Do not use `ready_to_capture` during `search_page_triage`; put promising rows in
`abstract_probe_articles` instead. Search-page triage is intentionally less
strict so it does not lose good articles before seeing abstracts.

Then apply the decision:

```powershell
python "literature-loop-capture\scripts\apply_query_decisions.py" `
  "LiteratureCaptures\<run>"
```

If the decision requests abstracts, run:

```powershell
python "literature-loop-capture\scripts\abstract_preview.py" `
  "LiteratureCaptures\<run>" `
  --recommendations "LiteratureCaptures\<run>\query-refinement\iteration-01\query-refinement-recommendations.json" `
  --abstract-queue "LiteratureCaptures\<run>\query-refinement\iteration-01\applied-decisions\abstract-preview-queue.csv" `
    ```

Do not run several `abstract_preview.py` commands concurrently with the same
connected Chrome/OpenCLI profile. Use one queued CSV per command and let the script process
rows sequentially.

In the standard loop, the direct input to `abstract_preview.py` is the
`query-refinement/iteration-NN/applied-decisions/abstract-preview-queue.csv`
file written by `apply_query_decisions.py`. Search-page decisions in
`query-refinement-recommendations.json` are the upstream rationale used to
create that queue; they are not the standard direct preview input. The script
also has a compatibility mode that reads `query-refinement-recommendations.json`
directly when `--abstract-queue` is omitted, but do not use that mode for the
normal staged loop because downstream packet builders expect the normalized
queue output. With the queue path and no explicit `--output-dir`,
`abstract_preview.py` writes the canonical run-level
`abstract-preview/abstract-preview.csv`, `.json`, and `.md`; those files become
the input to `build_abstract_capture_review_packets.py` and
`apply_abstract_capture_reviews.py`.

The search/subquestion agent reads `abstract-preview.md/json/csv` together
with the earlier title/snippet/context only through the clean worker packet
created by `build_abstract_capture_review_packets.py`. It writes
`abstract-capture-review-full-<subquestion>.json/md` with
`review_phase=abstract_capture_review`, then `apply_abstract_capture_reviews.py`
creates `capture-queue.csv` from validated `capture_articles`. The abstract
review either approves capture with stricter RCS, keeps rows as maybe, or skips
them. It must not mark the subquestion sufficient here; sufficiency waits until
the responsible subquestion agent has read the captured full text and notes
for the whole subquestion. If `next-simple-queries.csv` has rows from the
earlier search-page pass, treat those rows as queued candidate next-query
ideas only. They must be re-justified in `query-rationale-review.json` and
approved through the iteration review page before `continue_query_iteration.py`
runs.

```powershell
python "literature-loop-capture\scripts\build_abstract_capture_review_packets.py" `
  "LiteratureCaptures\<run>"

python "literature-loop-capture\scripts\validate_abstract_capture_review.py" `
  "LiteratureCaptures\<run>\abstract-preview\abstract-capture-review-full-01_example.json" `
  --abstract-preview "LiteratureCaptures\<run>\abstract-preview\abstract-preview.csv"

python "literature-loop-capture\scripts\apply_abstract_capture_reviews.py" `
  "LiteratureCaptures\<run>"
```

Capture only approved URLs:

```powershell
python "literature-loop-capture\scripts\capture_decision_queue.py" `
  "LiteratureCaptures\<run>" `
  --capture-queue "LiteratureCaptures\<run>\query-refinement\iteration-01\applied-decisions\capture-queue.csv" `
    ```

`capture_decision_queue.py` branches only at this point. Rows whose publisher
is `elsevier`, `acs`, `wiley`, or `springer` use structured HTML/full-text
capture. Rows from other publishers are invalid for this route; keep them as
`manual_hold` outside `capture-queue.csv` until the user explicitly expands
the supported publisher set.

After capture, write or update `reading-note-zh.md` for every captured article
in the affected subquestion first. The responsible subquestion agent must
read the captured full text, figures, tables, references, existing reading
notes, high-value seed ledger, and remaining gaps before judging coverage.
Before generating or editing the coverage packet, run
`validate_subquestion_reading_gate.py` for that subquestion with
`--require-response --require-summary`; if it fails, repair the agent artifacts
or record a blocker instead of continuing.
Then generate the coverage review packet:

```powershell
python "literature-loop-capture\scripts\subquestion_coverage_review.py" `
  "LiteratureCaptures\<run>"
```

Give `coverage-review/subquestion-coverage-review.md/json` to the responsible
subquestion agent. After the subagent edits the JSON with
`coverage_score_0_to_5`, `coverage_decision`, rationale, gaps, and
`next_simple_queries`, run the loop gate for that exact subquestion:

```powershell
python "literature-loop-capture\scripts\run_subquestion_loop.py" `
  "LiteratureCaptures\<run>" `
  --subquestion-id "01_example" `
  --iteration-budget 1
```

If the gate returns `needs_coverage_scoring`, send the packet back to the
subagent; do not continue. If it returns `needs_openalex_grounding_audit`, write
the OpenAlex grounding audit without exposing the API key:

```powershell
python "literature-loop-capture\scripts\run_subquestion_loop.py" `
  "LiteratureCaptures\<run>" `
  --subquestion-id "01_example" `
  --refresh-openalex-audit
```

Run the gate again. If it returns `ready_for_query_iteration`, do not export
`next_simple_queries` directly. The responsible agent must first write
`loop-state/<subquestion>/iteration-NN/query-rationale-review.json` from the
seed ledger, references, gaps, prior query outcomes, and user comments. Then
build the reviewable amendment:

```powershell
python "literature-loop-capture\scripts\query_iteration_review.py" `
  "LiteratureCaptures\<run>" `
  --subquestion-id "01_example" `
  --iteration 2
```

Open `loop-state/<subquestion>/iteration-02/iteration-review.html` in the
agent built-in browser or localhost page, record any user comments, and approve
`query_iteration_plan_approval` only after corrections are resolved. Only then
continue discovery from the approved amendment without losing subquestion and
publisher provenance:

```powershell
python "literature-loop-capture\scripts\continue_query_iteration.py" `
  "LiteratureCaptures\<run>" `
  --approved-query-plan "LiteratureCaptures\<run>\loop-state\01_example\iteration-02\query-plan-amendment.json" `
  --iteration 2 `
  --total-iterations 2 `
  --year-start 2021 `
  --year-end 2026
```

This runs the next one-page discovery iteration for the affected
subquestion/publisher/query groups and writes the next
`query-refinement/iteration-NN/query-refinement-agent-brief.md`.
Only approved `broad_discovery` rows flow into this command. Exact OpenAlex
targets, manual holds, and recommended references remain ledgers until the
primary subquestion reaches a terminal state and the reference/PDF follow-up
phase starts.
After query iteration starts, repeat the full evidence loop, not only search or
abstract screening. After that discovery iteration, repeat the same cycle: title/snippet review,
abstract preview for agent-picked links, capture only approved full text, write
or update reading notes, extract high-value seeds and reference markings, update
the gap list, then rerun coverage review. The coverage decision must use the
cumulative full text, reading notes, seed ledgers, references, and gaps from
all previous rounds of that subquestion, not just the newest query page.
If it marks `sufficient`, move to the next subquestion. If it marks
`stop_with_gaps`, record the gaps in `subquestion-summary-zh.md` and move on
only after the user-specified iteration budget is exhausted.

The search subagent first performs `search_page_triage` with `rcs_0_to_10`.
This pass is intentionally inclusive: RCS >= 5 queues abstract preview; RCS <= 4
usually iterates keywords or stops the branch after the iteration budget. It
must not approve full-text capture. After `abstract_preview.py`, the appropriate
subquestion/search subagent performs `abstract_capture_review`: it reads the
abstract preview together with the earlier title/snippet/context and only then
may set `ready_to_capture` for supported-publisher candidates with RCS >= 7 and
specific reasoning. The agent's rationale must be saved as `rcs_reasoning`;
Python must not silently decide relevance from ranks alone.

Use this scoring rubric consistently:

- `8-10`: foundational or seminal evidence for the subquestion; in search-page
  triage this still means abstract preview first.
- `6-7`: highly relevant evidence; capture supported-publisher candidates only
  during `abstract_capture_review` when RCS is at least 7.
- `4-5`: partial evidence; use abstract preview around 5 and iterate/stop
  around 4 unless the agent gives a specific reason.
- `2-3`: tangential evidence; iterate query unless the iteration budget is spent.
- `0-1`: off-topic, auth failure, or empty/invalid discovery.

For every subquestion, maintain a short evidence ledger in the subquestion
summary or coverage review:

```text
coverage_score_0_to_5:
coverage_decision: sufficient | iterate_query | stop_with_gaps | blocked
captured_primary_evidence:
captured_reference_evidence:
abstract_only_evidence:
named_resources_searched:
named_resources_not_yet_searched:
next_queries_or_blockers:
```

For each subquestion, keep looping until one of these is true:

- the responsible subquestion agent has read all captured full text,
  `reading-note-zh.md` files, high-value seed ledgers, reference picks, and gap
  lists for the current iteration, then records
  `coverage_decision=sufficient`
- the iteration budget is exhausted and the subagent records the remaining
  evidence gaps
- publisher login/auth/zero-candidate checks stop the run and the user re-authenticates

Do not move to the next subquestion just because a page was searched. The
subquestion agent must read the captured article folders, reading notes,
figures/tables, extracted high-value seeds, and selected references, then decide
whether the current evidence covers definitions/resources/methods/applications/
gaps expected for that subquestion. Only then write `subquestion-summary-zh.md`
or request another short-query iteration.

## Commands

Prepare publisher login readiness for the connected Chrome/OpenCLI profile:

```powershell
Log into the required publisher sites in the Chrome/OpenCLI profile. Use `opencli profile use <name>` or `OPENCLI_PROFILE=<name>` when more than one OpenCLI profile exists, and use `--opencli-session` when a non-default OpenCLI session is needed.
$env:OPENALEX_API_KEY = "<required-openalex-api-key>"
```

Run bounded discovery after approval. This writes discovery audits and
query-refinement packets; it does not capture full text yet:

```powershell
python "literature-loop-capture\scripts\incremental_capture.py" `
  --claim "your research question or review claim" `
  --approved-query-plan "LiteratureCaptures\query-plan-preview\query-plan-preview.json" `
  --review-context "optional review outline or section labels" `
  --rounds 3 `
  --max-queries 3 `
  --max-pages 1 `
  --max-results-per-page 20 `
  --discovery-only `
  --discovery-backend opencli `
  --write-query-refinement-packets `
  --include-structured-publishers `
  --output-root "LiteratureCaptures"
```

After the literature-search subagent scores candidates and writes
`query-refinement-recommendations.json`, run `apply_query_decisions.py` and
capture only the resulting agent-approved `capture-queue.csv` with
`capture_decision_queue.py`. Direct first-N full-text capture is disabled for
new root runs unless `--allow-first-n-capture` is supplied for intentional
bounded debugging.

When starting a background capture from PowerShell, pass arguments as an array
to `Start-Process`; do not build one large quoted command string. This avoids
silent argument splitting failures:

```powershell
$args = @(
  "-u", "literature-loop-capture\scripts\incremental_capture.py",
  "--claim", "your English research question",
  "--rounds", "5",
  "--max-queries", "4",
  "--max-pages", "1",
  "--max-results-per-page", "2",
  "--discovery-only",
  "--discovery-backend", "opencli",
  "--write-query-refinement-packets",
  "--approved-query-plan", "LiteratureCaptures\query-plan-preview\query-plan-preview.json",
  "--include-structured-publishers"
)
Start-Process -FilePath "python" -ArgumentList $args -WorkingDirectory "literature-loop-capture" -WindowStyle Hidden
```

Useful options:

- `--query`: advanced exact-query override; can be repeated. Do not use it for
  the user's thesis/research topic. It bypasses dynamic subquestion planning
  and creates `user-provided-XX` query families.
- `--search-url`: pass an already verified publisher search URL for Elsevier,
  ACS, Wiley, or Springer; other publishers are outside this structured route.
- `--review-context`: optional outline or review structure used only for note
  triage and summary fit.
- PDF fallback and MinerU conversion are out of scope for the OpenCLI
  structured-capture validation. Use the separate Supplemental PDF Follow-up
  route only after the reference/final follow-up gate.
- `--include-structured-publishers` / `--no-structured-publishers`: include or
  skip structured publisher searches. Structured publishers are included by
  default.
- `--openalex-grounding` / `--no-openalex-grounding`: OpenAlex metadata
  grounding is enabled by default and required for normal broad-topic runs.
  `--no-openalex-grounding` is only for explicit debugging or exact manual
  query overrides, and approved-plan validation rejects missing or empty
  OpenAlex metadata in the normal loop.
- `--sciencedirect-route direct`: legacy/debug option only. Do not use it in
  the standard flow; supported structured ScienceDirect/Elsevier capture must
  use literature-loop-capture direct publisher URLs.
- `--settle-ms`: search-result page settle time. Keep `8000` unless a discovery
  audit proves lower waits still return complete result links.
- `--no-springer`: optional debugging or rate-limit control when the user
  explicitly wants to exclude Springer from an otherwise four-publisher
  structured run.
- `--no-smart-scroll`: debug mode for article pages where fast scrolling misses
  figures or tables.

Validate a run:

```powershell
python "literature-loop-capture\scripts\validate_outputs.py" "LiteratureCaptures\<run-folder>"
```

Aggregate agent-picked references for follow-up capture only after all primary
subquestions have reached a terminal coverage state:

```powershell
python "literature-loop-capture\scripts\reference_followup.py" "LiteratureCaptures\<run-folder>" --candidate-pool-size 20
```

In default `agent-picked` mode, all reading-note recommended references are
retained after dedupe; `--candidate-pool-size` only bounds how many rows per
subquestion get Crossref metadata checks by default. Use
`--refs-per-important-paper N` only when the user explicitly wants to cap how
many recommendations are accepted from each close-read article. Use
`--no-crossref` when network access is unavailable or when only a local triage
list is needed.

For references whose queue action is `manual_hold`, keep the URL/DOI and reason
in `followup-capture-queue.csv`. Do not run PDF or MinerU extraction in this
OpenCLI structured-capture route.

Create a short-name organized export copy after completion or interruption:

```powershell
python "literature-loop-capture\scripts\organize_outputs.py" "LiteratureCaptures\<run-folder>"
```

Export `_knowledge/`, final overview/summaries, captured article ingest files,
reading notes embedded in `article.md`, copied figures/tables, query
history, and lightweight wiki navigation/process pages as a LLM Wiki project:

```bash
python literature-loop-capture/scripts/llm_wiki_export.py "LiteratureCaptures/<run-folder>"
```

By default this writes
`LiteratureCaptures/<run-folder>/llm_wiki_project_export/<project-slug>/`. Use this
when the user's next knowledge layer is nashsu/llm_wiki or a similar local
wiki app. It creates a project-shaped package with `purpose.md`, `schema.md`,
`wiki/`, `raw/sources/`, `raw/assets/`, `raw/provenance/`, and
`manifest.csv/json`. The `wiki/` tree contains overview/index/log,
subquestion, query-history, and ledger pages only; article-level source pages,
concepts, and entities are produced by LLM Wiki ingest, not by the exporter.
Relationship links that should appear in the LLM Wiki graph must be written as
wikilinks (`[[page-stem|label]]`); plain Markdown links are retained only for
opening files. The `raw/sources/` tree
contains curated Markdown intended for LLM Wiki ingest: each captured article
occurrence gets one `article.md` with reading-note context embedded plus
non-duplicate note/reference Markdown.
Do not place duplicate `fulltext.json`, `fulltext.md`, and
`captured-fulltext.md` into `raw/sources`; preserve those audit/machine files
under `raw/provenance/`. Source PDFs are not copied into the LLM Wiki project:
after MinerU/API normalization, Markdown full text is the article ingest
source, and the canonical `source.pdf` remains in the capture folder. Figures
and raw CSV tables are copied into each article occurrence's mirrored
`raw/assets/articles/.../assets/` folder. Article/source Markdown links to
those assets, and image assets should be embedded with Markdown image syntax
where useful so the user can inspect text and figures together while keeping
non-text assets outside `raw/sources` and out of the ingest queue.

The older raw-source-only export is still available for compatibility:

```bash
python literature-loop-capture/scripts/llm_wiki_export.py "LiteratureCaptures/<run-folder>" --raw-sources
```

## Outputs

Each run writes:

- `question.json`
- `grounding-notes.md`, `exploration-sources.csv/json`, and
  `query-plan-preview.md/json` when running the mandatory preflight
- `openalex-grounding.md/json`, `agent-query-plan-packet.md`, and
  `agent-query-plan.json` before query-plan validation and preview generation
- `query-rounds.json`
- `discovery-audit.csv` and `discovery-audit.json`
- `coverage-review/subquestion-coverage-review.csv/json/md` after running
  `subquestion_coverage_review.py`
- `run-summary.csv`, `run-summary.json`, and `run-summary.md`
- `reference-candidates.csv/json/md`, `final-reference-selection.csv/json/md`,
  `reference-provenance.csv/json/md`, and compatibility
  `reference-followups.csv/json/md` after running `reference_followup.py`
- `followup-capture-queue.csv`, `followup-capture-queue.json`, and
  `followup-capture-queue.md`, marking each reference as `capture`,
  `manual_hold`, or `blocked`
- `pdf-followup-queue.csv/json`, `manual-pdf-hold.csv/json`, and per-subquestion
  supplemental PDF queue/hold files after `supplemental_followup.py` runs
- `overview-materials.md` and `OVERVIEW_REQUIRES_AGENT.md` after running
  `reference_followup.py`; final `overview.md` is written later by agent
- `capture-log.jsonl`

Each subquestion writes:

- `subquestions/<group>/<id>/subquestion.json`
- `subquestions/<group>/<id>/agent-brief.md`
- `subquestions/<group>/<id>/subagent-prompt.md`
- `subquestions/<group>/<id>/subagent-response.md`
- `subquestions/<group>/<id>/reading-notes-index.csv`
- `subquestions/<group>/<id>/reference-candidates.csv/json/md` after
  reading-note recommended references are deduped and metadata-checked
- `subquestions/<group>/<id>/final-reference-selection.csv/json/md` as a
  compatibility ledger of reading-note-approved references in `agent-picked`
  mode
- `subquestions/<group>/<id>/reference-provenance.csv/json/md`
- `subquestions/<group>/<id>/followup-capture-queue.csv/json/md`
- `subquestions/<group>/<id>/subquestion-summary-zh.md`, written by the responsible
  subquestion agent
- `subquestions/<group>/<id>/sources/<source>/articles/primary_001/`
- `subquestions/<group>/<id>/references/<source>/articles/ref_001/`
  for second-level reference captures
- `subquestions/<group>/<id>/references/pdf/<source>/articles/ref_001/`
  for Supplemental PDF Follow-up captures from exact-target and reference
  ledgers

Each captured article folder writes:

- `metadata.json`
- `fulltext.json`
- `fulltext.md`
- `captured-fulltext.md`
- `snapshot.html` only when `--write-snapshot-html` is explicitly used
- `references.json` and `references.md` when references are detected from
  structured XML/HTML full text.
- `structure.json`
- `figures/manifest.json`, downloaded image files when available, and
  `figures/index.md`; per-figure JSON/caption sidecars are disabled by default
- `tables/manifest.json`, `tables/table-XX.csv`, and `tables/index.md`;
  per-table caption sidecars are disabled by default
- `auto-extract-note.md`
- `NOTE_REQUIRES_AGENT.md` until agent writes `reading-note-zh.md`

Supplemental PDF article folders also write:

- `source.pdf`
- `pdf-capture-status.json`
- raw MinerU output under `mineru/`
- canonical `mineru/fulltext.md` produced from the MinerU API result zip and
  then normalized into the standard capture files

`run-summary.csv` uses stable review-friendly columns including subquestion
group, subquestion ID/slug/text, publisher, source bucket, source role
(`primary` or `reference`), query round/family/text, page, discovery rank,
capture depth, parent article/reference for second-level captures, agent owner,
status, title, authors, year, journal, DOI, URL, abstract, keywords, full-text
length, section/figure/table counts, note status, worth-close-reading triage,
agent score/status, reference provenance, article folder, capture time, and
review-context fit.

`reference-candidates.csv` uses stable follow-up triage columns including
subquestion ID/slug, query round, query family, subquestion, query text, rank,
score, reference text, source article, parsed DOI, Crossref
DOI/title/authors/year/container/type, verification status, capture hint,
capture query, capture notes, score basis, and agent score/reason/status
fields. In the default `agent-picked` mode, score basis records that the
reference was selected during reading-note writing and `approval_status` should
be `agent_picked_approved`. Python may dedupe, check Crossref metadata, and
route rows, but it does not create the relevance selection.

`overview-materials.md` is not a final synthesis. It is only a compact evidence
index for agent to use while writing the final `overview.md`.

`followup-capture-queue.csv` is the handoff back to the capture workflow. In
`agent-picked` mode, rows with `action=capture` have reading-note approval plus
an inferred supported structured publisher route; they should be sent
back through
OpenCLI publisher discovery-only `incremental_capture.py`, then abstract
preview and `capture_decision_queue.py` for structured publisher full-text capture. Rows with
`action=manual_hold` are outside configured routes or need manual metadata
reconciliation/source inspection.

## Agent Reading Notes

After a bounded CLI run returns, inspect all rows with
`note_status=pending_agent`. For each article, read:

- `captured-fulltext.md`
- `source/fulltext` artifacts produced by OpenCLI structured capture
- `figures/index.md` and available figure images
- `tables/index.md` and `tables/*.csv`
- `metadata.json`

Then write `reading-note-zh.md` in Chinese. The note must cover:

- Keshav first pass: problem, background, contribution, article category,
  structure, title/abstract/introduction, headings, conclusion, references, and
  the five Cs.
- Keshav second pass: methods, data, experiments, key evidence,
  figure/table/graph checks, statistical or metric checks when applicable, and
  relevant unread references.
- Keshav third pass: reconstruct the author's argument, test assumptions,
  identify limitations, hidden weaknesses, missing citations, reusable ideas,
  evidence gaps, and future work/query actions.
- subquestion fit: how the article changes the coverage score and next action
  for the current subquestion.
- typed seed ledger: named resources, datasets/benchmarks,
  methods/models/workflows, evaluation terms/metrics, cited seed papers,
  gaps/blockers, and proposed next simple queries, each tied to the current
  `subquestion_reading_lens`.

Use this required template. Treat it as a multi-pass, goal-specific reading
process, not a single start-to-finish summary pass. Do not replace it with a
loose three-section summary:

```markdown
# 阅读笔记：<title>

## 第一遍：Five Cs

- Category:
- Context:
- Correctness:
- Contributions:
- Clarity:
- 是否继续第二遍:
- 与当前 subquestion 的初步关系:

## 第二遍：内容、图表、证据

- 文章主要做了什么:
- 研究对象、数据来源与方法:
- 关键证据:
- 图表/表格检查:
- 统计、指标或实验设计检查:
- 主要结论:
- 相关但未读 references:

## 第三遍：虚拟复现与批判

- 如果复现/重建这项工作，需要的数据、步骤和假设:
- 隐含假设:
- 方法或证据弱点:
- 可能缺失的关键引用:
- 可复用思想:
- 后续 query/reference 行动:

## 精读与跟进判断

- 值得精读:
- worth_close_reading_score_0_to_5:
- 对 subquestion coverage 的影响:
- 推荐进入 reference follow-up 的文献:

## Typed Seed Ledger

- named_resources:
- datasets_benchmarks:
- methods_models_workflows:
- evaluation_terms_metrics:
- cited_seed_papers:
- gaps_blockers:
- proposed_next_simple_queries:
- seed-driven iteration decision:
```

For articles marked worth close reading, also write `recommended-references.md`,
`recommended-references.csv`, and `recommended-references.json` in the article
folder. Default to 2 recommended references per important article, but choose
0 when the article is not worth close reading or has no usable references, and
choose more only when the subquestion still has an explicit coverage gap. Each
recommendation must include the reference text, reference index when known,
citation context, reason for following it, relation to the current subquestion,
and whether it should become a reference capture, an exact-target/reference
ledger row, or a manual hold.

Do not mark `note_status=completed_agent` unless the note includes all required
Keshav template headings. If the article lacks figures, tables, statistics, or
references, write `not available in capture` under the relevant heading rather
than omitting the heading.

## Reference Follow-up

After all primary subquestions have reached `sufficient`, `stop_with_gaps`, or
`blocked`, use `reference_followup.py` to aggregate the references already
recommended by reading notes for each query round/subquestion. Do not run this
after only one subquestion unless
the user explicitly asks to debug that subquestion in isolation. The default
mode is `--reference-mode agent-picked`. The script:

- reads each captured article folder's `recommended-references.csv/json`
- reads recommendations from every completed primary iteration for the
  subquestion, not only the latest round
- keeps recommendations only from articles marked worth close reading, unless
  the article folder contains `recommended-references.allow`
- dedupes the agent-picked pool per subquestion
- retains all reading-note recommended references in default `agent-picked`
  mode after dedupe; it does not apply a top-N relevance cutoff
- checks DOI and bibliographic metadata with Crossref only for a bounded
  metadata pool, default `--candidate-pool-size 20`
- writes root `reference-candidates.csv/json/md`
- writes each subquestion's `reference-candidates.csv/json/md`
- writes each subquestion's `final-reference-selection.csv/json/md` as a
  compatibility ledger of reading-note-approved references, not a second
  selection task
- writes each subquestion's `reference-provenance.csv/json/md`
- writes each subquestion's `followup-capture-queue.csv/json/md`; root queue
  files are also written when the script runs at the run root
- writes `overview-materials.md` and `OVERVIEW_REQUIRES_AGENT.md`
- suggests the next capture route through `capture_hint`

The Python score is not the intellectual judgment in default mode. The
responsible subagent selects references while writing reading notes; Python only
dedupes, verifies, and queues. Do not run a second subagent/agent relevance
review over these rows. If no reading-note recommendations exist, do not use
Python ranking as a replacement; send the article folders back for reading-note
or reference-selection repair before running follow-up capture.

Reference follow-up is mandatory for broad literature review runs, but it is a
post-primary-coverage phase. Do not stop after writing `reference-candidates.*`
or `final-reference-selection.*`. Continue routed rows directly:

- supported structured publishers: run the same OpenCLI publisher discovery ->
  browser-first abstract preview with OpenAlex fallback only for still-missing
  abstracts -> capture queue -> structured publisher full-text capture loop with
  `capture_depth=2`.
- Science/AAAS, Nature-family, and arXiv references: keep them out of the
  structured structured publisher queue and route them through Supplemental PDF Follow-up.
- unmodeled purchased publishers: record `manual_hold` with URL/DOI evidence
  and do not run automatic extraction in this route.
- open or unavailable resources: record as `manual_hold` with a concrete reason
  and preserve enough DOI/URL/title evidence for manual capture or a later
  targeted audit. Do not automatically add a named-resource row to the next
  broad discovery iteration; only do so if the user explicitly reopens the
  affected subquestion and approves a new broad search plan.

Only agent-verified exact OpenAlex targets from query-iteration ledgers enter
this terminal phase beside reading-note references. Treat them as named target
ledgers for routing: supported structured publishers can be captured by the
structured reference route, Science/Nature/arXiv-like rows go to supplemental
PDF routing, and unsupported rows stay in manual hold. Unverified,
`needs_agent_disambiguation`, `semantic_mismatch`, generic-word matches, and
outside-anchor matches remain rejected/manual audit rows; `reference_followup.py`
and `supplemental_followup.py` must not promote them to structured or PDF
capture queues. Do not mix exact targets into the `broad_discovery` queue
during this phase.

If important references are known by name but not present in extracted
`references.json`, create an agent-picked recommendation row manually with the
source article, citation context or rationale, and target query. Then run
`reference_followup.py` so Crossref and queue files are regenerated.

While writing reading notes, reference scoring must answer "does this reference
deepen or validate the current subquestion?" not "is this citation generally
famous?" Use:

- `5`: essential seed/reference; capture or query next unless blocked.
- `4`: likely high-value; abstract preview or metadata check required.
- `3`: plausible but indirect; keep as candidate, do not capture yet.
- `2`: background only; record but do not pursue in this bounded run.
- `1`: off-scope for the subquestion.
- `0`: invalid, duplicate, or unverifiable.

For each selected reference, save the reason in terms of the subquestion:
which missing evidence layer it fills, which captured article cited it, and
whether it should become a direct reference capture or a new named-resource
query. This is the mechanism by which the agent converts its emerging
understanding into deeper, more precise searches.

When full text contains numbered references, use those numbers first in
`recommended-references.*`. For example, if the body cites `[12]` or `(12)`,
copy the sentence/window around that citation, then map it to reference 12 in
the References section. Crossref metadata reconciliation is limited to the
agent-picked candidate pool and should not be run against every reference in
every paper. It checks DOI, title, year, container, and related registered
metadata; it does not judge whether the paper itself is reliable.

## llm_wiki Project Export

Use this after final `overview.md`, `subquestion-final-summaries-zh.md`, and
the latest `knowledge_staging.py` refresh exist. The purpose is to feed
nashsu/llm_wiki with the already curated literature dossier as a local project,
not to replace the capture workflow or ask llm_wiki to crawl publishers.

Before running the terminal export, run the LLM Wiki readnote quality gate.
This gate is mandatory because captured batches may include `maybe`, duplicate,
manual placeholder, or exact-target false-match articles that are useful during
screening but should not become LLM Wiki source pages.

The quality gate is:

1. Enumerate every canonical article folder under
   `subquestions/**/articles/*`.
2. For every article with normalized full text (`captured-fulltext.md`,
   `fulltext.md`, or `fulltext.json`) but no `reading-note-zh.md`, dispatch the
   responsible subquestion agent to write the missing reading note from the
   article folder and subquestion lens before export. Do not let Python invent
   the note.
3. For every article with no reading note and no normalized full text, record it
   in a blocker ledger such as `missing-reading-notes-no-fulltext.md` and omit
   it from `raw/sources/articles/**/article.md`. Metadata-only placeholders are
   not valid LLM Wiki ingest sources.
4. Read all `reading-note-zh.md` files and keep only article folders that the
   agent judged worth close reading for the current project: explicit
   `worth_close_reading: true/yes/是` with
   `worth_close_reading_score_0_to_5 >= 3.0`, or an equivalent score >= 4.0
   fallback when no negative marker exists.
5. Drop explicit `false/no/否`, retrieval mismatch, low-relevance, off-topic,
   negative-cleaning, or "not relevant" notes even when the article was captured
   successfully. Drop duplicate DOI/title occurrences after keeping the best
   representative by DOI first, then normalized title.
6. Write `readnote-curation-ledger.csv` and
   `raw/sources/dossier/root/readnote-curation-ledger.md` with keep/drop/blocked
   decisions and reasons.
7. Regenerate or remove unfiltered dossier pages that can reintroduce dropped
   articles into LLM Wiki ingest, including broad `papers`, `captured_papers`,
   `reading_notes_index`, `figures_tables`, `recommended_references`, and
   `manual_pdf_needed` pages when they still list excluded articles.
8. Verify that `manifest.csv`/`manifest.json` contain no paths to removed
   article, asset, provenance, or dossier files, and that the manifest article
   count equals the actual number of `raw/sources/articles/**/article.md` files.

The result should be a curated LLM Wiki package: `wiki/` keeps navigation and
process pages; `raw/sources/articles/**/article.md` contains only agent-reviewed
article sources suitable for LLM Wiki to turn into source, concept, entity, and
relationship pages.

Keep article ingest pages compact. `raw/sources/articles/**/article.md` should
include bibliographic metadata, abstract, local figure/table links, captured
body text, and `reading-note-zh.md` context, but it should not append full
`references.md` or `recommended-references.md` sections. It should also strip
tail `References`, `Bibliography`, `Literature Cited`, or `参考文献` sections
from captured body text before writing `article.md`. Those citation lists
usually duplicate long reference strings, inflate embedding chunks, and distract
LLM Wiki from the captured article body. Keep reference files in
`raw/provenance/**` and curated dossier ledgers for audit and follow-up instead.

```bash
python literature-loop-capture/scripts/llm_wiki_export.py "$RUN_DIR"
```

Default output:

```text
<run>/llm_wiki_project_export/<project-slug>/
  README.md
  purpose.md
  schema.md
  manifest.csv
  manifest.json
  wiki/
    index.md
    overview.md
    log.md
    synthesis/
    subquestions/
    queries/
    ledgers/
  raw/
    sources/
      dossier/
      articles/<subquestion>/<article>/<occurrence>/article.md
    assets/
      articles/<subquestion>/<article>/<occurrence>/assets/
    provenance/<subquestion>/<article>/
```

The `wiki/` tree is limited to overview/index/log/subquestions/queries/ledgers
navigation and process pages. Article-level source pages, concepts, entities,
and relationship pages are generated by LLM Wiki native ingest from
`raw/sources/articles/**/article.md`, not authored by the exporter.

The LLM Wiki export uses normalized Markdown as the article source. It must not
copy `source.pdf` into `raw/assets/` or use a PDF-only folder as an ingestable
article. If a PDF has not yet produced `captured-fulltext.md`, `fulltext.md`,
`fulltext.json`, a reading note, or figure/table artifacts, finish MinerU API
normalization first or omit that article from the terminal export. Keep
canonical PDFs only in the capture folders and `_knowledge` audit/dropbox
workflow.

`_knowledge/` remains the refreshable human preview layer.
`llm_wiki_project_export/<project-slug>/` is a terminal export folder and may be
deleted/regenerated. Do not put user PDF dropboxes there. Do not let
llm_wiki-generated summaries overwrite canonical capture folders, reading
notes, coverage reviews, or query ledgers. If llm_wiki finds a new gap or
relationship, record it as a new user/agent comment and route it back through
the normal coverage or targeted follow-up gates.

Use `--raw-sources` only when a raw-source-only package is needed instead of a
full app project.

Capture hints are intentionally conservative:

- `structured publisher-acs`, `structured publisher-wiley`, `structured publisher-elsevier`, `structured publisher-springer`: use the
  authorized literature-loop-capture publisher route after reading-note recommendation and
  metadata/routing checks
- `supplemental-pdf-science`, `supplemental-pdf-nature`,
  `supplemental-pdf-arxiv`: do not run the structured structured publisher queue; route
  through `supplemental_followup.py`, `pdf_followup_capture.py`, and the MinerU
  API normalization path
- other purchased publishers: keep as `manual_hold` until explicitly supported
- `manual-or-structured publisher-search`: inspect manually. Open-access capture is outside the
  standard flow unless the user explicitly enables it.

Use `followup-capture-queue.csv` after `reference_followup.py` has deduped and
routed the reading-note recommendations:

- `action=capture`: run the recommended second-level discovery through
  `incremental_capture.py --discovery-only`, then repeat abstract preview and
  capture queue approval before full capture. The resulting follow-up article
  folder must land under the same subquestion's
  `references/<source>/articles/` tree with `capture_depth=2`, must keep
  `source_role=reference`, `parent_article_dir` and
  `parent_reference_index`, and must receive the same structured outputs and
  `reading-note-zh.md` treatment as first-level articles.
- `action=manual_hold`: keep the reference for manual extraction because it is
  outside configured routes, unavailable, or has metadata that does not align
  with the cited reference.

Do not run reference follow-up again on second-level captures by default.
Third-level reference chasing requires an explicit user request and a new
bounded count.

## Supplemental PDF Follow-up

Run supplemental PDF follow-up only after primary subquestions are terminal
(`sufficient`, `stop_with_gaps`, or `blocked`) and reading-note recommended
references have been aggregated, deduped, and routed. This phase is for
normalized PDF reference captures after the reference routing gate; it does not
replace structured publisher capture.

Before asking the user to download any PDFs, run Knowledge Staging. The staging
folder is the user's information workspace for the whole run, not just a PDF
handoff:

```bash
python literature-loop-capture/scripts/knowledge_staging.py "$RUN_DIR"
```

`_knowledge/` groups all already captured evidence by subquestion: canonical
captured primary/reference/PDF article folders, raw capture occurrences,
reading-note indexes, paper cards, figure/table indexes, important seed lines,
recommended reference ledgers, coverage status, query/search journeys, duplicate
reports, an llm_wiki-oriented attachment manifest, and a clean manual PDF
dropbox. Manual PDF lists must show only rows with a concrete DOI. Rows without
DOI remain in the source audit CSV/JSON files until metadata is clarified; do
not ask the user to download them.

The user-facing manual PDF location is:

```text
_knowledge/subquestions/<subquestion_id>/manual_pdf_dropbox/
```

Each subquestion also receives `manual_pdf_to_download.csv`,
`manual_pdf_already_supplied.csv`, and `manual_pdf_download_list.csv`.
`manual_pdf_to_download.csv` is the short action list for the user.
`manual_pdf_download_list.csv` records the full DOI-backed audit list: DOI,
title, status, suggested filename, and canonical target folder. The user may
place all PDFs for one subquestion directly in that subquestion's dropbox
instead of opening one nested folder per paper. After the user confirms PDFs
were placed in `_knowledge`, ingest only newly added valid PDFs back into their
canonical `subquestions/<group>/<subquestion>/references/pdf/manual/...` article
folders, then run MinerU API normalization, reading-note subagents, and
`knowledge_staging.py` again. This keeps the human-facing knowledge layer and
the machine-facing capture folders synchronized without moving the original
capture evidence.

`_knowledge/` is a refreshable generated view, not a replacement for canonical
capture folders. Regeneration may remove and rewrite generated Markdown/CSV
indexes, root `papers/`, `seeds/`, `references/`, `overview/`,
`llm_wiki/`, and per-subquestion `paper_cards/`, but it must preserve
user staging folders such as `manual_pdf_dropbox/` and any legacy
`manual_pdf_inbox/`. Do not delete user-supplied PDFs during refresh. Paper
card filenames must be collision-resistant, and subquestion IDs used as folder
names must be sanitized so unusual IDs cannot write outside the intended
`_knowledge/subquestions/` tree.

For each subquestion, `_knowledge/subquestions/<id>/` should contain the human
review entry points:

- `overview.md`: subquestion text, current coverage score/decision, counts, and
  links to the rest of the dossier
- `papers.md`: canonical deduped papers with duplicate occurrence paths
- `paper_cards/*.md`: one card per canonical paper linking metadata, reading
  notes, recommended references, artifacts, and duplicate occurrences
- `figures_tables.md`: in-place index of existing PDFs, figures, and tables;
  attachments are linked, not copied
- `important_seeds.md`, `recommended_references.md`, `query_journey.md`, and
  `coverage.md`: the research-thinking layer that explains how search terms,
  seeds, references, confidence, and gaps evolved

At the root, `_knowledge/papers/duplicate-report.md` records duplicate capture
groups, and `_knowledge/llm_wiki/attachments_manifest.csv` lists every
indexed artifact path, hash, article title, DOI, and subquestion without
creating an attachment copy. This is the prep layer for the terminal
`llm_wiki_project_export/<project-slug>/` export: it lets the user inspect papers,
notes, figures/tables, seeds, references, and query history by subquestion
before feeding the curated dossier into llm_wiki.

Treat query iteration as part of the research evidence, not as disposable
process logging. `knowledge_staging.py` must write `_knowledge/search_journey.md`
and `_knowledge/subquestions/<id>/query_journey.md` pages that preserve, by
subquestion, the initial approved queries, later rationale-review query changes,
coverage confidence/score entering each iteration, missing-evidence terms,
broad discovery rows, exact/manual targets, manual-hold reasons, and links to
the source rationale/amendment artifacts. Python may dedupe, format, and link
these rows, but it must not invent the reasoning; the displayed reasoning must
come from `query-rationale-review.*`, `query-plan-amendment.*`, coverage
decisions, seed ledgers, reading notes, or explicit user corrections.

Publisher routing remains split by capture path:

- Elsevier/ScienceDirect, ACS, Wiley, and SpringerLink stay on the structured
  reference route.
- `science.org`, Science/AAAS family journals, `nature.com`, Nature Portfolio
  family journals, and `arxiv.org` rows go to `pdf-followup-queue.csv/json`.
  Identify Science/Nature family rows from source metadata such as host, DOI
  prefix, publisher, and journal/venue title; do not use review-topic keywords.
- Unsupported rows go to `manual-pdf-hold.csv/json`; each row receives a target
  folder under `subquestions/<group>/<subquestion>/references/pdf/manual/`.
  That folder is the machine ingest destination. The user-facing drop location
  remains `_knowledge/subquestions/<subquestion_id>/manual_pdf_dropbox/`.

Before presenting a manual download list to the user, build it from the residual
manual rows after supported structured capture and supplemental PDF routing have
already run. Remove or separate rows that are already captured by DOI/title,
already have a normalized target folder, duplicate another manual row, duplicate
a captured reference/source, or have an obvious metadata mismatch. Show the user
only the actionable remainder as "needed manual PDFs"; keep optional and
skip/already-captured rows in the audit files, not in the short action list.
Manual rows are cumulative across rounds, but the user-facing list should be the
minimal residual list for the current terminal follow-up pass.

`supplemental_followup.py` dedupes the combined agent-verified exact-target and
recommended reference leads before writing capture queues, using DOI first,
then OpenAlex ID, arXiv ID, and normalized title fallback. It must keep
unverified, ambiguous, and `semantic_mismatch` exact-target rows out of capture
queues. `pdf_followup_capture.py` must also skip a queue row when its target
article folder already contains a valid
`source.pdf`, recording `already_captured` rather than downloading or
overwriting it. `supplemental_followup.py` is safe to rerun after PDF capture:
when an existing target folder already has a valid `source.pdf` plus a status
file, it may refresh metadata/queue rows but must not reset
`pdf-capture-status.json` back to `pdf_pending`. Before starting a queue that
contains Science/Nature rows, remind the user to confirm that the connected
OpenCLI browser profile is logged in to `science.org` and `nature.com` and can
download PDFs directly from those publisher sites. For arXiv, Nature, and
Science-family PDF rows, write canonical publisher PDF URLs into new queues
first and validate that the response starts with `%PDF` before writing
`source.pdf`. For Science/Nature, the browser-session path must prefer the
original publisher URL (`science.org` or `nature.com`) even when an older stored
queue row came from a publisher URL, because the user may have authenticated those
publishers directly in OpenCLI. If direct HTTP fails, open the canonical
publisher PDF URL in the authenticated OpenCLI browser session and try an
in-browser credentialed `fetch`, preserving the OpenCLI login cookies. Save the
returned bytes only when they start with `%PDF`, and record the attempted URLs,
status codes, content types, byte counts, and first-byte evidence when they do
not. legacy PDF URLs are fallback/provenance only for legacy Science/Nature PDF
queues, not the default route. If a Science/Nature page redirects to a login
page, record an auth blocker and wait for the user to reconnect instead of
treating it as a PDF extraction failure. Some PDF pages render inside Chrome's
PDF viewer, whose download/right-click controls are outside the webpage DOM; do
not rely on coordinate or menu automation in the default batch path. If direct
HTTP, browser-session fetch, and DOM download selectors do not yield a valid
PDF, record `blocked_pdf_viewer_download` so the user can manually save
the PDF into the relevant `_knowledge` subquestion dropbox, or approve an
explicit one-off UI/canonical-folder fallback when the dropbox path is not
practical.

Use the run directory variable consistently:

```bash
python literature-loop-capture/scripts/supplemental_followup.py "$RUN_DIR"
python literature-loop-capture/scripts/pdf_followup_capture.py "$RUN_DIR/pdf-followup-queue.csv" --opencli-session lit-pdf
MINERU_API_KEY="<mineru-api-key>" python literature-loop-capture/scripts/mineru_api_extract.py "$RUN_DIR"
```

The standard supplemental PDF route uses the MinerU API only. Do not run local
MinerU, `magic-pdf`, or other local model commands in this skill. The only
required credential is `MINERU_API_KEY`; scripts record only key presence and
never print the value. `mineru_api_extract.py` uploads valid `source.pdf` files,
polls MinerU, unpacks the returned zip into `mineru/`, writes
`mineru/fulltext.md`, and calls `mineru_normalize.py`.

For user-supplied PDFs, the default user action is to place PDFs in the
subquestion dropbox, not in the machine article folder:

```text
_knowledge/subquestions/<subquestion_id>/manual_pdf_dropbox/
```

Use `manual_pdf_to_download.csv` as the short action list and
`manual_pdf_download_list.csv` to map each PDF back to the canonical target
folder recorded by `manual-pdf-hold.*`, normally
`subquestions/<group>/<subquestion>/references/pdf/manual/articles/<article-id>/source.pdf`.
After the user confirms the PDFs are saved, ingest/sync each newly added valid
PDF from the dropbox into its canonical target folder as `source.pdf`, verify
`%PDF` bytes, run MinerU API extraction, normalize into the standard article
structure, and dispatch the same reading-note subagent workflow used for
publisher captures. Do not re-upload or overwrite PDFs whose target folder
already has a valid `source.pdf` and completed MinerU/normalization status
unless the user explicitly asks for reprocessing.

`mineru_normalize.py` is a normalizer, not a local parser in the standard
workflow. It consumes already-returned API markdown and writes the files
expected by reading notes, validation, overview, and export flows:
`fulltext.json`, `fulltext.md`, `captured-fulltext.md`, `structure.json`,
figure/table manifests and indexes, and `NOTE_REQUIRES_AGENT.md`. When the
MinerU API emits image or table assets, copy them into the standard
article-level `figures/` and `tables/` folders and record them in the same
manifest/index files used by structured publisher captures, rather than leaving
them only inside the raw `mineru/` folder.

The Supplemental PDF Follow-up data flow is:

1. agent-verified exact OpenAlex ledger rows and agent-picked recommended
   references enter `supplemental_followup.py`
2. supported PDF rows are deduped and written to `pdf-followup-queue.*`;
   unsupported rows are written to `manual-pdf-hold.*`
3. `pdf_followup_capture.py` writes one `source.pdf` per target article folder,
   skipping rows whose target already has a valid PDF
4. `mineru_api_extract.py` uses `MINERU_API_KEY` to write MinerU API output
   under `mineru/`
5. `mineru_normalize.py` writes the standard article files, figures, tables,
   and status metadata from the API output
6. the same reading-note/subagent, coverage, overview, and export machinery used
   for structured captures consumes the normalized article folders

After step 6, update cumulative subquestion coverage with the new PDF/reference
evidence and remaining manual blockers. Only if the user reopens a still-weak
subquestion should this produce another query plan; otherwise it feeds the final
overview and export.

## Agent Overview

Write `overview.md` only after reading all available article folders,
`reading-note-zh.md` files, figures, tables, run summaries, and follow-up
reference lists. For a technical question, the overview should cover:

- Current problem state: what the captured literature says the field is trying to solve
- Method landscape: method families, resources, datasets, evaluation practices, and
  representative papers
- New methods and trends: recent techniques, architectures, workflows, or data resources
- Limitations: evidence gaps, weak evaluations, unresolved technical bottlenecks,
  reproducibility issues, and conflicting findings
- Innovation opportunities: plausible next research directions or solution routes grounded in
  the captured evidence
- Follow-up capture: which top-ranked references should be captured next for each
  subquestion

Keep claims traceable to captured article folders, reading notes, and
`subquestions/<group>/<id>/reference-candidates.*`,
`final-reference-selection.*`, and `reference-provenance.*`.

Before writing or updating `overview.md`, perform a final audit:

- list all subquestion coverage scores and decisions
- list every query iteration that was run and every proposed next query not run
- list all high-value named resources/methods discovered from primary articles,
  references, and user feedback, plus their query-derivation-ledger action
- list reference-follow-up rows by action: captured, manual_hold, blocked
- list subagent artifacts or `main_agent_fallback` artifacts for each
  subquestion/query-refinement/coverage review, plus reading-note
  `recommended-references.*` provenance for reference follow-up
- state whether the overview is complete, partial, or blocked

If any audit item is missing, write/update a gap file first and do not present
the overview as final.

## References

Read `references/publisher-direct publisher-patterns.md` when debugging literature-loop-capture
publisher routes, result pagination, article-link extraction, or structured publisher PDF
fallback behavior.
