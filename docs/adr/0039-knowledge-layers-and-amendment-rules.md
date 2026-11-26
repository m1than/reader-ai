---
id: ADR-0039
title: Knowledge layers as append-only source packs, faceted tags, greedy retrieval with judges, the brain/v1 agent contract; rules for amending decided ADRs
status: proposed
date: 2026-11-26
verified_by: "RESULT.md of spikes retrieval-ablation, judge-gate and pack-links applying the decision rules in B10; SRV-INGEST property test (base packs, overlays, manifest, re-anchoring) and SRV-KB property test (pair link runs), both over recorded model outputs; SRV-SEARCH and SRV-MCP evaluation meets the brain/v1 targets; SRV-PIPELINE test of the check order and loop limit; trace-check green"
revisit_when: "Claude Haiku as caller stays below 90% of the strong model's retrieval sufficiency (point estimate) after two policy versions; find p95 reaches 2.5 s on the owner's Mac; the judged path degrades to judged:false on more than 20% of gated queries for a week (initial value); more than 200 packs are active in one manifest (initial value); or brain/v1 needs a breaking change"
supersedes: []
amends: [ADR-0001, ADR-0005, ADR-0007, ADR-0008, ADR-0009, ADR-0012, ADR-0015, ADR-0018, ADR-0019, ADR-0025, ADR-0032, ADR-0035, ADR-0038]
---

## Context

This ADR was first drafted on 2026-11-25 as ADR-0020 (PR #42) at stage 0. That number now belongs to the toolchain baseline, so it is renumbered ADR-0039 and restated against the code that exists on `V/slice-integration`.

Owner decisions, 2026-11-25:

1. Knowledge data is append-only. Adding a source adds an increment to the knowledge base and never changes what is already there. Removing a source is cheap.
2. The knowledge base is the second brain for any agent: an agent on a cheap model must find what it needs in few calls, fast, through a simple interface.
3. MLflow is the trace and evaluation tool.
4. Judge models select what an agent receives through MCP. In v1 they run on Claude. The method must let a local model replace Claude in a named place later without changing the method; local models themselves stay in v2 (ADR-0018).
5. Selection over graphs and embeddings uses greedy algorithms only. Several selection problems here are NP-hard; they are solved approximately, never by enumerating subsets.
6. Tools behind the interface will change; the interface stays.
7. Targets: a cheap model reaches at least 90% of a strong model's retrieval sufficiency; median 3 calls per answer or fewer; at most 2,000 tokens per answer; `find` p95 below 2.5 s.
8. Where two designs are both viable (who owns links between sources), both are built on a data set and the measurement decides.
9. This decision goes into a separate ADR that also sets the rules for amending decided ADRs and for keeping rework small.
10. Claude Haiku is the cheap model the system is built around. Prompts and rules are written carefully against real data, versioned in MLflow, and every run is traced against the prompt versions it used.

Owner answers to the open questions of the first draft, 2026-11-25:

11. A chain (Bitcoin, Ethereum) is a branch of `topic`, not a facet. The topic tree stays shallow; no deeper topology.
12. Reliability uses the Admiralty scale: source reliability A–F on the source profile, information credibility 1–6 on a finding or a link.
13. Learner state (L5) enters retrieval from stage 6.
14. Judge alignment is allowed: "we are not developing a model, we are building its second brain". Alignment rewrites judge instructions from the owner's labels and trains no model weights.
15. Money for indexing: the owner tops the credit up when it is needed. The agent measures the cost, records it, and asks for a top-up when a cap is reached; it does not wait for a share to be set first.
16. Prompts: the repository file is the source of truth. MLflow is the team's dashboard for traces, prompt versions, evaluation and labels.
17. Tokens per answer: `find` and `open` share a 2,000-token budget per answer through their `budget` argument; `brain_map` counts once per agent session.

Owner decisions, 2026-11-26:

18. In every selection the system makes, a model call that verifies runs only at the final step, on what the steps without a model have already selected. This covers `find` (B6) and the checks after drafting a lesson (ADR-0009, see Amends). Pair linking (B4) already works this way and stays as it is.
19. Context prefixes and L2 summaries stay at ingest. They are text that the search ranks on, not verification: a prefix written only for spans already found would never repair a span the search missed, which is where Contextual Retrieval's 49% reduction in failed retrievals comes from. `retrieval-ablation` still decides whether each is kept.
20. **Span identity moves to ADR-0008 locators.** A span is identified by its locator (material sha256, page, character offsets) plus a hash of its quoted text, with a re-anchoring job. Every stored citation migrates; the `<material_sha256>:<seq>` ids of FR-INGEST-17 are retired.
21. **The online judge runs inside `find`.** `judge.find` is called on the request path, last, on the selected cards (B6). The credential separation of ADR-0025 is amended as little as possible to allow it (B7b).
22. **Decision 18 applies to the pipeline in full.** After drafting: rule checks → lab verification → claim support → adversarial review, up to 3 loops, every return to the draft counted, and fact-check reads through the search route. This replaces SRV-PIPELINE's measured one-loop deviation.
23. **Everything is built now**, on the current single source: base packs, overlays, the manifest with compare-and-swap, garbage collection and purge, facets, and the full `find` cascade. Only pair linking (B4) waits for a second source (stage 7), because it needs one.

Model tags (F5) and the concept labels of B4 step 2 are proposals made before a selection, not verification, so decision 18 leaves them unchanged.

### State on 2026-11-26

The modules this ADR changes exist, with approved specs and code (`spec/modules/`, `server/src/reader/`):

| Module | What exists | Where |
| --- | --- | --- |
| SRV-INGEST | One frozen import per material, keyed `[material_sha256, converter_version]`; spans `material_spans` with page and bbox, id `<material_sha256>:<seq>` (FR-INGEST-13, -14, -17); soft delete and restore by `deleted_at`, tombstones only, no row ever deleted (FR-INGEST-30–32) | `server/src/reader/ingest/` |
| SRV-SEARCH | Chunks of 1,200 characters with 200 overlap (SRV-SEARCH.yaml:120); vectors keyed `(ingest_id, model, params_sha256, chunk_no)`, old keys ignored, never deleted (FR-SEARCH-4/5/7); exact scan, no vector index (`search/migrations/0001_search.py`); weighted RRF exists but hybrid is off: measured 24 of 40 against dense 27 of 40 (SRV-SEARCH.yaml:286-289); top-k by dense order, 300-character snippets | `server/src/reader/search/` |
| SRV-AGENT | Run tokens issued per call after the ledger reservation, bound to one user, model and output cap, revoked in `finally` (FR-AGENT-3; `agent/provider.py`); `agent-proxy` keeps at most 32 tokens and expires them at 1,500 s (`agent/proxy.py:35-36`); `Provider` port (FR-AGENT-6); model per task as data in `reader.yaml` `tasks` with aliases including `haiku` (FR-AGENT-6, -17); per-run, per-task, daily and monthly caps (FR-AGENT-9, -12) | `server/src/reader/agent/` |
| SRV-OBS | MLflow 3.18 behind Rotel (ADR-0032); text-free traces (INV-OBS-1/2); prompt registration by file sha (FR-OBS-15, ADR-0035); `version_key` over prompts, models and pipeline config (FR-OBS-21); bench datasets `search-bench-v1` and `tutor-bench-v2` with code-computed scores, no LLM judge (FR-OBS-22, -23; ADR-0038); `owner_labels` for lessons and answers (FR-OBS-24) | `server/src/reader/obs/` |
| SRV-PIPELINE | Whole source section in context (`SOURCE_MAX_CHARS` 300,000, `pipeline/source.py:22`); `draft → fact_check → adversarial_review`, one revise loop, a measured deviation (SRV-PIPELINE.yaml:16-19, `pipeline/stages.yaml`); fact-check against the lesson's own spans; outputs reused by `input_sha256` including the prompt file hash (FR-PIPELINE-3/5) | `server/src/reader/pipeline/` |
| SRV-TUTOR | Answers as worker jobs; `search(k=8)` with a byte budget, about 21,000 bytes of sources (SRV-TUTOR.yaml:99-109) | `server/src/reader/tutor/` |
| SRV-EVENTS, OPS-DEPLOY | One `worker` consuming every queue at concurrency 1 (FR-EVENTS-9); `agentnet` holds only `worker` and `agent-proxy` (FR-DEPLOY-3, VEC-DEPLOY-2) | `server/src/reader/jobs/`, `deploy/compose.yaml` |
| SRV-LESSON, WEB-NOTEBOOK, SRV-FRESH | Lesson revisions are content-addressed and never updated (INV-LESSON-6); citations carry `<material_sha256>:<seq>` span ids (VEC-LESSON-3, FR-NOTEBOOK-2); the freshness scan hashes its span inputs (FR-FRESH-3, -10) | `server/src/reader/lessons/`, `web/`, `server/src/reader/freshness/` |

Accepted ADRs this one builds on or touches: ADR-0021 (formulas and code in the text layer), ADR-0023 (provider token delivery), ADR-0025 (only the worker reaches `agent-proxy`), ADR-0028 (the bge-m3 `embed` service), ADR-0030 (the freshness check by web search), ADR-0032 (MLflow on SQLite as the trace backend), ADR-0035 (the worker writes assessments and registers prompts), ADR-0036 (owner settings editable in the UI), ADR-0038 (MLflow runs, datasets and review). ADR-0034 (opt-in trace content) was rejected, so traces stay text-free.

Measured numbers this ADR uses:
- 9,463 spans over 935 pages, about 10.1 per page (`docs/progress.yaml`, S0.spike.tutor-chunks). Span text as sent: mean 342 bytes, median 204, p95 1,155; 79 spans (0.8%) above 2,000 bytes (`spikes/tutor-chunks/RESULT.md`). Russian text ran at 1.9 bytes per token, about 0.53 tokens per byte (`spikes/tutor-call/RESULT.md`; ASCII JSON ran at about 0.32), so for the Russian pilot a span is about 180 tokens at the mean, about 107 at the median and about 610 at p95.
- Compile cost per stage, the floor reserves of `pipeline/stages.yaml` (the largest measured L02 cost, rounded up): draft $0.61, fact-check $0.25, review $0.53, revise $0.88. A measured L02 compile with one loop cost $2.42–$2.97 (`docs/progress.yaml`, `budget.compile_cap_usd`, set by the owner at $8 per lesson).
- Exact scan alone: 6.6 ms at 2,285 chunks (SRV-SEARCH.yaml:50). Live search p95 end to end: 173 ms (V.SRV-SEARCH-min).
- Haiku through the Agent SDK: CLI ready 0.89–1.20 s, result 1.82–2.10 s, $0.00026 per call with a prompt cache hit (`spikes/agent-sdk-container/RESULT.md`). One Sonnet call: 1.5–3.5 s (S0.spike.tutor-call).
- About 23,900 Russian source tokens for the 25 pages of lesson L02 (`spikes/compile-cost/RESULT.md`).

This ADR is `proposed`. Nothing in it is in force, and no task that depends on it starts, until the owner accepts it. If the owner rejects it, the agent removes the ADR-0039 sentences from the roadmap, the pointer lines and the gated tasks in `docs/progress.yaml` in one change, and the specs keep their current approved text.

## Part A — Rules for amending decided ADRs

| # | Rule |
| --- | --- |
| A1 | **Amend, never rewrite.** The Decision, Options and Rejected sections of every ADR are not rewritten by the agent, whatever its status line says. A new or amending ADR is in force only once its status is `accepted` |
| A2 | **Change by delta.** A change to an earlier ADR is a new ADR with `amends: [...]` in its frontmatter and an Amends table: ADR, section, was, becomes, why. Whatever the table does not name stays in force. `supersedes` is used only when an ADR is replaced as a whole; the status flip stays the owner's (H3). **Exception:** the owner may amend an ADR in place with a blockquote `> Amendment (owner, <date>), <subject>: …`, as in ADR-0020, ADR-0029 and ADR-0030. Such a note is the owner's decision and needs no new ADR |
| A3 | **One pointer line, one place.** The amended ADR gets exactly one edit: a blockquote as the first line of its Context, `> Amendment proposed by [ADR-NNNN](...) (date): <one line>. In force once ADR-NNNN is accepted; then, where the two disagree, ADR-NNNN wins.` Pointer notes that already exist elsewhere (above Context in ADR-0005, ADR-0008, ADR-0011, ADR-0023) stay where they are. The ADR-0028 form, notes "for the owner to add when this ADR is accepted", is superseded: the agent adds the pointer in the same change as the amending ADR. After acceptance the owner may shorten it |
| A4 | **Same change, every dependent artifact.** In the same PR, or the same merge into the integration branch:<br>• the pointer lines;<br>• one sentence in each affected roadmap row, prefixed `ADR-NNNN, once accepted:`;<br>• agent instructions (AGENTS.md) where a rule changes;<br>• `docs/progress.yaml` tasks, gated on an owner task to accept the ADR.<br>An approved spec changes only with its tests (H2), and only once the ADR is accepted |
| A5 | **Contracts in ADRs, mechanisms in config.** An ADR fixes interfaces, invariants and decision rules. Models, indexes, thresholds, prompts and judge versions are config or versioned policy, recorded on every trace; changing them needs no ADR. Changing a contract (a schema, the tool surface, an invariant) needs one. Numbers in an ADR that are not measured are initial policy values, marked as such |
| A6 | **Decide forks by a rule written first.** A fork with two viable options and a measurable difference becomes a spike whose decision rule is written in the ADR before the spike runs, in units the data set can resolve. The spike's RESULT.md applies the rule; a new ADR is needed only when no option passes or a revisit_when condition is met |
| A7 | **Measure at the last responsible moment.** A spike runs before the spec that consumes its numbers is approved, and no earlier than the data it needs exists, so no spike is redone because the design moved after it ran |
| A8 | **State the rework.** Each amending ADR states the files it changes now, the existing code that must change with a size estimate, and the approved spec ids that change with their tests |
| A9 | **A change that adds or amends an ADR is merged by the owner**, even when it raises no status: it changes what later tasks are allowed to do (amends ADR-0019 "Merging pull requests"). Under the single-PR integration model, the agent may merge such a branch into the integration branch, and the final integration PR to `main` waits for the owner |

This ADR is the first one written under these rules.

**Review round limit (owner, 2026-11-26).** A task blocks when `deep-reviewer` has not returned `VERDICT: approve` after two rounds on the same spec (ADR-0019 row of Amends). This is new and stricter than past practice: SRV-INGEST took four spec rounds, `compile-cost` and `trace-backend` three, and ADR-0029 six.

## Part B — Knowledge layers

### B1. Layers and their depth

| Layer | Unit | Depth bound | Card budget (tokens) | Built by | Lives in |
| --- | --- | --- | --- | --- | --- |
| L0 Source | Source profile: role, nature, reliability, `as_of`, `license`, language, cost | 1 node per source | ≤ 200 | Ingest; owner confirms at G1 | Profile overlay |
| L1 Span | One Docling element: paragraph, table, code block, formula, test vector. Nothing is split at ingest. A card larger than its budget is cut **at read time** (the budget is policy, not pack identity, P2). For `license: shareable` the card says `more: true` and `open` on the same sealed span id returns the rest within the caller's budget. What a `personal-use` card carries depends on the channel (B8, personal-use table): on MCP no L1 card is returned at all | 1 | ≤ 600 of text (initial value; the measured Russian span mean is about 180 and p95 about 610, so about 5% of spans are cut) plus a context prefix of about 50–100 (initial value) | Ingest | Base pack; prefix in an overlay |
| L2 Structure | Node of the source's own outline, with a summary | As in the source, capped at 4 levels | Leaf ≤ 100, chapter ≤ 300, root ≤ 500 | Ingest | Base pack; summaries in an overlay |
| L3 Concept | Canonical concept (`con:bip-32`) with ru/en aliases | Topic tree ≤ 3 levels; top level approved at G1 (ADR-0015) | ≤ 150, at most 10 related concepts shown | Registry | Registry; evidence per pack |
| L4 Finding | Atomic claim, ≤ 3 sentences, ≥ 1 citation (ADR-0015 kinds) | Flat, typed links | ≤ 120 | Pipeline, tutor, learner | Learner store |
| L5 Learner | State per concept: known, new, gap; recall state; calibration | Flat | Not returned as content; used to filter and boost | Sessions | Learner store |

Card budgets and depth caps are initial policy values (A5).

**Spans replace chunks.** Today SRV-SEARCH embeds fixed chunks of 1,200 characters with 200 overlap, and SRV-INGEST stores Docling spans separately. Under this ADR the L1 span is the unit of retrieval: the chunk table becomes the vector overlay over spans, keyed (base pack, span locator, embedding model, overlay version). Spans are not split at ingest; long spans are cut at read time (L1 row). The pilot has about 10.1 spans per page (9,463 / 935), against the current chunk count of 2,285; `retrieval-ablation` measures whether span-level retrieval keeps the bench recall of the chunk baseline (27 of 40 on questions-v2).

Re-embedding: 9,463 spans of 342 bytes at the mean are about 3.2 MB of text, against about 5.5 MB for 2,285 chunks of 1,200 Cyrillic characters at about 2 bytes each. By text volume the span overlay is of the same order as today's full-pilot embed job, measured live at 12.0 minutes (`docs/progress.yaml`, V.SRV-SEARCH-min; the spec's estimate was 22–36 minutes, SRV-SEARCH.yaml:44); per-item overhead for 4 times as many items may make it longer. This is an estimate; `retrieval-ablation` records the wall time.

**Navigation invariant, per channel**, tested as a graph check: on the tutor and pipeline channels, and on MCP for `shareable` material, every active span is reachable from the ids `brain_map` returns by following `open` children and `next` links. On MCP, `personal-use` material is reachable only down to L2 summaries and L4 findings: no `open` child and no `next` link leads into its L1 spans (B8, personal-use table). A walk test starts at `brain_map` with an MCP token and follows every `open` and `next` link; it must return no `personal-use` span card and no more than 300 characters of quoted source text per citation. It also calls `open` on every `spn:` id cited by an L3 or L4 card and on guessed locators of the same material, and expects a refusal for each. The same walk with a course member who has no own copy runs on the tutor channel (B8, cross-user test). The number of calls an agent actually needs is a measured metric, not an invariant (B9: median ≤ 3).

**Drafting a lesson.** The pipeline puts the whole source section in context instead of retrieving it when the section's measured token count fits a context budget in policy. This is already built (`pipeline/source.py`, 300,000 characters). Lesson L02's 25 pages are about 23,900 Russian tokens (`compile-cost`), well inside it. Retrieval serves cross-source checks, the fact-check route, the tutor and MCP.

### B2. Source packs, overlays and the manifest

| Term | Meaning |
| --- | --- |
| **Base pack** | What parsing one material produces: spans (L1) and outline nodes (L2) with their ADR-0008 locators. Id = sha256(material sha256, parse config); the parse config includes the converter version and profile (ADR-0021, ADR-0029) |
| **Overlay** | A versioned set of derived rows keyed by a base pack id. Kinds: profile (L0), tags, vectors per embedding model (ADR-0028 `embed` service), context prefixes per prompt version, L2 summaries, concept evidence, pair link runs. Overlay version = sha256(inputs, builder version, prompt and model versions) |
| **Manifest** | Append-only log of brain versions. One entry = the active base packs and, for each, the active overlay versions |

| # | Invariant |
| --- | --- |
| P1 | Base packs and overlays are immutable once sealed. A build writes only rows carrying its own id and never updates another pack's or overlay's rows. A build stays `building` until sealed; only sealed builds enter a manifest |
| P2 | **Span identity is the ADR-0008 locator**, defined over data the converter stores, not over the PDF text layer:<br>• **PDF material:** `spn:<material_sha256>:p<page>:<start>-<end>`. `start` and `end` are character offsets into the page's **canonical span text**: the page's spans in reading order, their stored normalised text (FR-INGEST-6, so decoded LaTeX for formulas, Markdown for tables, the caption for figures, the synthetic marker for list items) joined by one `\n`. Offsets count Unicode code points of that NFC text. Every span kind therefore has a range. The locator carries no pack id, so at most one base pack per material is active in a manifest (a manifest entry that would activate a second one is refused). The 6 pilot pages without a text layer yield no text spans while OCR is deferred, so they need no locator until it lands.<br>• **Empty text** (a figure without a caption, an undecoded formula): a zero-length range `<start>-<start>` plus `#<n>`, its ordinal among the empty spans at that offset.<br>• **Non-PDF material:** the ADR-0008 file and line-range form, `spn:<material_sha256>:<path>:L<a>-<b>`, with `:` and `%` in the path percent-encoded, so a reader that splits on the last `:` (FR-NOTEBOOK-2) still finds the range.<br>• Outline ids are (material sha256, outline path). The card budget is policy and never part of a span id.<br>• A tagger, model, prompt or vocabulary change creates a new overlay, never a new base pack, so citations survive it. A parse-config or converter upgrade creates a new base pack, and offsets may shift.<br>• **Quote hash and re-anchoring.** Each span and each citation stores `quote_sha256` = sha256 of the span's normalised text. The re-anchoring job maps an old locator to the new span with the same hash on the same page; among equal hashes on that page (repeated headings, empty text), the one with the same ordinal among equals in reading order; if the counts of equal-hash spans differ between the two packs, the citation becomes `orphaned` (P8) rather than guessed.<br>• The frozen-converter rule (`version_frozen`, FR-INGEST-11, -12, -13, INV-INGEST-2) is lifted: a re-ingest under a new converter is a new base pack.<br>• **Migration order (owner decision 20).** (1) A converter revision that emits each span's locator and quote hash (host-side conversion, ADR-0027, ADR-0029). (2) Re-conversion of the pilot (935 pages; 665 s measured on the host, `spikes/tutor-chunks`). (3) A seq-to-locator map for each material, accepted only if it is 1:1 and every pair has an unchanged `quote_sha256`; otherwise the migration stops and the task blocks. (4) Mutable rows (tutor citations) are rewritten through the map in one Alembic migration that refuses to finish if any id fails to map. No findings table exists yet (findings are lesson cells, FR-PIPELINE-7/8), so findings get locator ids when SRV-KB creates them. (5) Published lesson revisions are immutable (INV-LESSON-6) and are **not** rewritten: a resolver maps their `<sha>:<seq>` ids through the map at read time, and the next compile writes new revisions with locator ids. (6) Freshness rows are never updated after `done` (FR-FRESH-10) and are not rewritten either: the scan's input hash (FR-FRESH-3) changes with the ids, so each lesson gets one new scan, about one freshness check per lesson at its measured cost (SRV-FRESH). After the migration every pipeline step's `input_sha256` misses (FR-PIPELINE-3), so the next compile of a lesson pays in full |
| P3 | Changing a source's reliability or role is a new profile overlay version; nothing is re-indexed (F2) |
| P4 | `brain_version` = sha256 of the manifest entry: active base pack ids, their active overlay versions, and the registry view version.<br>• Adding, removing or rolling back a source, or switching an overlay, is one new manifest entry.<br>• Activating a pack, together with all link runs computed for it, is one entry, not one per run.<br>• Appends use compare-and-swap on the parent entry |
| P5 | Every read goes through views restricted to the active manifest.<br>• A link is visible only when both of its ends are active.<br>• The concept registry is read through the same restriction: concepts and aliases whose only evidence is in inactive packs, and that the owner has not approved, are invisible |
| P6 | **Garbage collection** is background work, never on a request path. It deletes sealed packs and overlays that are:<br>• absent from the last N manifest entries,<br>• older than an age floor,<br>• and not cited by a published lesson revision or a finding.<br>N and the floor are in config. The owner can purge a pack at once (ADR-0017 data ownership); citations then keep their locator and show the source as removed. This replaces the tombstone-only rule of FR-INGEST-32 for pack data; material rows and the owner's soft delete (FR-INGEST-30/31) stay as the user-facing remove and restore, and each of them writes one manifest entry. Partition per pack (`DETACH PARTITION CONCURRENTLY`) comes only if GC deletion is measured to hurt |
| P7 | **Derived data** (concept cards, cross-source summaries, gap reports, the "book turned around" view) is a cache keyed by (node, sorted contributing pack and overlay versions, builder version). It is never edited in place. A changed pack set misses the cache and rebuilds in the background; until then `find` serves the cards whose key is still fully active and falls back to lower layers |
| P8 | Findings and learner state are not in packs. When every span a finding cites is inactive or unmapped, the finding gets the derived state `orphaned` and enters the review queue; nothing is deleted. Published lesson revisions stay immutable; inactive citations are shown as such |
| P9 | Removal costs one manifest entry on the write path. Its read-side cost is cache misses (P7), which rebuild in the background. The course soft-delete cascade of FR-INGEST-31 stays as it is; it is a course operation, not a knowledge operation |
| P10 | Scope: one manifest per user (every table already carries `owner_id`; INV-SEARCH-1). A shared course gives its members role-filtered read access to the course owner's packs (ADR-0016), except that a `personal-use` pack is served to a member only as the MCP row of the B8 per-channel table, on every channel, unless the member has attached their own copy with the same material sha256 (ADR-0016; ADR-0008 Decision 4). Owner isolation (INV-SEARCH-1) stays in force unchanged until SRV-SHARE specifies course reads. A read that spans a member's own manifest and a course records both manifest versions |

**Links are computed per pair of base packs.**
- A link run for the unordered pair {A, B} is an overlay whose version hashes all of its inputs:
  - both base pack ids;
  - their profile, vector and concept-evidence overlay versions;
  - the version of the owner-approved concept set;
  - the link builder version.
  An owner approval or a profile change therefore yields a new run version, not a stale run.
- Concept identity across packs: identifier concepts (`con:bip-32`) are canonical. Term concepts proposed separately in two packs (for example a Russian and an English name) stay distinct until the owner approves one as an alias of the other in the review queue. That merge is a new approved-set version, and the link runs it affects are recomputed.
- Runs exist only for pairs that share at least one concept (pack-level blocking), so their number grows with overlapping pairs, not with n(n−1)/2.
- When a pack is activated, runs are computed for every active partner that shares a concept and has none.
- A run sees only P, Q and owner-approved concepts (B4), so its result depends neither on the order in which sources were added nor on any third pack.

Where a pair run is stored is decided by the spike `pack-links` (B10):

- **Variant A:** in an overlay of the later pack, one overlay per pack holding all its pair runs.
- **Variant B:** in one overlay per pair.

### B3. Faceted tags

| # | Rule |
| --- | --- |
| F1 | One facet, one principle of division (Ranganathan). The sector tree of ADR-0015 (`reader.yaml` `sectors`) becomes the `topic` facet; other dimensions are separate facets, combined at query time. Sectors stay a tree, so the SectorMap of ADR-0013 and export by sector path in ADR-0017 are unchanged |
| F2 | A tag lives at the layer where it is true and is inherited downward by a join at query time: license, role, reliability and `as_of` on the source profile; form on the span; topic on the concept |
| F3 | Only stable context enters the embedded text: source, chapter and section titles in the context prefix. Volatile tags stay in columns, because prefixing metadata forces re-embedding the whole index on every metadata change |
| F4 | Vocabularies are closed and declared as data (a facets file with a JSON Schema). Only `topic` grows, from findings (ADR-0015). Free-form tags are not accepted; synonyms become concept aliases |
| F5 | Every tag records `by: rule | model | owner`, a confidence and the tagger version. Rule tags (identifier regexes, Docling element types, outline position) come first at 0 model calls; model tags are proposals. A vocabulary change builds a new tag overlay |
| F6 | A facet is enabled only with a named consumer: a filter, a boost, a gap check or a view. A facet unused by any query path for one stage is disabled |

Facets enabled from the start, each with its consumer:

```yaml
facets:
  role:        { level: source, values: [map, normative, time_axis, verification, reference_impl] }  # route filters
  reliability: { level: source, values: [A, B, C, D, E, F] }   # Admiralty source reliability; fact-check threshold
  credibility: { level: finding, values: [1, 2, 3, 4, 5, 6] }  # Admiralty information credibility of a finding or a link; fact-check threshold
  as_of:       { level: source, type: date }                    # current-state route, supersedes rule
  license:     { level: source, values: [personal-use, shareable] }  # the existing ADR-0008/0011 vocabulary; MCP, trace and copyright invariants
  form:        { level: span,   values: [prose, code, formula, table, figure, test_vector], by: rule }  # exact route, card shape
  topic:       { level: concept, tree: sectors, multi: true, grows: true }  # brain_map, coverage objective; chains are branches
```

`form` is derived by rule from the existing span `kind` (text, heading, list_item, formula, code, table, figure; SRV-INGEST.yaml:1173); `test_vector` is a rule over code spans. `license` and the language already live in `reader.yaml` `materials[]` and in uploads; they move into the profile overlay.

Chains are branches of `topic` (for example `bitcoin/keys`, `ethereum/transactions`), within its depth cap of 3 levels. Exact questions that name a chain filter on that branch.

Proposed facet, enabled only when its consumer lands: `knowledge_type` (fact, mechanism, procedure), for recall cards versus explain cells, if the owner adopts the explain-back proposal.

### B4. Linking a pair of packs

Runs for each new pair {P, Q} of active base packs that share a concept (B2). Each step is cheaper than the next and limits its work. It needs two packs, so it is built with the second source (stage 7); the code path and its property test exist from the start.

| # | Step | Method | Cost |
| --- | --- | --- | --- |
| 1 | Mentions | Identifier regexes (BIP-, EIP-, ERC-, RFC, FIPS) and the aliases of concepts evidenced in P or Q, or approved by the owner, give evidence (concept, span). No other pack's aliases are read | 0 model calls |
| 2 | Candidate concepts | Defined terms, glossary entries and repeated unmatched terms become concepts proposed with their pack's evidence | 0 calls; a model only labels |
| 3 | Blocking | Pairs are formed only between spans of P and Q that share a concept | Sum over concepts of n_P(c) · n_Q(c), not \|P\| · \|Q\| |
| 4 | Similarity | Within a block, keep a pair if it is among the top m candidates of either of its two spans, with cosine ≥ τ. The union makes the result symmetric | Vectors already exist |
| 5 | Judgement | The link-type judge labels a pair `agrees`, `contradicts`, `refines` or `supersedes`. `supersedes` is proposed only for a later `as_of` and `role: normative`. Outputs are recorded and keyed by (pair input hash, judge@version), so a rebuild replays them | 1 call per candidate, within a per-run USD cap (B7) |
| 6 | Write | Links with provenance (`rule` or `model`, confidence, builder version) into the pair's link run | — |

m, τ and the confidences (1.0 for an identifier, 0.9 for an alias) are initial policy values.

Order of magnitude, on an assumed example: two sources of 2,000 spans give 4 million pairs; 300 shared concepts with 7 mentions per source give 300 × 49 ≈ 15,000 pairs, about 270 times fewer. The first real pair replaces these numbers with measured ones.

### B5. Greedy algorithms, bounded scans

Every selection over the graph or the embeddings is a greedy algorithm with a bounded amount of work. Where the problem is an optimisation, the spec states the approximation guarantee and the conditions under which it holds. Enumerating subsets or combinations, ILP solvers and global recomputation are not used. Bounded linear scans are allowed: an exact distance scan over a filtered candidate set, or a pass over at most 50 candidates.

| Problem | Exact version | Greedy used | Guarantee |
| --- | --- | --- | --- |
| Pick k cards that cover the query's terms and facets | Max coverage, NP-hard | Largest marginal gain first, with lazy evaluation | ≥ 1 − 1/e ≈ 0.632 of optimal for a monotone submodular objective (Nemhauser, Wolsey and Fisher, 1978) |
| Pick cards under a token budget | Budgeted max coverage, NP-hard | Best of cost-benefit greedy and the best single card | ≥ (1 − 1/e) / 2 ≈ 0.316 (Khuller, Moss and Naor, 1999). Refined analyses: 0.405 (Tang et al.) and about 0.429 (2026 preprint) |
| Pick sources that cover a task's terms (atlas gaps, purchase proposals) | Weighted set cover, NP-hard | Lowest cost per newly covered term first | H(n) ≤ 1 + ln n; Feige (1998) showed ln n is the approximation threshold |
| Merge `same_point` duplicates into one card | Correlation clustering, NP-hard. Graph: complete ±, with + for pairs whose `same_point` link is above τ and − otherwise | Pivot: pick a random unassigned card, attach its + neighbours, repeat. The random order is seeded by the request hash, so a response stays reproducible | 3 in expectation over seeds (Ailon, Charikar and Newman, 2005) |
| Cross-source candidate pairs | Quadratic in spans | Blocking by concept plus symmetric top-m (B4) | No quality bound; work bounded |
| Graph expansion at query time | Path search can explode | At most 1 hop, top-m edges by weight | Work bounded |
| Hierarchy of a source | Clustering | The source's own outline; no global community detection | Deterministic |

**Objective of the card-selection rows.** It is weighted coverage of the query's terms and facets, which is monotone submodular. With one source the terms are identifiers and full-text terms from L1 and L2; concepts (L3) join when the registry lands. Source priorities enter only as non-negative element weights. Superseded cards and license limits enter only as filters applied before selection. Either way the bounds keep holding. Lexicographic re-ranking after selection is not used. On the judged path of B6, the judge's drops are a third filter between two greedy selections. Each selection keeps its bound over its own input set, but that path has no end-to-end bound against the full candidate pool; the gate and degradation paths keep the bound over their candidates. The judge's order sets only the order in which the selected cards are returned.

This replaces the plain top-k by dense order of FR-SEARCH-7 step 4.

### B6. Retrieval cascade for `find`

A route = f(channel, question type). Routes, budgets and thresholds are declared as data and versioned in `policy_version` (B7a).

No step before the judge makes an LLM call (step 2 embeds the query through the local `embed` service, ADR-0028). The judge runs last and reads only the cards already selected (owner decision 18).

| # | Step | Budget |
| --- | --- | --- |
| 1 | Route by rules: channel (MCP, tutor, pipeline) and question type (identifiers found, wording) | 0 model calls |
| 2 | Candidates: manifest, role and license filters first, then dense vectors and, where the route enables it, full-text rank (`tsvector`) merged by reciprocal rank fusion | ≤ 50 candidates. Exact distance on the filtered set while the vector step alone has p95 ≤ 150 ms (initial value). Measured today: the exact scan alone takes 6.6 ms at 2,285 chunks; 173 ms is the end-to-end search p95 and is not the quantity the threshold applies to. Hybrid fusion measured worse than dense alone (24 vs 27 of 40, SRV-SEARCH.yaml:286-289), so it stays off per route until `retrieval-ablation` shows a significant gain on spans |
| 3 | Merge duplicates by `same_point` (B5 pivot) | One card, several citations |
| 4 | Gate: if the scores separate clearly (margin rule in policy), go to step 7 | 0 model calls |
| 5 | Otherwise expand 1 hop over `next` and concept links, keep the top 50 of the expanded set by score, and select from them under 1.5 × the response budget (B5 budgeted greedy). A card reached only by expansion takes its parent's score times a decay in policy | 0 model calls. The cap of 50, the factor 1.5 and the decay are initial policy values |
| 6 | One `judge.find` call on the step-5 selection only: relevance order, sufficiency, and which cards to drop. It runs as a job on the `worker-judge` lane of B7b, within a timeout in policy | Exactly 0 or 1 judge call per `find`. Judge input ≤ min(1.5 × the calling channel's response budget, the judge-input cap in policy; 3,000 tokens initial); when the step-5 selection is larger, only its top slice by score, up to that cap, is judged, and the rest passes to step 7 unjudged. The cap is absolute so a channel with a large budget (the tutor's starts near 11,000 tokens) never sends the judge more than the latency budget was set for |
| 7 | Select under the response budget (B5 budgeted greedy): from the cards the judge kept when step 6 ran, otherwise from the merged candidates (step 3, or the capped expanded set of step 5). Cards are returned in the judge's order when it ran, otherwise in score order | Within the response budget (B8) |

**`judged`.** Cards carry `judged: true` only when step 6 ran and returned in time. When the gate skipped the judge or step 6 degraded, they carry `judged: false`, and the trace records which of the two happened.

**Degradation.** Step 6 is skipped on judge timeout or error, on an exhausted budget, when the judge lane is saturated, or for a caller without a provider. Step 7 then selects from the capped expanded set of step 5. A `judge.find` job has max attempts 1 and is keyed by its request id, never by the input hash, so a timed-out or dead job never blocks a later identical `find` (FR-EVENTS-11 keys `job_keys` by (kind, key)). A job whose deadline has passed before it starts makes no model call, so a burst never spends the caller's credit on answers nobody waits for. The same holds when `find` is called from a worker job (the tutor, the pipeline's fact-check route): the calling job enqueues the `judge.find` job and waits for it the same way the api does. Because the judge has a timeout and the degraded path needs no model, `find` p95 < 2.5 s holds **if** the steps other than the judge stay within about 0.3 s. Only today's search is measured (173 ms p95); expansion, the two selections and the queue hop are not, so `judge-gate` measures them and the judged-path completion rate.

**Why the judge comes last.** The order is owner decision 18; this paragraph states what it costs and saves at the measured card sizes. An L1 card of the Russian pilot is about 230–280 tokens at the mean (span text about 180, Context numbers, plus a prefix of 50–100), so a 2,000-token response holds about 7–9 cards. A judge that read the top 20 candidates would read about 4,600–5,600 tokens, 55–65% of them on cards never returned. Judging the 1.5× selection reads at most 3,000 tokens, every card of which the response can contain, and the margin of 0.5 lets the judge drop cards without leaving the response short. The saving is close to the first draft's 60–85%, which assumed 200–500-token spans; either way the order rests on decision 18 and on the judge's input being bounded by the budget rather than by a candidate count. The price is stated in B5: the judged path has no end-to-end bound against the full candidate pool. `judge-gate` compares this order with the earlier one (B10).

**Latency budget.** Measured Haiku through the Agent SDK: CLI ready 0.89–1.20 s, result 1.82–2.10 s (`spikes/agent-sdk-container`, n = 3 fresh-container runs of a short prompt, not a 3,000-token judge input, a queue hop or the proxy). With the end-to-end search at 173 ms p95, a judge result of about 2.1 s leaves about 0.2 s of the 2.5 s target. The CLI start (0.89–1.20 s) is about half of that result time, and the result time already includes a fresh CLI spawn: `t0` is set after the interpreter starts and the SDK is imported, so the measured 1.82–2.10 s covers spawn, start and the call (container creation is outside it and does not happen on the lane). The judge lane therefore spawns one CLI per call, exactly as the worker does today, and keeps no warm pool (B7b). The timeout is 2.2 s (initial value, on that weak basis); `judge-gate` sets it.

Initial route table (question type × channel modifiers):

| Question type | Layers | Filter |
| --- | --- | --- |
| Exact ("hardened index offset in BIP-32") | L3 → L1 | `role: normative`, identifier from step 1 |
| Thematic | L2 → L3 | None on form |
| Current state | L1, L4 | Newer `as_of`, `role: time_axis`, not superseded |
| Fact-check (ADR-0009 stage 4) | L1 only | Span required; reliability above a threshold in policy |

| Channel | Modifier |
| --- | --- |
| MCP | Caller's course roles; copyright limit on every card (B8); L4 and L1 preferred for `shareable` material; for `personal-use` material only L2 and L4 (B8, personal-use table) |
| Tutor | L5 boost: the learner's gaps first |
| Pipeline | No L5; reliability threshold from policy |

Layers L3–L5 come online with their specs. Until then the routes run on L1 and L2, and the evaluation reports which layers were available.

### B7. Judges, credentials, cost and labels

Judges are **slots**: model task classes in `reader.yaml` `tasks` (ADR-0005; ADR-0018 "model per task as data"; FR-AGENT-6, -17 already declare tasks with model aliases). Our own code calls each judge through the `Provider` port (FR-AGENT-6) and records the result.

| Slot | Decides | Runs | Paid by | v1 binding |
| --- | --- | --- | --- | --- |
| `judge.link_type` | Link type between two spans (B4 step 5) | Offline, per pair run, worker | Pack owner; `index` line of the ledger | Claude Haiku |
| `ingest.context_prefix` | The context prefix of a span (B1) | Offline, per pack, on the `worker` in batched jobs of 25 spans (initial value) that re-enqueue themselves, so a tutor job waits at most one batch: about 45–55 s at the measured 1.82–2.10 s per call. The whole pilot is about 9,463 × 1.82–2.10 s ≈ 4.8–5.5 hours of worker time; `retrieval-ablation` records the real wall time. Queue order is an SRV-EVENTS property (FR-EVENTS-9); its spec checks that the worker serves jobs in enqueue order. While a pack is prefixed, a tutor answer can wait up to one batch, 45–55 s, against NFR-TUTOR-1's 30 s (measured on an idle worker); this is no worse than today's 12-minute embed job, and it is stated to the owner | Pack owner; `index` line of the ledger | Claude Haiku |
| `judge.find` | Relevance order, sufficiency and cards to drop, over the top slice of the cards already selected (≤ min(1.5 × the channel's response budget, 3,000 tokens initial), B6 step 6), in one call | Online, gated (B6 steps 4 and 6), `worker-judge` lane (B7b) | The caller, on the caller's own provider (ADR-0005 invariant). Ledger line by channel: the caller's tutor use on the tutor channel; the lesson's compile run on the pipeline's fact-check route, inside `budget.compile_cap_usd` and counted in the loop rule's reserve (ADR-0005 row); on MCP (stage 7) the user's monthly budget plus a per-token daily judge cap in SRV-MCP (initial value), so a `kb:read` token cannot spend the month on gated `find` calls | Claude Haiku |
| `judge.eval.*` | Retrieval relevance, sufficiency and groundedness, plus custom judges (card is self-contained; citation supports claim) | Offline, on the evaluation set and sampled traces, in the `obs.eval` worker job | Owner; `eval` line | A strong Claude model |
| `judge.align` | A proposed rewrite of one judge's instructions from the owner's labels (labels item 5) | Offline, in a worker job, at most once per 50 new labels per judge | Owner; `eval` line, within the `eval` cap | A strong Claude model |

Rules:

- **Replacing Claude in a slot** with a local model is a config change. It is allowed only when the candidate matches or beats the current binding on the evaluation set (ADR-0018 tuning rule).
- **Caps.** Offline jobs have a USD cap per pack (prefixes) and per pair run (link judging), next to the existing per-run, per-task, daily and monthly caps (FR-AGENT-9, -12). The initial values are the costs measured in B10 plus 20% (an initial value). Reaching 100% stops the job with an event (ADR-0005); the agent then blocks the task with a top-up ask to the owner (owner decision 15). Unjudged candidates stay unlinked, unprefixed spans keep their titles-only context, and the pack stays usable. The evaluation cap is set from the measured cost of one evaluation run (`judge-gate`). The new caps are file settings; whether they join the UI allow-list of ADR-0036 is the owner's choice, and if they do, `policy_version` hashes the effective values.
- **Judge outputs** are append-only rows keyed by (input hash, judge@version), the same pattern as the pipeline's `input_sha256` reuse (FR-PIPELINE-3/5). A new judge version sits next to the old one, so the two can be compared on the same inputs. Replays read the recorded outputs. Outputs of the offline slots (`judge.link_type`, `ingest.context_prefix`) belong to their pack or pair overlay and follow its garbage collection. Outputs of `judge.find` span a query and several packs, so they are not overlays: they are kept for 30 days (initial value, the trace retention of ADR-0032 Decision 6) and deleted at once with a purge of any pack whose span they cite.
- **Prompt injection.** Judge prompts treat card and source text as data. Instructions found in material or findings are never followed. Unreviewed findings (`proposed`, including those written by external agents with `kb:write`) are excluded from `find`, from judge input and from `next`. Judge calls run with an empty declared tool set (ADR-0025 rule 2, tools by allow-list).
- **Ranking priors.** A card's prior weight in ranking comes only from the owner's labels and the evaluation set, never from live judge scores. Otherwise the judge would reinforce its own choices.
- **Tuning data.** Claude outputs never become tuning data (ADR-0018). Judges may use Claude for evaluation, which ADR-0018 allows.

Labels, the owner's time used as efficiently as possible:

1. A binary label per trace and judge, with an optional one-line reason. Postgres `owner_labels` stays the record (FR-OBS-24), extended from `lesson | answer` to (trace, judge slot). The worker mirrors only the pass or fail to MLflow as a HUMAN assessment named `owner.<slot>_ok`, with the slot's dots replaced by underscores (`owner.judge_find_ok`, `owner.judge_eval_sufficiency_ok`), next to `owner.lesson_ok` and `owner.answer_ok` (ADR-0038). The reason text stays in Postgres and is never mirrored.
2. **What a label needs.** Traces are text-free, so labelling a `judge.find` call reads a `judge_inputs` row: the query text, the card ids and the judge's output, keyed by the judge input hash. The `judge.find` job copies these from its `judge_requests` row (B7b, transport) before that row is deleted, and only under the consent rule below. The table is owner-only, lives in Postgres, is kept 30 days (initial value) and is deleted with a purge (judge outputs rule above). Another user's query is stored there only if that user has the ADR-0018 evaluation-use consent flag on; otherwise the row holds ids and hashes only and the call is not offered for labelling. The labelling screen is part of the owner area (WEB-MANAGE).
3. Sampling order: traces where two judges disagree; then judge scores within ±0.1 of the gate threshold (initial value); then the owner's thumbs-down in tutor sessions. Stratified across routes and packs.
4. Batches of 25 traces, about 8–12.5 minutes at 20–30 s each (initial values). The first target is 50–100 labels per judge.
5. Owner labels are owner data and may later feed local-model tuning (ADR-0018 excludes only Claude outputs).
6. **Alignment** runs once a judge has 50 labels, and again after each further 50 (initial values), in the `judge.align` slot. It rewrites judge instructions and trains no model weights (owner decision 14). It runs as a worker job of our own code, not through `mlflow.genai` (ADR-0038 Decision 1 keeps the MLflow client package out of the worker); the optimiser (SIMBA- or GEPA-style instruction search) is chosen in the SRV-OBS spec. **Hand-off:** the worker has a read-only root and never writes the repository, so the job writes a `judge_prompt_proposals` row (slot, base prompt sha, proposed text, label ids, scores). The owner runs an export, `reader obs proposals` (OPS-CLI; the agent has no owner session and never runs it), which writes the guarded proposal text to a file outside the git work tree, as `reader backup` requires of its output. The agent then reads that file, treats the proposal text as untrusted data derived from queries and material (instructions in it are never followed), and either commits it as a new prompt file through the normal review and the no-regression rule of B7a, or discards it. **Invariant:** aligned instructions contain no material, question or answer text, and the proposal is instructions only, with no demonstrations or examples appended. CI cannot read the book, so the check is a runtime guard: the `judge.align` job refuses to write a proposal that shares an 8-word sequence (initial value) with a query, card or answer of the labels it used, or with the material's text, or that contains any whole labelled query or answer as a normalised substring at any length (most queries are shorter than 8 words); the export runs the same guard again before it writes. A unit test pins the guard on synthetic text.

### B7a. Prompts and policy versions

- **Source of truth.** Every prompt the system sends (judges, context prefix, link type, card text) is a template file in the repository, as the pipeline and tutor prompts already are (`pipeline/prompts/`, `tutor/prompts/`). It is reviewed like code, and its file hash is the provenance.
- **MLflow registry.** Each file version is registered by the `obs.prompts` job (FR-OBS-15, ADR-0035), by sha, with the release tag; code loads prompts from the repository, never from MLflow, so search works with MLflow stopped (INV-OBS-3). New judge prompts join the declared prompt list (VEC-OBS-4).
- **No-regression rule.** A prompt change merges only after the agent has run the evaluation job against the current version, with live model calls inside the evaluation cap, outside CI. The run must show no regression (B10 statistics), and the Haiku targets must hold. The result is recorded as an MLflow evaluation Run (ADR-0038) and in the PR description. Reverting the commit is the rollback.
- **`policy_version`** = sha256 of the declared data only: the route table, thresholds, k and the RRF constant, the facets file, slot-to-model bindings, embedding model ids and prompt file hashes. It extends the existing `version_key` (FR-OBS-21), which covers prompts, stage models and pipeline config. The code version is a separate trace attribute.
- **Rules that need no model** (routes, facet filters, identifier regexes, budgets) stay data files: 0 calls, deterministic.

### B7b. The judge lane (amends ADR-0025)

ADR-0025 puts only the `worker` on the `agent-proxy` network, and the worker consumes every queue at concurrency 1 (FR-EVENTS-9), where one full-pilot embed job held the slot for 12.0 minutes live (V.SRV-SEARCH-min) and pack prefixing will hold it in 45–55 s batches for hours (B7). An online judge cannot wait behind that.

| Option | Gives | Costs |
| --- | --- | --- |
| **A. A judge lane: a second worker service, `worker-judge`, from the same image, consuming only the `judge.find` queue at concurrency 4 (initial value), one CLI spawned per call, as the worker does today** | ADR-0025's rule "only worker containers reach `agent-proxy`" holds; the api never holds a provider handle; tools empty; the lane cannot be blocked by compile or embed jobs | One more service with its own memory limit: at most four CLI processes at once, no pool. OPS-DEPLOY sets an initial limit of four times the per-call CLI footprint plus the worker's own base (initial value; no per-call CLI footprint has been measured in this repository, so the base number is unmeasured), and `judge-gate`, which runs on the deployed lane, measures the peak; a paired OPS-DEPLOY change then sets the measured limit. The waiter (the api, or a worker job that calls `find`) waits for the job result by `LISTEN/NOTIFY` with the B6 timeout |
| B. Put the api on the `agent-proxy` network for the `judge.find` slot only | No queue hop | The internet-facing service gains a route to the credential proxy and per-run handles; this is the exposure ADR-0025 exists to remove |
| C. Raise the one worker's concurrency | No new service | Compile stages would run in parallel against the live-runs budget, which SRV-EVENTS keeps serial on purpose; the judge still queues behind long jobs |

**Decision:** option A. Amendments to ADR-0025:
- **Rule 1 (caller control):** `worker-judge` joins `worker` on the `agent-proxy` network; it presents the caller's per-run handle (ADR-0005, ADR-0023).
- **Rule 3 (process controls) and rule 4 (Compose services):** `worker-judge` is a static Compose service with the worker's hardening (non-root, read-only root, tmpfs home, cap-drop ALL, no-new-privileges), the DB DSN and no other secret.
- **Rule 5 (routes):** `worker-judge` reaches `agent-proxy`, Postgres and Rotel only; no route to `kernel-*`, `mcp` or the web-fetch proxy.
- **Rule 2 (tools):** it accepts only the `judge.find` task, whose declared tool set is empty.
- **`verified_by`:** every test that names the worker (token absent from env, `/proc/*/environ`, `/run/secrets`, `~/.claude`, `docker inspect`; a request without a valid handle gets no credential) runs against `worker-judge` too (OPS-SANDBOX, SRV-AGENT).

**One call, one process, one token.** Each `judge.find` job runs today's FR-AGENT-3/-6 path unchanged: reserve on the ledger (FR-AGENT-9), issue a run token bound to the caller, the model and the output cap, spawn the CLI with that token and a fresh HOME, make exactly one call, revoke the token in `finally`, discard the process. There is no pre-spawned or warm process, so no token exists without a reservation, and no conversation is reused: no earlier query or card text reaches the next call, and replay by (input hash, judge@version) holds. The measured 1.82–2.10 s already includes this spawn (B6, latency budget).

**Token-table invariant.** The lane holds at most 4 live run tokens (its concurrency) and never causes the eviction of a `worker` token. `agent-proxy` keeps at most 32 tokens and revokes the oldest beyond that (FR-AGENT-3, `agent/proxy.py`); the `worker` holds at most 1 at concurrency 1, so the two lanes together hold at most 5 of 32. The bound must survive crashes: revoke errors are swallowed today (`agent/provider.py`), a SIGKILL or an OOM skips `finally`, and `agent/proxy.py` never purges expired entries, so a lane-side counter alone would leak. Therefore `agent-proxy` itself enforces a quota per client service (4 for `worker-judge`, initial value) and refuses a fifth issue rather than evicting; it purges expired entries; and `worker-judge` revokes every token it still holds when it starts. SRV-AGENT tests the bound: the proxy refuses a fifth lane token; with the lane saturated the worker's token is never evicted; and after the lane is killed mid-call and restarted, the lane's tokens are revoked and the count is back to 0.

**Transport of query and card text.** Job arguments are stored in `job_keys.args` (FR-EVENTS-11), which has no retention, so they never carry text. The waiter writes a `judge_requests` row (request id, caller, input hash, deadline, query text, card ids and card text as the judge reads them) and enqueues a `judge.find` job whose args are only the request id and the input hash; the job is keyed by the request id. The job reads the row, makes the call, writes the output row keyed by (input hash, judge@version), copies the query and card ids to `judge_inputs` only under the consent rule (B7 labels item 2), and deletes the `judge_requests` row at its end; a row whose deadline has passed is deleted by the job without a call, and a background collector deletes any row older than its deadline plus one minute (initial value). **Invariant**, with a canary test in the style of FR-EVENTS-10: no query or card text appears in any `judge.find` job's args or in `job_keys`. The waiter waits at most the B6 timeout. A job that finishes later still writes its output row keyed by (input hash, judge@version), so a later identical request and every replay can use it; only the waiting response ignores it, and its cost is recorded. `job_keys` rows of the judge kinds are pruned by the same collector once their job is finished and older than one day (initial value), since a request-id key is never reused (FR-EVENTS-11 gains a retention rule for these kinds). Concurrency and the timeout are policy values set by `judge-gate`.

### B8. Agent contract `brain/v1`

The interface every agent sees. Tools and models behind it change freely (A5).

| Tool | Scope | Returns |
| --- | --- | --- |
| `brain_map()` (also a resource) | `kb:read` | Top of the brain within 1,500 tokens (initial value): sectors with counts, sources with roles, `brain_version`, how to search. Meant to be read once per agent session |
| `find(query, filters?, k?, budget?)` | `kb:read` | Cards chosen by B6 within `budget` tokens (default and maximum in policy; initial maximum 2,000; a larger `budget` is clamped to the maximum) |
| `open(id, depth?, budget?)` | `kb:read` | Any typed id: `src:`, `sec:`, `con:`, `fnd:`, `spn:`, `les:` (lesson outline, replacing `get_lesson_outline`); `depth ≤ 4` (initial value), same budget rule as `find`. A `spn:` id must name a sealed span of an active pack; an arbitrary character range is refused |
| `propose(...)` | `kb:write` | A proposed finding in the owner's review queue (unchanged from ADR-0015) |

Invariants of every `brain/v1` tool:

1. Results are filtered by the caller's course roles (ADR-0015, ADR-0016).
2. For `license: personal-use`, what a card carries is set per channel and per caller by the table below. Text leaves its owner on two paths: MCP, and sharing (a course member's tutor or pipeline job sends what it reads to the member's own provider). Both get the restricted MCP row. On MCP no L1 span card of such material is returned and no `open` child or `next` link leads to one; L2 summaries and L4 findings come back with quotes of at most 300 characters per citation, as ADR-0015 ("findings and citations, never source text beyond a short quote") and ADR-0008 Decision 4 already say, and with no continuation. Because an MCP caller can never reach a span, walking `next` or repeating `open` cannot reassemble the book; the per-citation limit is the only text bound needed there, and no separate per-response cap is set. The current search snippet already keeps the per-citation limit (FR-SEARCH-7).
3. Every response respects its token budget, counted with the characters-per-token estimator SRV-AGENT already uses; the SRV-SEARCH spec names it.
4. Unreviewed findings are never returned.

**What a `personal-use` card carries, per channel.** The pilot book is entirely `personal-use`, so this table decides what every channel and both stage-V spikes see. "Entitled caller" means the pack's owner, or a user who has attached their own copy of the same material sha256 (ADR-0016). Every other caller, including a course member reading the owner's packs, gets the MCP row on every channel.

| Channel | Who reads it | L1 span cards | L2 summaries, L4 findings | Text bound |
| --- | --- | --- | --- | --- |
| MCP (`brain/v1` to external agents), and any channel for a caller who is not entitled | Any agent with `kb:read` and course roles; a course member without their own copy | None; no `open` child or `next` link into L1 | Yes, quotes ≤ 300 characters per citation, no continuation | 300 characters per citation (ADR-0008 Decision 4, ADR-0015) |
| Tutor | The entitled learner's own tutor job (worker) | Full span text, cut at the card budget | Yes | The tutor's source budget, `prompt_bytes_max` minus the question and cell (21,000 bytes today, SRV-TUTOR `bounds`, FR-TUTOR-5), which bounds grounding (NFR-TUTOR-3) |
| Pipeline (fact-check route, drafting) | Compile jobs (worker) of the entitled owner | Full span text | Yes | The stage's own context budget (`SOURCE_MAX_CHARS`, FR-PIPELINE-2) |
| Judge input (`judge.find`, `judge.eval.*`) | Judge calls on the `worker-judge` lane or the worker, for an entitled caller; for any other caller the judge reads only the MCP row | Full span text of the cards being judged | Yes | min(1.5 × the response budget of the calling channel, the judge-input cap; 3,000 tokens initial) (B6 step 6); never stored outside `judge_requests` and the consent-gated `judge_inputs` (B7b) |

A cross-user test pins this: a second user (a course member without their own copy) calling `find` and `open` on every channel against the owner's `personal-use` pack receives no L1 card, no span text beyond a 300-character quote, and no judge call that read L1 text. Until SRV-SHARE exists, owner isolation (INV-SEARCH-1) already makes the owner the only caller who can read a pack.

Internal channels never put this text into traces, MLflow or the repository (INV-OBS-1/2). The response token budget is per channel in policy: the MCP maximum starts at 2,000 tokens (owner decision 17); the tutor's starts at its current source budget, so moving the tutor onto `find` does not halve its grounding without a measurement.

One card shape for every layer:

```yaml
id: con:bip-32
layer: L3
title: BIP-32 hierarchical deterministic keys
text: "..."                  # self-contained; carries its context
cite: [{ spn: "<material-sha256>:p<page>:<start>-<end>", quote_sha256: "..." }]
source: { id: src:bip-0032, role: normative, as_of: 2013-02-11, reliability: A }   # for L3: its best normative source
next: [con:bip-39, con:hardened-derivation, "sec:<material-sha256>:ch03/wallets"]
judged: true
```

- `next` makes the brain hypertext: a weak model follows links instead of planning queries.
- **Reproducibility.** Scope: layers L1–L3 on the MCP and pipeline channels. There, the same `brain_version`, `policy_version`, caller roles and request give the same response on the judge-free path (as INV-SEARCH-4 already promises for today's search), and also with a judge when the recorded judge output for that input is replayed. Findings, learner state and label priors change outside the manifest, so tutor responses and L4 results are traced but not promised to replay. Responses may be cached only under a key that includes the caller's identity, roles and manifest version.
- **Versioning.** Adding an optional field is allowed within `brain/v1`. Removing or renaming a field or a tool is `brain/v2`, served next to v1 until traces show no v1 caller.

### B9. Tracing and evaluation in MLflow

MLflow is the trace and evaluation backend, deployed under ADR-0032 (MLflow 3.18 on SQLite, OTLP through Rotel, owner-only dashboard). The request path never depends on it (INV-OBS-3).

**Traces stay text-free for all material.** ADR-0034 (opt-in trace content) was rejected, and INV-OBS-1/2 keep every span free of material, question and answer text. `RETRIEVER` spans therefore carry `doc_uri` = `reader://<id>` and a content hash per card, never `page_content`, whatever the card's `license`.

Span tree of one `find` call:

- `TOOL` root with the caller (agent and model), `brain_version`, `policy_version`, the code version and the tool.
- `ROUTER` (custom span type) with the route and filters. A filter taken from the question (an identifier on the exact route) is recorded as a concept id or a hash, never as the extracted string.
- `RETRIEVER` per layer used, with `doc_uri` and hashes only.
- The selection under 1.5 × the budget, then the wait for `judge.find` (with `judged` and, when false, the reason), then the final selection with the token count. The `judge.find` job itself is its own trace, linked to the span that enqueued it (ADR-0032 Decision 3).

All new attributes join the export allow-list (VEC-OBS-1). Pack builds and link runs are traced too: counts per B4 step (spans, mentions, pairs after blocking, judge calls, cost), next to the existing `ingest.chunk` and `embed.batch` spans.

Evaluation:

- **Data set.** The owner-accepted bench sets stay: `search-bench-v1` (26 records) and `tutor-bench-v2` (questions-v2 with v03 replaced), registered as MLflow datasets (FR-OBS-22, ADR-0038). They are extended, not replaced, to 30 questions per question type (exact, thematic, fact-check; 90 in all) with expected facts written in the owner's or agent's own words, ≤ 300 characters each, never copied source text. The owner reviews a sample of 30. The extended set is a new dataset version. **What reaches MLflow:** the question text, as the current datasets already carry it (ADR-0038), plus ids and gold pages. The expected facts stay in the repository fixture only and are never sent to MLflow. Every expected fact passes the same verbatim check as the alignment invariant (no 8-word sequence shared with the book, initial value) before the set is frozen.
- **Judges that read text run locally.** `judge.eval.*` runs in a worker job (`obs.eval`, a new job kind in ADR-0038's catalogue) that reads card text from our store in memory; only scores reach MLflow, as code-written assessments with `source` LLM_JUDGE. This amends FR-OBS-23's "no model call, no judge", as ADR-0038's `revisit_when` foresees ("a bench needs a model call to score").
- **Matrix:** caller model (Claude Haiku, a strong Claude model) × `brain_version` × `policy_version`.
- **Channel.** The stage-V evaluation and both stage-V spikes run on an internal owner channel: the owner is the entitled caller of the pilot pack, so `find` and `open` see L1 spans as on the tutor channel. On real MCP (stage 7) the pilot, being entirely `personal-use`, returns no L1 cards, so the exact and fact-check routes answer Bashir questions only from L2 summaries and L4 findings; the stage-7 MCP evaluation reports those questions separately and is expected to score lower on them, which is the price of ADR-0008 Decision 4, not a regression. The openly licensed sources of stage 7 carry L1 on MCP.
- **Metrics:** sufficiency, calls per answer, tokens per response and per answer (counting `brain_map` once per session), p95 latency, judged-path completion rate.
- **Schedule.** LLM judges run on the data set on a schedule and on sampled traces (by trace id; the job reads the text from our store, not from the trace). Live requests record only deterministic metrics: calls, tokens, empty results, latency.
- **Pack value.** The evaluation runs before and after a pack is activated, and the paired difference is the pack's measured contribution. A pack that loses significantly (B10 statistics) is deactivated with one manifest entry.

Targets (owner, 2026-11-25), each reported as a point estimate with its 95% interval:

| Target | Measured as |
| --- | --- |
| Haiku ≥ 90% of the strong model's sufficiency | Point estimate |
| Median ≤ 3 calls per answer | Median over the data set |
| ≤ 2,000 tokens per answer | `find` and `open` responses of one answer, summed; `brain_map` counted once per session (owner decision 17) |
| `find` p95 < 2.5 s | p95 over the data set, judged path included |

### B10. Spikes and their decision rules

**Statistics for every rule.** Paired comparison on the same questions, with an exact McNemar test on the discordant questions. A difference is stated in whole questions per question type (30 questions per type). "Significant" means the exact McNemar test gives two-sided p < 0.05; the test decides, not the net count. A net gain of 6 is only the minimum that can reach it (6–0 gives p = 0.031; 5–0 gives 0.0625), and with discordant losses more is needed (7–1 gives 0.070, 8–2 gives 0.109). Smaller gains are deliberately dropped: the system keeps only steps whose value the data can show. One consequence the owner should see: at about 10 baseline misses of 30 per type, the 49% reduction Contextual Retrieval reports would fix about 5 questions, below what the test can confirm, so the rule may drop context prefixes even at the published effect size.

| Spike | Runs | Question | Measure | Decision rule (A6) |
| --- | --- | --- | --- | --- |
| `retrieval-ablation` | After the extended evaluation set is accepted; uses SRV-INGEST's span dump and the `embed` service; runs before the revised SRV-INGEST and SRV-SEARCH specs are approved | Which retrieval steps earn their place on one source | On exact, thematic and fact-check questions: recall@10 and sufficiency for L1 spans against the current chunk baseline; plus context prefix; plus L2 summaries; plus the `form` filter; plus RRF hybrid. Vector step p95 alone at the measured size. Tokens, USD cost and wall time per pack of context prefixes, L2 summaries and the span re-embedding. The fact-check route against today's whole-section fact-check, on the fact-check questions | Keep a step if, on its question type, its gain is significant (exact McNemar p < 0.05; at least a net 6 of 30). Its measured cost per pack sets the initial cap (B7). Exact scan while the vector step alone has p95 ≤ 150 ms (initial value). The fact-check route replaces the whole-section baseline in the pipeline only if it is not significantly worse and not worse by more than a net 2 of 30; otherwise the agent blocks for the owner with both measurements, since decision 22 chose the route |
| `judge-gate` | After `retrieval-ablation`, the judge slots (`V.kl-agent-judges`) and the deployed lane (`V.kl-deploy-judge-lane`, with its initial memory limit) | Whether and when the online judge runs, and the lane's settings | Per channel budget (MCP at 2,000 tokens; tutor at its source budget, with the judge input at the 3,000-token cap): Haiku `judge.find` p95 and judged-path completion inside the timeout on the `worker-judge` lane, one CLI spawned per call, including the queue hop and the proxy; the steps other than the judge; peak memory of the lane, which a paired OPS-DEPLOY change then sets as its limit. Gate fire rate, judged-path completion rate and sufficiency for: score order only; judge on every query; judge gated at several margins. Every judged variant places the judge after the selection (B6). As a reference, the earlier order (judge on the top 20 before the selection) runs once at the best margin. USD cost of one full evaluation run | Pick the cheapest variant that is not significantly worse than the best, is not worse by more than a net 2 of 30 questions on any type, and has `find` p95 < 2.5 s. It also sets the timeout, the judge-input cap and the lane concurrency, per channel where they differ. If the judge-after-selection order is significantly worse than the reference, or worse by more than a net 2 of 30 on any type, the agent blocks the task for the owner with both measurements, because the order is an owner decision (18). The reference is not a candidate in the pick rule. The factor stays 1.5 in this spike, so the block also says that a loss may come from a factor that is too small. The evaluation cap is set from the measured run cost |
| `pack-links` | Stage 7, before SRV-KB, on Bashir plus two openly licensed sources (BIP texts, Bitcoin Optech topics) | Variant A or B for storing pair link runs (B2); the facet-filter (`role`, `reliability`, `as_of`) and current-state rules, which need several sources | Property test over recorded model outputs: after random add, remove and rebuild sequences, the active link set equals a fresh build of the same pack set. Rows, lines of code and overlays rebuilt when one pack is rebuilt. The facet-filter and current-state measures use the `retrieval-ablation` method | Both variants must pass the property test. Pick the variant with fewer lines of code, unless the other rebuilds ≥ 30% fewer overlays on a pack rebuild (initial value). Facet filters use the `retrieval-ablation` rule |

The stage 0 `trace-backend` spike is done and MLflow is deployed (ADR-0032); this ADR adds no work to it.

### B11. Which spec owns what

| Piece | Spec |
| --- | --- |
| Base packs, overlays (profile, tags, vectors, prefixes, L2 summaries), manifest, GC and purge, locator span ids, quote hash, re-anchoring, the citation migration, property test for packs and overlays | SRV-INGEST |
| Span-level vector overlay, `find` cascade, routes, policy, greedy selection, `judge.find` call and degradation, `judged` | SRV-SEARCH |
| Judge slots, the judge lane, recorded judge outputs, the `index` and `eval` ledger lines and caps | SRV-AGENT, OPS-DEPLOY (the `worker-judge` service, its networks and memory), OPS-SANDBOX (the ADR-0025 tests extended to it) |
| Text-free `find` spans, `policy_version`, the `obs.eval` job, judge labels, the `judge_inputs` store, alignment and its proposals, prompt registration | SRV-OBS |
| The labelling screen for judge traces | WEB-MANAGE |
| Judge task classes and the new caps as settings | SRV-CONFIG |
| `worker-judge` in `reader up`, secret delivery, backup and restore; the proposals export | OPS-CLI |
| Locator citations in lessons and the notebook; the seq-id resolver for published revisions | SRV-LESSON, WEB-NOTEBOOK |
| Freshness inputs over locator ids and the one re-scan | SRV-FRESH |
| The `judge.find` queue and the second consumer | SRV-EVENTS |
| Check order after drafting, loop limit, fact-check through the search route, Admiralty credibility | SRV-PIPELINE |
| L5 boost in the tutor route; tutor retrieval through `find` | SRV-TUTOR |
| Concept registry, linking (B4), pair link runs, property test for links, findings validity and `orphaned` | SRV-KB (new) |
| `brain/v1` tools and invariants | SRV-MCP (new) |
| Export of the registry and link runs | SRV-EXPORT (new) |

## Amends

| ADR | Section | Was | Becomes | Why |
| --- | --- | --- | --- | --- |
| ADR-0001 | Process | No rule for changing an ADR; template has `supersedes` only | Part A rules A1–A9 (A9 also amends ADR-0019, row below); the template gains `amends: []` | Changes by delta keep decided ADRs readable and keep rework visible |
| ADR-0005 | Budgets and cost ledger | $200 credit split as compile ≤ $100 and tutor, per task class | Pack overlays (context prefixes, L2 summaries, link judging) are recorded on their own `index` line of the ledger, with per-pack and per-pair-run caps set from the costs `retrieval-ablation` measures; when a cap is reached the owner tops the credit up. `judge.find` runs on the caller's provider and counts against the caller's tutor or MCP use. `judge.eval.*` and `judge.align` have an `eval` cap set from the run cost measured by `judge-gate`. **Compile cost under decision 22:** at the stage floor reserves, a first pass is $0.61 + $0.25 + $0.53 = $1.39 and each loop $0.88 + $0.25 + $0.53 = $1.66, so three loops come to about $6.37 per lesson at today's floors, about $147 for 23 lessons (an estimate, not a bound: the floors are minima, a live draft already cost $0.6233 against the $0.61 floor, and the reordered stages will have floors of their own). **The bound is the cap:** `budget.compile_cap_usd`, $8 per lesson (owner, 2026-11-22), $184 for 23 lessons of the $200 monthly credit. **Rule:** a loop starts only if its reserve, the sum of the FR-AGENT-9 reserves (max of the floor and the worst-case estimate) of the loop's calls, fits under the cap together with what the lesson has already spent. Because reserves are worst-case estimates (today's deploy budgets size two worst-case calls at about $4, $2 and $4 per stage), a loop's summed reserve is several dollars, so at the $8 cap the rule will usually stop after one or two loops: "up to 3" is the limit, rarely the outcome. The revised SRV-PIPELINE resizes FR-AGENT-9's per-task `budget_usd_per_lesson` for the four checks of the new order. A `judge.find` call made from the fact-check route is part of the lesson's compile run and counts in this reserve. ADR-0005's $4.35 becomes a target that the revised SRV-PIPELINE reports against, not a stop, and ADR-0005's $100 compile share is exceeded by design up to the cap; this is an agent decision for the owner to confirm (Decided by the agent, item 8). Owner decision 15 covers indexing money only | 23 lessons × $4.35 = $100.05 already fills the compile share; new model work needs its own owner-set money and a cap. The ADR-0005 invariant forbids borrowing credentials. Measured L02 compiles with one loop cost $2.42–$2.97, and 0 of 2 converged after one loop (SRV-PIPELINE), so more loops will be used |
| ADR-0007 | Decision, retrieval | Hybrid full-text plus pgvector HNSW, merged by RRF | Exact distance on the filtered set, already built (FR-SEARCH-7, `search/migrations/0001_search.py`), until the vector step alone exceeds p95 150 ms; RRF per route, off until `retrieval-ablation` shows a gain; data organised as base packs and overlays behind a manifest (B2) | HNSW with filters loses most results at default settings; exact search has 100% recall; hybrid measured worse than dense (24 vs 27 of 40) |
| ADR-0008 | Decision 3, spans | Every chunk keeps its span | A span is one Docling element of a base pack and keeps a locator as its identity (P2: offsets into the page's canonical span text, or the file and line-range form), plus a quote hash for re-anchoring; stored `<sha>:<seq>` ids migrate in the P2 order, and published revisions resolve through a map instead of being rewritten (owner decision 20). Context prefixes and inherited tags are overlays (B2). Decision 4 (`license`, `personal-use` never exposed) is unchanged and becomes the `license` facet | Contextual Retrieval reduced failed retrievals by 49%, and by 67% with reranking (Anthropic, 2024). A locator identity keeps citations stable across converter upgrades |
| ADR-0009 | Stage 1 output; drafting; stage 4 fact-check; order of the checks after drafting | Outline, chunks with spans, embeddings; claims cite spans; fact-check → lab verification → adversarial review | Stage 1 outputs a base pack and its overlays. Drafting puts the whole source section in context when its measured token count fits the policy budget. Fact-check reads through the fact-check route (B6). After drafting, the checks run cheapest first and the first failure loops back to the draft: schema and citation presence (rules, 0 model calls) → lab verification (sandbox runs) → claim support (with a model) → adversarial review (last). Up to 3 loops; every return to the draft counts as one, a rule failure included; a loop starts only if it fits under `budget.compile_cap_usd` (ADR-0005 row). Every claim gets an Admiralty credibility 1–6. This replaces SRV-PIPELINE's measured one-loop deviation and its order `draft → fact_check → adversarial_review` (owner decision 22). The freshness check of ADR-0030 keeps its place beside fact-check | The adversarial review then reads only a draft that has passed every cheaper check (owner decision 18) |
| ADR-0015 | Knowledge model; tool table | One sector hierarchy; six tools | Sectors become the `topic` facet and stay a tree. A concept registry (L3) is added. Findings gain validity (`valid_from`, `superseded_by`) and the derived state `orphaned`. The tool table is replaced by `brain/v1` (B8), which keeps role filtering, the 300-character limit and the review queue as invariants | Small models are more sensitive to similar tools. One card shape and `next` links are the hypothesis that cuts calls per answer, measured in B9 |
| ADR-0018 | Evaluation; v1/v2 split | Evaluation harness in v2 (the backend part is already decided by ADR-0032) | A retrieval evaluation with LLM judges (B9) and the prompt no-regression rule (B7a) move into v1, on top of ADR-0038's bench datasets and Runs. The general regression gate for every prompt and model change stays in v2 | The owner wants the RAG traced and evaluated from the start; retrieval evaluation is the minimum that makes the Haiku target checkable |
| ADR-0019 | Merging pull requests | The agent merges when CI is green and the PR does not change an ADR status | Also excepted: a change that adds or amends an ADR (A9); the final integration PR waits for the owner. AGENTS.md loop step 6 says the same | An ADR changes what later tasks may do; merging it is a decision, as a status flip is |
| ADR-0019 | What still stops a task (not the whole run) | Five conditions | Also blocks: `deep-reviewer` has not returned `VERDICT: approve` after two rounds on the same spec (owner, 2026-11-26). AGENTS.md "What blocks a task" says the same | Without a round limit the agent can loop on reviews without end |
| ADR-0012 | Decision, Compose layout | The services of ADR-0012 and the kernel services of ADR-0022/0025 | One more static service, `worker-judge` (B7b) | The judge lane |
| ADR-0025 | Rules 1 (caller control), 2 (tools), 3 (process controls), 4 (Compose services), 5 (routes) and `verified_by` | Only `worker` is on the `agent-proxy` network and holds per-run handles; the process controls, the service list, the routes and the verifying tests name the worker alone | `worker-judge` is added to each of them as B7b states: on the `agent-proxy` network with the caller's per-run handle; the `judge.find` task only, empty tool set; the worker's hardening; reaches `agent-proxy`, Postgres and Rotel only; every worker test also runs against it. One CLI process per call, spawned after the reservation and the token, exactly as the worker does today; at most 4 live tokens on the lane, never evicting a `worker` token | An online judge cannot wait behind a concurrency-1 worker that an embed job held for 12.0 minutes live and prefix batches hold for hours; putting the api on the proxy network is the exposure ADR-0025 removes |
| ADR-0032 | Decision 5 (privacy, the attribute allow-list) and Decision 1 (networks, as already widened by ADR-0038 Decision 6) | The allow-list holds today's job and stage attributes; the `otel` network holds api, worker and rotel | `TOOL`, `ROUTER` and `RETRIEVER` attributes join the allow-list with ids, counts, `brain_version`, `policy_version` and content hashes only, never card, question or extracted-identifier text; `worker-judge` joins `otel` to export its spans. The `obs.eval` job runs in the existing worker under ADR-0038 Decision 1, so no new reach to `mlflow` | The `find` cascade must be visible in MLflow; ADR-0034's rejection keeps text out |
| ADR-0035 | Decision 1, the assessment source | The worker writes assessments with `source` CODE only (HUMAN added by ADR-0038) | The worker also writes LLM_JUDGE assessments, from the `obs.eval` job only, scores and enums only, never text; their names join VEC-OBS-5 | Judge scores must be visible next to code scores; ADR-0035's writer and its text-free rule stay |
| ADR-0038 | Decisions 1–3, bench scoring and HUMAN assessments | Scores come from code; no model call, no judge; HUMAN names `owner.lesson_ok` and `owner.answer_ok` only | Under its own `revisit_when` ("a bench needs a model call to score"): the `obs.eval` job scores with `judge.eval.*` locally and writes LLM_JUDGE assessments through the worker's own HTTP client (new VEC-OBS-5 `writes` entries); HUMAN assessments gain `owner.<slot>_ok` per judge slot, pass or fail only; the `judge.align` job proposes judge prompts from those labels. The MLflow client package and `mlflow.genai.evaluate` stay out of the worker | Judges and their alignment need labels per judge; scores only, so the text-free rule holds |

## Rework (A8)

| Area | Files and tables that change | ~Lines | Approved ids that change with their tests (H2) |
| --- | --- | --- | --- |
| Span identity, quote hash, re-anchoring, converter revision, re-conversion, seq-to-locator map, citation migration | `material_spans` columns (locator, `quote_sha256`) and a migration; the host converter and `ingest/host/mapping.py`; `ingest/jobs.py`; a re-anchor job; citation readers in pipeline and tutor; the artifact format and the pilot hashes | 450–650 | FR-INGEST-6, -10, -11, -12, -13, -14, -17; INV-INGEST-1, -2; VEC-INGEST-1, -3, -4; FR-PIPELINE-7; FR-TUTOR-5, -8; INV-SEARCH-2 |
| Locator citations outside ingest: lesson citation schema, the read-time resolver for published revisions, notebook rendering, freshness inputs and the one re-scan | `lessons/`, `web/` notebook citation component, `freshness/{check,scan,sample}.py` | 150–300 | VEC-LESSON-3, INV-LESSON-6 (unchanged, retested); FR-NOTEBOOK-2, INV-NOTEBOOK-2; FR-FRESH-3, -8, -10, VEC-FRESH-7 |
| Packs, overlays, manifest with CAS, GC and purge | New tables; `ingest/manage.py` | 600–900 | FR-INGEST-13, -31, -32; FR-SEARCH-4, -7, -9, -20 |
| `find` cascade, policy, greedy selection, token budget, span-level vectors | `search/store.py`, `search/params.py`, new router, selection and policy modules; the route shape; 8 test files in `server/tests/search/` | 800–1,200 | FR-SEARCH-3, -5, -7, -8; INV-SEARCH-4; NFR-SEARCH-1; VEC-SEARCH-1, -2, -5 |
| Judge slots, recorded outputs, ledger lines, the per-call path and the token-table bound | `agent/ledger.py`, `agent/provider.py`, alias data | 250–400 | FR-AGENT-3, -6, -9, -12, -17 |
| New settings: judge task classes, the `index` and `eval` caps, per-pack and per-pair-run caps | `config/` schema, the effective-settings overlay | 60–120 | SRV-CONFIG schema rows for `tasks` and `budgets`; FR-CONFIG-25, -26 (only if the owner adds the caps to the UI allow-list) |
| The judge lane as a service: queue keyed by request id, second consumer, `judge_requests` transport and its collector, networks, memory, secret delivery, the ADR-0025 tests | `deploy/compose.yaml`, `jobs/`, OPS-SANDBOX tests | 200–330 | FR-EVENTS-9, -10, -11; FR-DEPLOY-3, VEC-DEPLOY-2; INV-SANDBOX-3 and the tests behind ADR-0025's `verified_by` |
| `reader up`, backup and restore with the new service; the proposals export | The `reader up` generator and its override, `cli/` backup and restore | 80–150 | FR-CLI-4 (the services given the `reader.yaml` bind), FR-CLI-5 (`db-password` consumers), FR-CLI-17 and FR-CLI-18 (backup and restore stop and start `worker-judge` with api, worker and mcp), plus the expected-override vector |
| The labelling screen for judge traces | `web/src/manage/` | 150–250 | FR-MANAGE-6 (a new owner tab on `/settings`), FR-MANAGE-1 primitives reused |
| `policy_version`, `obs.eval`, judge labels, `judge_inputs`, alignment proposals, `find` spans | `obs/attributes.py`, `obs/feedback.py`, `obs/labels.py`, `obs/bench.py`; 22 test files in `server/tests/obs/`, the canary tests among them | 350–550 | FR-OBS-14, -21, -22, -23, -24; VEC-OBS-1, -4, -5; INV-OBS-1, -2 (unchanged in substance, retested) |
| Pipeline check order, loops, the cap rule, fact-check route, credibility | `pipeline/steps.py`, `pipeline/stages.yaml`, the SRV-LESSON finding schema | 300–500 | FR-PIPELINE-4, -5, -6, -8; NFR-PIPELINE-1; VEC-PIPELINE-1, -2; INV-PIPELINE-1, -2, -3, -5 |
| Tutor through the cascade | `tutor/jobs.py`, `tutor/answer.py` | 100–200 | FR-TUTOR-5; NFR-TUTOR-1, -2, -3; INV-TUTOR-2 |
| **Total** | | **about 3,490–5,550** across 15 approved specs: SRV-INGEST, SRV-SEARCH, SRV-AGENT, SRV-OBS, SRV-PIPELINE, SRV-TUTOR, SRV-LESSON, WEB-NOTEBOOK, SRV-FRESH, SRV-EVENTS, OPS-DEPLOY, OPS-SANDBOX, OPS-CLI, SRV-CONFIG, WEB-MANAGE | |

| Changed with this ADR | Count |
| --- | --- |
| New ADR | 1 |
| ADR template (`amends:` field) | 1 |
| Pointer lines in earlier ADRs (0001, 0005, 0007, 0008, 0009, 0012, 0015, 0018, 0019, 0025, 0032, 0035, 0038) | 13 |
| Execution plan `docs/plans/adr-0039-execution.md` | 1 |
| Roadmap, AGENTS.md, `docs/architecture.md`, `docs/progress.yaml` | per A4, in the same change |

## Decision

- Knowledge is built from immutable base packs and versioned overlays, combined by an append-only manifest; removing a source costs one manifest entry. Spans are identified by ADR-0008 locators with a quote hash.
- Tags are facets declared as data.
- Every selection over the graph or the embeddings is greedy with stated bounds.
- `find` is a cascade with at most one call to the online judge slot, made last, only on the cards already selected, on a dedicated judge lane, within a timeout after which it returns `judged: false`.
- After drafting, checks run cheapest first with up to 3 loops, each started only under the per-lesson compile cap; fact-check reads through the search route.
- Agents use the stable `brain/v1` contract.
- Everything is traced and evaluated in MLflow; traces stay text-free; judges that read text run locally and upload scores; prompts are reviewed files registered there.
- Amendments to earlier ADRs follow Part A.

## Options

| Option | Gives | Costs (numbers) |
| --- | --- | --- |
| **Base packs, overlays, manifest, faceted tags, greedy cascade with a judge lane, brain/v1** | Adding or removing a source is one manifest entry; L1–L3 answers reproducible on the judge-free path and by replay; one interface for any model | About 3,490–5,550 lines of rework across 15 approved specs; one more service; compile cost about $6.37 per lesson at today's floors (an estimate), bounded by the $8 per-lesson cap |
| Keep today's search (chunks, tombstones, dense top-k) and add only a quote hash | About 100 lines | No manifest or `brain_version`; no judge; selection is not budget-aware; does not meet owner decisions 20–23 |
| One mutable index, updated in place | Fewer concepts | Removing a source deletes its rows plus every link to it across the whole link table and stale mixed summaries; no rollback; no version to reproduce an answer |
| Pack id over everything (parse, tagger, models) | One id per build | Every tagger, prompt or model change creates new span ids, so every citation goes inactive and every finding becomes `orphaned` |
| Global GraphRAG communities | Strong thematic answers | Recomputing communities over the whole graph on every new source costs in proportion to the corpus, not to the new pack |
| LLM judge on every candidate | Best ranking per query | About 50 calls per `find`; at about 2 s per Haiku call (measured), far beyond 2.5 s |
| One judge call on the top 20 before the selection (first draft) | The judge can lift a candidate the greedy would not pick | About 4,600–5,600 input tokens, 55–65% of them on cards the response never returns, at the measured Russian span sizes (B6); against owner decision 18 |

## Rejected options — why (numbers)

- **Keep today's search.** It costs least, but the owner chose to build packs, the manifest and the judged cascade now (decision 23), and seq ids do not survive a converter upgrade.
- **Mutable index.** The cost of removing a source grows with the whole link table, against one manifest entry. There is no `brain_version` to reproduce an answer.
- **Pack id over everything.** A single prompt edit would orphan 100% of the findings that cite that source.
- **Global GraphRAG communities.** Work per added source is proportional to the corpus. Pair linking with concept blocking is proportional to the two packs: about 15,000 pairs instead of 4 million in the B4 example.
- **LLM judge on every candidate.** About 50 calls per query against a budget that allows at most one call (B6).
- **Judge before the selection.** Owner decision 18 puts the judge last. At the measured span sizes, 20 judged cards against about 7–9 returned: 55–65% of the judge's input per call is spent on cards that are dropped anyway, and the input grows with the candidate count instead of the budget. `judge-gate` measures whether lifting such cards is worth it; if it is, the owner decides (B10).
- **The api on the proxy network; one worker at higher concurrency.** See B7b.
- **Card text in traces for shareable material.** ADR-0034 was rejected; scores computed locally give the same evaluation without text in MLflow.
- **One raw `search` tool.** A cheap model would stitch context itself, spending calls and tokens. Self-contained cards with `next` remove that work (hypothesis, measured in B9).
- **One tool per layer.** Five or six similar tools are the case where tool-selection accuracy drops, most for small models.
- **Exact optimisation for selection.** Budgeted max coverage, set cover and correlation clustering are NP-hard. The greedy algorithms of B5 carry proven bounds at linear or near-linear work.
- **All tags embedded in text.** Every owner decision about a source's role or reliability would re-embed the whole index.
- **Decision thresholds of 0.05 or 0.02 on 30 questions.** One question is 3.3% of a 30-question type. The standard error at p = 0.8, n = 30 is about 0.073, so such thresholds sit below the resolution of the data.
- **A lower 95% bound for the 90% target.** At 90 questions that bound would demand a point ratio near 0.98, far stricter than the owner's 90%.

## Consequences

- **Easier:**
  - adding a book is an increment with a measured value;
  - removing it is instant and reversible;
  - citations survive converter upgrades;
  - agents on cheap models get one stable interface;
  - a local model can take over a judge slot by passing the same evaluation.
- **Harder:**
  - every read goes through manifest views;
  - overlays and derived data need version keys;
  - a converter revision, a re-conversion and a one-time migration of every mutable citation, plus a resolver for published revisions;
  - one more service on the credential network;
  - each facet needs a consumer;
  - the extended evaluation set must be accepted before the stage-V spike decisions;
  - compile cost per lesson rises with up to 3 loops: about $6.37 at today's floors (an estimate, not a bound), bounded by `budget.compile_cap_usd` at $8 per lesson, $184 for 23 of the $200 monthly credit.
- **Forbidden:**
  - updating rows of another pack or overlay;
  - deleting on a request path;
  - enumerating subsets over graphs or embeddings;
  - free-form tags;
  - live judge scores as ranking priors;
  - Claude outputs as tuning data;
  - unreviewed findings in agent results;
  - material, question or answer text in traces;
  - MLflow on the request path;
  - the api holding a provider handle.

## Decided by the agent, for the owner to confirm

The owner decided decisions 1–23. These choices were made by the agent while restating the ADR on 2026-11-26; each stays as written unless the owner changes it on acceptance:

1. **The judge lane** is a second worker service, `worker-judge`, at concurrency 4 (initial value), and the api waits for its result by `LISTEN/NOTIFY` (B7b).
2. **No warm pool:** each judge call spawns its own CLI after the reservation and the token, exactly today's FR-AGENT-3/-6 path, and the lane holds at most 4 live tokens, never evicting a `worker` token (B7b). The query and card text travel in a `judge_requests` row deleted at the job's end or deadline, never in job args (B7b).
3. **The judge timeout** is 2.2 s (initial value). Its basis is weak: n = 3 fresh-container runs of a short prompt, not a 3,000-token judge input, a queue hop or the proxy (B6).
4. **Expanded cards** take their parent's score times a decay in policy (B6 step 5).
5. **Label names** are `owner.<slot>_ok` with dots as underscores, pass or fail only; the reason stays in Postgres (B7 labels).
6. **Alignment hand-off:** the job writes a proposal row; the agent reads it through an owner-run `reader obs proposals` export and commits it as a prompt file through review and the no-regression rule; an 8-word verbatim guard, run in the `judge.align` job and again in the export, keeps material, question and answer text out (B7 labels).
7. **Revisit trigger:** `judged: false` on more than 20% of gated queries for a week (initial value).
8. **The loop rule:** a loop starts only if the sum of its calls' FR-AGENT-9 reserves fits under `budget.compile_cap_usd` ($8) with what the lesson has spent; ADR-0005's $4.35 becomes a target, and the 23-lesson total may reach $184 of the $200 monthly credit instead of $100 (Amends, ADR-0005).
9. **Cards are cut at read time,** never split at ingest, so the card budget stays policy (B1).
10. **The locator scheme:** offsets into the page's canonical span text, a zero-length range with an ordinal for empty text, the file and line-range form for non-PDF; published revisions resolve through a map rather than being rewritten (P2).
11. **Context prefixes run on the worker** in batched jobs of 25 spans (initial value), so a tutor answer can wait up to 45–55 s behind a batch while a pack is prefixed, above NFR-TUTOR-1's 30 s (B7).
12. **The `judge_inputs` store:** owner-only, 30 days (initial value), other users' queries only with their ADR-0018 evaluation consent (B7 labels).
13. **Personal-use material on MCP** returns no L1 span cards and no link into them, only L2 summaries and L4 findings with quotes of at most 300 characters; internal channels (tutor, pipeline, judge input) keep full span text within their own budgets (B8, personal-use table).
14. **Expected facts of the evaluation set** stay out of MLflow (B9).
15. **Per-channel response budgets:** MCP starts at 2,000 tokens (owner decision 17); the tutor's starts at its current source budget (B8).
16. **`judge.find` jobs are keyed by request id**, and a worker job that calls `find` waits for the judge the same way the api does (B6).
17. **The lane's initial memory limit** is four times the per-call CLI footprint plus the worker's base (the footprint is unmeasured), replaced by the `judge-gate` measurement in a paired OPS-DEPLOY change (B7b).
18. **An absolute judge-input cap**, 3,000 tokens initial: the judge reads min(1.5 × the channel's response budget, the cap), the top slice of the selection, so the tutor's larger budget never exceeds the input the latency budget was set for (B6 step 6).
19. **`personal-use` text only for an entitled caller** (the pack owner or a holder of their own copy of the same sha256) on the tutor, pipeline and judge-input channels; every other caller gets the MCP row (B8; P10).
20. **The token quota is enforced in `agent-proxy`** per client service (4 for `worker-judge`), refusing rather than evicting, with expired entries purged and the lane revoking its tokens at start (B7b).
21. **Late `judge.find` outputs are kept for replay**, and `job_keys` rows of the judge kinds are pruned after one day (initial value) (B7b).
22. **A per-token daily judge cap on MCP** (stage 7, SRV-MCP; initial value set there) and the compile run as the ledger line of a `judge.find` made by the fact-check route (B7).
23. **The stage-V evaluation and both spikes run on the internal owner channel**; the stage-7 MCP evaluation reports `personal-use` Bashir questions separately (B9).

Facts to measure:
- Context-prefix calls for the pilot: about 9,500 at the measured 10.1 spans per page (9,463 / 935), about 4.8–5.5 hours of worker time. `retrieval-ablation` measures the real cost and wall time; the compile share (23 × $4.35 = $100.05) is already full, so indexing is a separate ledger line.
- The judged-path completion rate inside the timeout, the steps other than the judge, and the lane's memory (`judge-gate`).
- Compile cost per lesson under up to 3 loops (the revised SRV-PIPELINE).

## Sources

- **Retrieval:**
  - Anthropic, Introducing Contextual Retrieval (2024): 49% and 67% fewer failed retrievals; a full prompt below about 200,000 tokens.
  - Sarthi et al., RAPTOR, ICLR 2024: +20% absolute accuracy on QuALITY with GPT-4.
  - pgvector 0.8.0 release notes and AWS Database Blog: overfiltering with `ef_search = 40`; iterative index scans; exact search gives 100% recall.
  - Virginia Tech, metadata-aware retrieval (arXiv 2601.11863): metadata prefixes require re-embedding the full index on update.
- **Tools and latency:**
  - Tool-DC (arXiv 2603.11495): tool-calling accuracy degrades as candidate tools grow, most for smaller models; similar tools confuse argument filling.
  - `spikes/agent-sdk-container/RESULT.md`: Haiku CLI ready 0.89–1.20 s, result 1.82–2.10 s.
  - docs/research/agent-tracing-2026-11.md: the Agent SDK runs the Claude Code CLI as a child process.
- **This repository:** SRV-INGEST, SRV-SEARCH, SRV-AGENT, SRV-OBS, SRV-PIPELINE, SRV-TUTOR, SRV-EVENTS specs; `docs/progress.yaml` (S0.spike.tutor-chunks, V.SRV-SEARCH-min, S0.spike.tutor-call).
- **MLflow documentation:** OTLP ingestion; RETRIEVER span type; assessments with `source` HUMAN and LLM_JUDGE; Prompt Registry; judge alignment (at least 10 traces, 50–100 recommended).
- **Approximation bounds:**
  - Nemhauser, Wolsey and Fisher (1978);
  - Khuller, Moss and Naor (1999);
  - Tang et al. (arXiv 2008.05391);
  - Greedy+Singleton ratio (arXiv 2611.13575, 2026 preprint, not peer-reviewed);
  - Feige (1998);
  - Ailon, Charikar and Newman (2005).
- **Classification:** Ranganathan, faceted classification (Colon Classification, 1933).
