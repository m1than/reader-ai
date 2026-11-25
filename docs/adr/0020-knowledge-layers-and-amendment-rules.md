---
id: ADR-0020
title: Knowledge layers as append-only source packs, faceted tags, greedy retrieval with judges, the brain/v1 agent contract; rules for amending decided ADRs
status: proposed
date: 2026-11-25
verified_by: "RESULT.md of spikes retrieval-ablation, judge-gate and pack-links applying the decision rules below; SRV-INGEST property test: after any random sequence of pack add, remove and rebuild, the active view equals a fresh build of the same pack set; SRV-SEARCH MLflow evaluation meets the brain/v1 targets; trace-check green"
revisit_when: "A cheap model stays below 90% of the strong model's retrieval sufficiency after two policy versions; find p95 reaches 2.5 s on the owner's Mac; more than 200 packs are active; or brain/v1 needs a breaking change"
supersedes: []
amends: [ADR-0007, ADR-0008, ADR-0009, ADR-0015, ADR-0018]
---

## Context

Owner decisions, 2026-11-25:

1. Knowledge data is append-only. Adding a source adds an increment to the knowledge base; it never changes what is already there. Removing a source is cheap.
2. The knowledge base is the second brain for any agent: an agent on any cheap model must find what it needs in few calls, fast, with a simple interface.
3. MLflow is the trace and evaluation tool.
4. Judge models select what an agent receives through MCP. In v1 judges run on Claude; the method must let a local model replace Claude in a named place later, without changing the method (local models are out of scope now; ADR-0018 keeps them in v2).
5. Selection over graphs and embeddings uses greedy algorithms only. Several selection problems here are NP-hard; they are solved approximately, never by exhaustive search.
6. Tools behind the interface will change; the interface stays.
7. Targets: a cheap model reaches at least 90% of a strong model's retrieval sufficiency; median 3 calls per answer or fewer; at most 2,000 tokens returned per answer; `find` p95 below 2.5 s.
8. Where two designs are both viable (who owns links between sources), build both on a data set and decide by measurement.
9. This decision goes into a separate ADR that also sets the rules for amending decided ADRs and for keeping rework small.
10. Claude Haiku is the cheap model the system is built around. Prompts and rules are written carefully against real data, versioned in MLflow, and every run is traced against the prompt versions it used.

State on 2026-11-25: no code exists under `server/`; SRV-INGEST, SRV-SEARCH, SRV-KB and SRV-MCP are `todo` in `docs/progress.yaml`. Every change below costs text now and 0 lines of code.

## Part A — Rules for amending decided ADRs

| # | Rule |
| --- | --- |
| A1 | **Decided means decided.** ADR-0001 … ADR-0019 count as decided from the owner's acceptance on 2026-11-20, whatever their `status` line says until the owner's commit. Their Decision, Options and Rejected sections are not rewritten |
| A2 | **Change by delta.** A change to a decided ADR is a new ADR with `amends: [...]` in its frontmatter and an Amends table: ADR, section, was, becomes, why. Whatever the table does not name stays in force. `supersedes` is used only when an ADR is replaced as a whole; the status flip stays the owner's (H3) |
| A3 | **One pointer line.** The amended ADR gets exactly one edit: a blockquote at the top of its Context, `> Amended by [ADR-NNNN](...) (date): <one line>. Where the two disagree, ADR-NNNN wins.` This is the form ADR-0001 already uses for ADR-0019 |
| A4 | **Same PR, every dependent artifact.** Pointer lines, one sentence in each affected roadmap row (prefixed `ADR-NNNN:`), and `docs/progress.yaml` tasks for new spikes. An approved spec changes only with its tests (H2), and only in the stage that owns it |
| A5 | **Contracts in ADRs, mechanisms in config.** An ADR fixes interfaces, invariants and decision rules. Models, indexes, thresholds, prompts and judge versions are config or versioned policy, recorded on every trace; changing them needs no ADR. Changing a contract (a schema, the tool surface, an invariant) needs one |
| A6 | **Decide forks by a rule written first.** A fork with two viable options and a measurable difference becomes a spike whose decision rule is written in the ADR before the spike runs. The spike's RESULT.md applies the rule; a new ADR is needed only when no option passes or a revisit_when condition is met |
| A7 | **Measure at the last responsible moment.** A spike runs as the first task of the stage whose spec consumes its numbers, so no spike is redone because the design moved after it ran |
| A8 | **State the rework.** Each amending ADR states the files it changes now and the existing code that must change. The target for existing code is 0 |

This ADR is the first one written under these rules.

## Part B — Knowledge layers

### B1. Layers and their depth

| Layer | Unit | Depth bound | Card budget (tokens) | Built by | Lives in |
| --- | --- | --- | --- | --- | --- |
| L0 Source | Source profile: role, nature, reliability, `as_of`, `license_mode`, language, cost | 1 node per source | ≤ 200 | Ingest; owner confirms at G1 | Pack |
| L1 Span | One Docling element: paragraph, table, code block, formula, test vector. Code and vectors are never split | 1 | 200–500 of text plus a context prefix of about 50–100 | Ingest | Pack |
| L2 Structure | Node of the source's own outline, with a summary | As in the source, capped at 4 levels | Leaf ≤ 100, chapter ≤ 300, root ≤ 500 | Ingest | Pack |
| L3 Concept | Canonical concept (`con:bip-32`) with ru/en aliases | Topic tree ≤ 3 levels; top level approved at G1 (ADR-0015) | ≤ 150, at most 10 related concepts shown | Registry, append-only | Registry plus per-pack evidence |
| L4 Finding | Atomic claim, ≤ 3 sentences, ≥ 1 citation (ADR-0015 kinds) | Flat, typed links | ≤ 120 | Pipeline, tutor, learner | Learner store |
| L5 Learner | State per concept: known, new, gap; recall state; calibration | Flat | Not returned as content; used to filter and boost | Sessions | Learner overlay |

Navigation invariant: any span is reachable from `brain_map` in at most 3 calls (map → find → open). The L2 depth cap exists for this invariant.

For drafting one lesson, the pipeline puts the whole source section in context instead of retrieving it: the average Bashir chapter is 935 / 23 ≈ 41 pages, well under the roughly 500 pages (200,000 tokens) at which Anthropic recommends retrieval over a full prompt. Retrieval serves cross-source checks, the tutor and MCP.

### B2. Source packs and the manifest

A **pack** is everything one source contributes: its L0 profile, L1 spans with context prefixes and vectors, L2 outline nodes, L3 evidence, and the links found when the pack was linked.

| # | Invariant |
| --- | --- |
| P1 | A pack is immutable once built. Its id is the sha256 of (material sha256, ingest config, tagger version, model ids). Re-ingesting with any change yields a new pack |
| P2 | Building a pack writes only rows carrying its `pack_id`; it never updates a row of another pack |
| P3 | The brain is a **manifest**: an append-only log of versions, each a sorted list of active pack ids plus overlay versions. `brain_version` = sha256 of that list. Adding, removing and rolling back a source are each one new manifest entry |
| P4 | Every read goes through views restricted to the active manifest. A link is visible only when both of its ends are active |
| P5 | Physical deletion is background garbage collection, never on a request path: rows of a pack absent from the last N manifests (N in config) and not cited by a published lesson revision. Partition per pack (`DETACH PARTITION CONCURRENTLY`, PostgreSQL 14+) is introduced only if GC deletion is measured to hurt; until then a `pack_id` column and views suffice |
| P6 | Derived data (cross-source summaries, concept cards, gap reports, the "book turned around" view) is a cache keyed by (node, sorted contributing pack ids, builder version). It is never edited in place; a changed pack set misses the cache and rebuilds lazily |
| P7 | Findings and learner state are not in packs. When every span a finding cites is inactive, the finding gets the derived state `orphaned` and enters the review queue; nothing is deleted. Published lesson revisions stay immutable; inactive citations are shown as such |
| P8 | Vectors are stored per (pack, embedding model id). A model change adds a vector set and switches the manifest overlay; it is not a migration (consistent with ADR-0007) |
| P9 | Every trace records `brain_version` and `policy_version`, so any answer can be reproduced |

**Who owns a link between two sources** is decided by the spike `pack-links` (B10):

- **Variant A:** the pack whose linking run found the link owns it. Simple, asymmetric.
- **Variant B:** a bridge pack per pair of sources owns their links. Symmetric; the number of bridges grows with the number of pairs.

### B3. Faceted tags

| # | Rule |
| --- | --- |
| F1 | One facet, one principle of division (Ranganathan). The sector tree of ADR-0015 becomes the `topic` facet; other dimensions are separate facets, combined at query time |
| F2 | A tag lives at the layer where it is true and is inherited downward by a join at query time: license, role, reliability and `as_of` on the source; form on the span; topic on the concept. Changing a source's reliability re-indexes nothing |
| F3 | Only stable context enters the embedded text (source, chapter and section titles in the context prefix). Volatile tags stay in columns, because prefixing metadata forces re-embedding the whole index on every metadata change |
| F4 | Vocabularies are closed and declared as data (a facets file with a JSON Schema). Only `topic` grows, from findings (ADR-0015). Free-form tags are not accepted; synonyms become concept aliases |
| F5 | Every tag records `by: rule | model | owner`, a confidence and the tagger version. Rule tags (identifier regexes, Docling element types, outline position) come first at 0 model calls; model tags are proposals. A vocabulary change runs a re-tag job, as a model change runs a re-embed job |
| F6 | A facet must have a consumer: a filter, a boost, a gap check or a view. A facet unused by any query path for one stage is removed |

Default facets, open to the owner's change (Open questions 1–2):

```yaml
facets:
  role:           { level: source,  values: [map, normative, time_axis, verification, reference_impl] }
  reliability:    { level: source,  values: [A, B, C, D, E, F] }
  as_of:          { level: source,  type: date }
  form:           { level: span,    values: [prose, code, formula, table, figure, test_vector], by: rule }
  chain:          { level: concept, values: [bitcoin, ethereum, generic], multi: true }
  knowledge_type: { level: concept, values: [fact, mechanism, procedure] }
  topic:          { level: concept, tree: sectors, multi: true, grows: true }
```

### B4. Linking a new pack

Runs once per new pack P against the active brain. Each step is cheaper than the next and limits its work.

| # | Step | Method | Cost |
| --- | --- | --- | --- |
| 1 | Mentions | Identifier regexes (BIP-, EIP-, ERC-, RFC, FIPS) and the alias dictionary give evidence (concept, span); confidence 1.0 for an identifier, 0.9 for an alias | 0 model calls |
| 2 | Candidate concepts | Defined terms, glossary entries and repeated unmatched terms become concepts proposed inside P | 0 calls; a model only labels |
| 3 | Blocking | Cross-source pairs are formed only between spans that share a concept | Sum over concepts of n_P(c) · n_B(c), not \|P\| · \|B\| |
| 4 | Similarity | Within a block, keep the top m = 5 candidates per span with cosine ≥ τ | Vectors already exist |
| 5 | Judgement | The link-type judge labels a pair `agrees`, `contradicts`, `refines` or `supersedes`. `supersedes` is proposed only for a later `as_of` and `role: normative` | 1 call per candidate, within a per-pack budget in config |
| 6 | Write | Links with provenance (`rule` or `model`, confidence, tagger version), stored per B2 | — |

Order of magnitude, on an assumed example: two sources of 2,000 spans give 4 million pairs; 300 shared concepts with 7 mentions per source give 300 × 49 ≈ 15,000 pairs, about 270 times fewer. The first second source replaces these numbers with measured ones.

### B5. Greedy algorithms only

Every selection over the graph or the embeddings is a greedy algorithm with a bounded amount of work. Where the problem is an optimisation, the spec states the approximation guarantee. Exhaustive search, ILP solvers and global recomputation are not used.

| Problem | Exact version | Greedy used | Guarantee |
| --- | --- | --- | --- |
| Pick k cards that cover the query's concepts and facets | Max coverage, NP-hard | Largest marginal gain first, with lazy evaluation | ≥ 1 − 1/e ≈ 0.632 of optimal (Nemhauser, Wolsey and Fisher, 1978) |
| Pick cards under a token budget | Budgeted max coverage, NP-hard | Best of cost-benefit greedy and the best single card | ≥ (1 − 1/e) / 2 ≈ 0.316 (Khuller, Moss and Naor, 1999); refined analyses give 0.405 and about 0.429 |
| Pick sources that cover a task's terms (atlas gaps, purchase proposals) | Weighted set cover, NP-hard | Lowest cost per newly covered term first | ln n; Feige (1998) showed ln n is the approximation threshold |
| Merge `same_point` duplicates into one card | Correlation clustering, NP-hard | Pivot: take the highest-scoring unassigned card, attach its neighbours above τ, repeat | 3 in expectation (Ailon, Charikar and Newman, 2005) |
| Cross-source candidate pairs | Quadratic in sources | Blocking by concept plus top-m per span (B4) | No quality bound; work bounded |
| Graph expansion at query time | Path search can explode | At most 1 hop, top-m edges by weight | Work bounded |
| Hierarchy of a source | Clustering | The source's own outline; no global community detection | Deterministic |

### B6. Retrieval cascade for `find`

Routes, budgets and thresholds are declared as data and versioned as `policy_version`.

| # | Step | Budget |
| --- | --- | --- |
| 1 | Route by rules (question type, identifiers found) | 0 model calls |
| 2 | Candidates: active manifest and facet filters first, then BM25 and dense vectors, merged by reciprocal rank fusion | ≤ 50 candidates. Exact distance on the filtered set while it is fast enough: with HNSW at the default `ef_search = 40`, a filter that keeps 10% of rows leaves about 4 results; exact search keeps 100% recall |
| 3 | Merge duplicates by `same_point` (B5 pivot) | One card, several citations |
| 4 | Relevance and sufficiency judge (B7), only when the gate is not met | At most 1 judge call per `find` |
| 5 | If insufficient, expand 1 hop over `next` and concept links, then judge again | At most 1 expansion |
| 6 | Select under the token budget (B5 budgeted greedy). Normative sources rank above maps; on "current state" routes newer `as_of` ranks higher; superseded cards are dropped | ≤ 2,000 tokens returned |

Route table, initial policy:

| Question type | Layers | Filter |
| --- | --- | --- |
| Exact ("hardened index offset in BIP-32") | L3 → L1 | `role: normative`, identifier from step 1 |
| Thematic | L2 → L3 | None on form |
| Current state | L1, L4 | `as_of` after the source's era, `role: time_axis`, not superseded |
| Fact-check (ADR-0009 stage 4) | L1 only | Span required, reliability above a threshold in policy |
| Tutor | All, boosted by L5 | Learner's gaps first |
| MCP | L4 → L1 | `license_mode`; citation limit of ADR-0015 |

### B7. Judges and labels

Judges are **slots**: model task classes in `reader.yaml` (ADR-0005, ADR-0018 "model per task as data").

| Slot | Decides | Runs | v1 binding |
| --- | --- | --- | --- |
| `judge.link_type` | Link type between two spans (B4 step 5) | Offline, once per pack | Claude Haiku |
| `judge.find` | Relevance order and sufficiency of ≤ 20 candidates in one call | Online, gated (B6 step 4) | Claude Haiku; the gate is set by `judge-gate` |
| `ingest.context_prefix` | The 50–100 token context prefix of a span (B1) | Offline, once per pack | Claude Haiku |
| `judge.eval.*` | MLflow RetrievalRelevance, RetrievalSufficiency, RetrievalGroundedness, plus custom judges (card is self-contained; citation supports claim) | Offline, on the evaluation set and sampled traces | Claude, strong class |

Replacing Claude in a slot with a local model is a config change, allowed only when the candidate matches or beats the current binding on the evaluation set (ADR-0018 tuning rule). This is how "replace Claude in specific places" is done without changing the method.

Rules:

- Judge outputs are an append-only overlay: (target, judge@version, score, trace). A new judge version sits next to the old one, so the two can be compared on the same traces.
- A card's prior weight in ranking comes only from the owner's labels and the evaluation set, never from live judge scores. Otherwise the judge would reinforce its own choices.
- Claude outputs never become tuning data (ADR-0018). Judges may use Claude for evaluation, which ADR-0018 allows.

Labels, the owner's time used as efficiently as possible:

1. Binary label per trace and judge, with an optional one-line reason. Labels are stored as MLflow human assessments under the judge's name, the form judge alignment requires.
2. Sampling order: traces where two judges disagree; then judge scores within ±0.1 of the gate threshold; then the owner's thumbs-down in tutor sessions. Stratified across the 6 routes and across packs.
3. Batches of 25 traces (about 10–12 minutes at 20–30 s each). The first target is 50–100 labels per judge; MLflow alignment needs at least 10 and works better with 50–100.
4. Owner labels are owner data and may later feed local-model tuning (ADR-0018 excludes only Claude outputs).

### B7a. Prompts and rules in the MLflow Prompt Registry

- Every prompt the system sends (judges, context prefix, link type, the router's fallback, card text) is a named prompt in the MLflow Prompt Registry. Code loads it by alias (`prompts:/<name>@production`) and records the resolved version on the span that used it.
- The source of truth is a template file in the repository, so a prompt change is reviewed like code. A change registers a new MLflow version tagged with the commit; versions are never edited.
- `policy_version` = sha256 of the route table, the thresholds and the resolved prompt versions. A prompt change is therefore visible on every trace.
- The `production` alias moves to a new version only after the evaluation set shows no regression against the current version on the B9 metrics, and the Haiku targets hold. Moving the alias back is the rollback.
- Rules that need no model (routes, facet filters, identifier regexes, budgets) stay data files, not prompts: they cost 0 calls and are deterministic.

### B8. Agent contract `brain/v1`

The interface every agent sees. Tools and models behind it change freely (A5).

| Tool | Scope | Returns |
| --- | --- | --- |
| `brain_map()` (also a resource) | `kb:read` | Top of the brain within 1,500 tokens: sectors with counts, sources with roles, `brain_version`, how to search |
| `find(query, filters?, k?)` | `kb:read` | At most k cards, ≤ 2,000 tokens in total, chosen by B6 |
| `open(id, depth?)` | `kb:read` | Any typed id: `src:`, `sec:`, `con:`, `fnd:`, `spn:` |
| `propose(...)` | `kb:write` | A proposed finding in the owner's review queue (unchanged from ADR-0015) |

One card shape for every layer:

```yaml
id: con:bip-32
layer: L3
title: BIP-32 hierarchical deterministic keys
text: "..."                 # self-contained; carries its context
cite: [spn:<pack>/<span>]
as_of: 2013-02-11
role: normative
reliability: A1
next: [con:bip-39, con:hardened-derivation, sec:<pack>/ch03/wallets]
```

- `next` makes the brain hypertext: a weak model follows links instead of planning queries.
- Same `brain_version`, `policy_version` and request give the same response, so responses are cacheable and testable.
- Versioning: adding an optional field is allowed within `brain/v1`; removing or renaming a field or a tool is `brain/v2`, served next to v1 until traces show no v1 caller.

### B9. Tracing and evaluation in MLflow

MLflow is the trace backend (owner, 2026-11-25). MLflow 3.6 and later accepts OTLP/HTTP at `/v1/traces`, so Rotel (SRV-OBS) exports to it without changes.

Span tree of one MCP call:

- `TOOL` root with the caller (agent and model), `brain_version`, `policy_version` and the tool.
- `ROUTER` (custom span type) with the route and filters.
- `RETRIEVER` per layer used, returning Documents: `page_content` is the card, `doc_uri` is `reader://<id>`.
- `RERANKER` or `judge.find` when the gate fires, then assembly with the token count.

Pack builds are traced too: counts per B4 step (spans, mentions, pairs after blocking, judge calls, cost).

Evaluation:

- The 30-question pilot set, extended with question types (exact, thematic, current state, gap), becomes an MLflow evaluation dataset with `expected_facts`.
- Matrix: caller model (Claude Haiku, a strong Claude model) × `brain_version` × `policy_version`. Metrics: sufficiency, calls per answer, tokens per answer, p95 latency.
- LLM judges run on the dataset on a schedule and on sampled traces. Live requests record only deterministic metrics (calls, tokens, empty results, latency).
- **Pack value:** the evaluation runs before and after a pack is activated. The difference is the pack's measured contribution. A pack that lowers sufficiency is deactivated with one manifest entry.

Targets (owner, 2026-11-25): Claude Haiku as the caller reaches ≥ 90% of the strong model's sufficiency; median ≤ 3 calls per answer; ≤ 2,000 tokens per answer; `find` p95 < 2.5 s.

### B10. Spikes and their decision rules

| Spike | Runs | Question | Measure | Decision rule (A6) |
| --- | --- | --- | --- | --- |
| `retrieval-ablation` | Stage 3, after SRV-INGEST, before SRV-SEARCH; extends `embeddings-local` | Which retrieval steps earn their place | recall@10 and sufficiency per question type for: L1; plus context prefix; plus L2; plus facet filters. Exact scan vs HNSW p95 at the measured size. Cost of context prefixes | Keep a step only if it adds ≥ 0.05 recall@10 or sufficiency on its question type. Exact scan while the vector step p95 ≤ 150 ms |
| `judge-gate` | Stage 3, after `retrieval-ablation` | When the online judge must run | Sufficiency and p95 of: RRF only; RRF plus `judge.find` on every query; RRF plus `judge.find` gated by score margin, at several thresholds | Pick the cheapest variant within 0.02 sufficiency of the best whose p95 < 2.5 s |
| `pack-links` | Stage 7, before SRV-KB, on Bashir plus two openly licensed sources (BIP texts, Bitcoin Optech topics) | Variant A or B for link ownership (B2) | Property test: after random add, remove and rebuild sequences, the active link set equals a fresh build of the same pack set; links recomputed when one pack is rebuilt; rows; lines of code | Both must pass the property test. Pick the variant with fewer lines of code, unless the other recomputes ≥ 30% fewer links on a rebuild |

`trace-backend` (stage 0) no longer chooses the backend. It checks MLflow on the owner's Mac: memory at idle and under one compile, nesting of Agent SDK spans under our stage span, and a score attached to a trace. Phoenix stays the fallback if MLflow fails those checks.

## Amends

| ADR | Section | Was | Becomes | Why |
| --- | --- | --- | --- | --- |
| ADR-0007 | Decision, retrieval | Hybrid full-text plus pgvector HNSW, merged by RRF | Same fusion; exact distance on the filtered set until `retrieval-ablation` shows the vector step p95 > 150 ms; data partitioned logically by `pack_id` (B2) | HNSW with filters returns about 4 of 10 wanted results at defaults; at our size exact search is affordable and has 100% recall |
| ADR-0008 | Decision 3, spans | Every chunk keeps its span | Every span is one Docling element in a pack, with a context prefix and inherited L0 tags; ingest output is an immutable pack (B2) | Contextual Retrieval reduced failed retrievals by 49%, and by 67% with reranking (Anthropic, 2024) |
| ADR-0009 | Stage 1 output; stage 4 fact-check | Outline, chunks with spans, embeddings; claims cite spans | Stage 1 outputs a pack. Drafting puts the whole source section in context. Fact-check uses the fact-check route (B6): L1 only, reliability threshold | A chapter of about 41 pages fits in context; retrieval there would only add misses |
| ADR-0015 | Knowledge model; tool table | One sector hierarchy; six tools | Sectors become the `topic` facet (B3); concept registry L3 added; findings gain validity (`valid_from`, `superseded_by`) and the derived state `orphaned`; the tool table is replaced by `brain/v1` (B8) | Six heterogeneous tools confuse small models more than three uniform ones; a single tree forces each item into one branch |
| ADR-0018 | Trace backend; open question 1 | Chosen by the stage 0 spike, MLflow or Phoenix | MLflow (owner, 2026-11-25); the spike checks MLflow only (B10) | Owner decision |

Rework (A8): this PR changes 1 new ADR, 5 pointer lines, 6 roadmap rows and 3 progress tasks. Existing code changed: 0 lines.

## Options

| Option | Gives | Costs (numbers) |
| --- | --- | --- |
| **Append-only packs, manifest, faceted tags, greedy cascade, brain/v1** | Adding or removing a source is one manifest entry; any answer reproducible by version; one interface for any model | A view layer and a manifest table; per-pack linking job |
| One mutable index, updated in place | Fewer concepts | Removing a source deletes its rows plus every link to it across the whole link table and stale mixed summaries; no rollback; traces cannot be reproduced |
| Global GraphRAG communities | Strong thematic answers | Recomputing communities over the whole graph on every new source costs in proportion to the corpus, not to the new pack |
| LLM judge on every candidate | Best ranking per query | About 50 calls per `find`; in one measurement GPT-4o-mini averaged 1,549 ms against 241 ms for bge-reranker-v2-m3 per reranking call |

## Rejected options — why (numbers)

- **Mutable index.** The cost of removing a source grows with the whole link table, against one manifest row; and no `brain_version` exists to reproduce an answer.
- **Global GraphRAG communities.** Work per added source is proportional to the corpus. Pack linking with concept blocking is proportional to the new pack (B4: about 15,000 pairs instead of 4 million in the example).
- **LLM judge on every candidate.** Around 50 calls per query cannot meet p95 < 2.5 s when one call takes about 1.5 s. A gated single call can.
- **One raw `search` tool.** A cheap model would stitch context itself, spending calls and tokens; self-contained cards with `next` remove that work.
- **One tool per layer.** Five or six similar tools are the case where tool-selection accuracy drops, most for small models.
- **Exact optimisation for selection.** Budgeted max coverage, set cover and correlation clustering are NP-hard; the greedy algorithms of B5 carry proven bounds at linear or near-linear work.
- **All tags embedded in text.** Every owner decision about a source's role or reliability would re-embed the whole index.

## Consequences

- Easier: adding a book is an increment with a measured value; removing it is instant and reversible; agents on cheap models get one stable interface; a local model can take over a judge slot by passing the same evaluation.
- Harder: every read goes through manifest views; derived data needs cache keys; each facet needs a consumer.
- Forbidden: updating rows of another pack; deleting on a request path; exhaustive search over graphs or embeddings; free-form tags; live judge scores as ranking priors; Claude outputs as tuning data.

## Open questions

1. Is `chain` a separate facet, or a branch of `topic`?
2. Reliability: Admiralty A–F × 1–6, or three levels?
3. L5 in retrieval from stage 3, or from stage 6 once the tutor has produced learner state?
4. Judge alignment (SIMBA or GEPA in MLflow) runs an LLM over traces to rewrite judge instructions. ADR-0018 records that the Consumer Terms forbid using the service to develop or train AI models. Until the owner decides whether alignment on the subscription is acceptable, alignment does not run; judges work unaligned.

## Sources

- Anthropic, Introducing Contextual Retrieval (2024): 49% and 67% fewer failed retrievals; full prompt below about 200,000 tokens.
- Sarthi et al., RAPTOR, ICLR 2024: +20% absolute accuracy on QuALITY with GPT-4.
- pgvector 0.8.0 release notes and AWS Database Blog: overfiltering with `ef_search = 40`; iterative index scans; exact search gives 100% recall.
- Virginia Tech, metadata-aware retrieval (arXiv 2601.11863): metadata prefixes require re-embedding the full index on update.
- Tool-DC (arXiv 2603.11495): tool-calling accuracy degrades as candidate tools grow, most for smaller models; similar tools confuse argument filling.
- MemReranker (arXiv 2605.06132): bge-reranker-v2-m3 241 ms vs GPT-4o-mini 1,549 ms average.
- MLflow documentation: OTLP ingestion since 3.6; RETRIEVER span type; retrieval judges; judge alignment (≥ 10 traces, 50–100 recommended); GEPA alignment optimizer; Prompt Registry (`register_prompt`, `load_prompt` by version or alias, `set_prompt_alias`).
- Nemhauser, Wolsey and Fisher (1978); Khuller, Moss and Naor (1999); Tang et al. (arXiv 2008.05391); arXiv 2611.13575; Feige (1998); Ailon, Charikar and Newman (2005).
- Ranganathan, faceted classification (Colon Classification, 1933).
