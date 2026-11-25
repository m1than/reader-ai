---
id: ADR-0020
title: Knowledge layers as append-only source packs, faceted tags, greedy retrieval with judges, the brain/v1 agent contract; rules for amending decided ADRs
status: proposed
date: 2026-11-25
verified_by: "RESULT.md of spikes retrieval-ablation, judge-gate and pack-links applying the decision rules in B10; SRV-INGEST property test (base packs and overlays) and SRV-KB property test (pair link runs), both over recorded model outputs; SRV-SEARCH and SRV-MCP MLflow evaluation meets the brain/v1 targets; trace-check green"
revisit_when: "Claude Haiku as caller stays below 90% of the strong model's retrieval sufficiency (point estimate) after two policy versions; find p95 reaches 2.5 s on the owner's Mac; more than 200 packs are active in one manifest; or brain/v1 needs a breaking change"
supersedes: []
amends: [ADR-0001, ADR-0005, ADR-0007, ADR-0008, ADR-0009, ADR-0015, ADR-0018, ADR-0019]
---

## Context

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

Model tags (F5) and the concept labels of B4 step 2 are proposals made before a selection, not verification, so decision 18 leaves them unchanged.

State on 2026-11-25: no code exists under `server/`; SRV-INGEST, SRV-SEARCH, SRV-KB and SRV-MCP are `todo` in `docs/progress.yaml`. Every change below costs text now and 0 lines of code.

This ADR is `proposed`. Nothing in it is in force, and no task that depends on it starts, until the owner accepts it. If the owner rejects it, the agent removes the ADR-0020 sentences from the roadmap, the pointer lines and the gated tasks in `docs/progress.yaml` in one PR, and SRV-INGEST and SRV-SEARCH return to their earlier `needs`.

## Part A — Rules for amending decided ADRs

| # | Rule |
| --- | --- |
| A1 | **Amend, never rewrite.** The Decision, Options and Rejected sections of ADR-0001 … ADR-0019 are not rewritten, whether their status line still says `proposed` or already says `accepted`. The roadmap records the owner's intent to accept them; the status flip is his commit, and the agent keeps working under them as it does today. A new or amending ADR is in force only once its status is `accepted` |
| A2 | **Change by delta.** A change to an earlier ADR is a new ADR with `amends: [...]` in its frontmatter and an Amends table: ADR, section, was, becomes, why. Whatever the table does not name stays in force. `supersedes` is used only when an ADR is replaced as a whole; the status flip stays the owner's (H3) |
| A3 | **One pointer line.** The amended ADR gets exactly one edit: a blockquote at the top of its Context, `> Amendment proposed by [ADR-NNNN](...) (date): <one line>. In force once ADR-NNNN is accepted; then, where the two disagree, ADR-NNNN wins.` After acceptance the owner may shorten it to the form ADR-0001 uses for ADR-0019 |
| A4 | **Same PR, every dependent artifact.** In the same PR:<br>• the pointer lines;<br>• one sentence in each affected roadmap row, prefixed `ADR-NNNN, once accepted:`;<br>• agent instructions (AGENTS.md) where a rule changes;<br>• `docs/progress.yaml` tasks for new spikes, gated on an owner task to accept the ADR.<br>An approved spec changes only with its tests (H2), and only in the stage that owns it |
| A5 | **Contracts in ADRs, mechanisms in config.** An ADR fixes interfaces, invariants and decision rules. Models, indexes, thresholds, prompts and judge versions are config or versioned policy, recorded on every trace; changing them needs no ADR. Changing a contract (a schema, the tool surface, an invariant) needs one. Numbers in an ADR that are not measured are initial policy values, marked as such |
| A6 | **Decide forks by a rule written first.** A fork with two viable options and a measurable difference becomes a spike whose decision rule is written in the ADR before the spike runs, in units the data set can resolve. The spike's RESULT.md applies the rule; a new ADR is needed only when no option passes or a revisit_when condition is met |
| A7 | **Measure at the last responsible moment.** A spike runs before the spec that consumes its numbers is approved, and no earlier than the data it needs exists, so no spike is redone because the design moved after it ran |
| A8 | **State the rework.** Each amending ADR states the files it changes now and the existing code that must change. The target for existing code is 0 |
| A9 | **A PR that adds or amends an ADR is merged by the owner**, even when it raises no status: it changes what later tasks are allowed to do (amends ADR-0019 "Merging pull requests") |

This ADR is the first one written under these rules.

## Part B — Knowledge layers

### B1. Layers and their depth

| Layer | Unit | Depth bound | Card budget (tokens) | Built by | Lives in |
| --- | --- | --- | --- | --- | --- |
| L0 Source | Source profile: role, nature, reliability, `as_of`, `license`, language, cost | 1 node per source | ≤ 200 | Ingest; owner confirms at G1 | Profile overlay |
| L1 Span | One Docling element: paragraph, table, code block, formula, test vector. Code and vectors are not split at ingest; a card larger than its budget is cut at the budget with a `next` link to the continuation, whose id is the locator sub-range that follows | 1 | 200–500 of text plus a context prefix of about 50–100 | Ingest | Base pack; prefix in an overlay |
| L2 Structure | Node of the source's own outline, with a summary | As in the source, capped at 4 levels | Leaf ≤ 100, chapter ≤ 300, root ≤ 500 | Ingest | Base pack; summaries in an overlay |
| L3 Concept | Canonical concept (`con:bip-32`) with ru/en aliases | Topic tree ≤ 3 levels; top level approved at G1 (ADR-0015) | ≤ 150, at most 10 related concepts shown | Registry | Registry; evidence per pack |
| L4 Finding | Atomic claim, ≤ 3 sentences, ≥ 1 citation (ADR-0015 kinds) | Flat, typed links | ≤ 120 | Pipeline, tutor, learner | Learner store |
| L5 Learner | State per concept: known, new, gap; recall state; calibration | Flat | Not returned as content; used to filter and boost | Sessions | Learner store |

Card budgets and depth caps are initial policy values (A5).

**Navigation invariant**, tested as a graph check: every active span is reachable from the ids `brain_map` returns by following `open` children and `next` links. The number of calls an agent actually needs is a measured metric, not an invariant (B9: median ≤ 3).

**Drafting a lesson.** The pipeline puts the whole source section in context instead of retrieving it when the section's measured token count fits a context budget in policy.
- A Bashir chapter averages about 39–41 pages: 890 or 935 pages over 23 chapters, depending on what is counted.
- Anthropic puts the point at which a full prompt stops being the simpler choice at roughly 200,000 tokens, about 500 English pages.
- The Russian token count per chapter is measured by `ingest-docling` and `compile-cost`, not assumed.

Retrieval serves cross-source checks, the tutor and MCP.

### B2. Source packs, overlays and the manifest

| Term | Meaning |
| --- | --- |
| **Base pack** | What parsing one material produces: spans (L1) and outline nodes (L2) with their ADR-0008 locators. Id = sha256(material sha256, parse config) |
| **Overlay** | A versioned set of derived rows keyed by a base pack id. Kinds: profile (L0), tags, vectors per embedding model, context prefixes per prompt version, L2 summaries, concept evidence, pair link runs. Overlay version = sha256(inputs, builder version, prompt and model versions) |
| **Manifest** | Append-only log of brain versions. One entry = the active base packs and, for each, the active overlay versions |

| # | Invariant |
| --- | --- |
| P1 | Base packs and overlays are immutable once sealed. A build writes only rows carrying its own id and never updates another pack's or overlay's rows. A build stays `building` until sealed; only sealed builds enter a manifest |
| P2 | **Span identity is the ADR-0008 locator:** material sha256, page or file+lines, character offsets. Outline ids are (material sha256, outline path).<br>• A tagger, model, prompt or vocabulary change creates a new overlay, never a new base pack, so citations survive it.<br>• A parse-config or Docling upgrade does create a new base pack, and offsets may shift. Each citation therefore also stores a hash of its quoted text, and a re-anchoring job maps old locators to new spans by that hash. Citations it cannot map become `orphaned` (P8) |
| P3 | Changing a source's reliability or role is a new profile overlay version; nothing is re-indexed (F2) |
| P4 | `brain_version` = sha256 of the manifest entry: active base pack ids, their active overlay versions, and the registry view version.<br>• Adding, removing or rolling back a source, or switching an overlay, is one new manifest entry.<br>• Activating a pack, together with all link runs computed for it, is one entry, not one per run.<br>• Appends use compare-and-swap on the parent entry |
| P5 | Every read goes through views restricted to the active manifest.<br>• A link is visible only when both of its ends are active.<br>• The concept registry is read through the same restriction: concepts and aliases whose only evidence is in inactive packs, and that the owner has not approved, are invisible |
| P6 | **Garbage collection** is background work, never on a request path. It deletes sealed packs and overlays that are:<br>• absent from the last N manifest entries,<br>• older than an age floor,<br>• and not cited by a published lesson revision or a finding.<br>N and the floor are in config. The owner can purge a pack at once (ADR-0017 data ownership); citations then keep their locator and show the source as removed. Partition per pack (`DETACH PARTITION CONCURRENTLY`, PostgreSQL 14+) comes only if GC deletion is measured to hurt |
| P7 | **Derived data** (concept cards, cross-source summaries, gap reports, the "book turned around" view) is a cache keyed by (node, sorted contributing pack and overlay versions, builder version). It is never edited in place. A changed pack set misses the cache and rebuilds in the background; until then `find` serves the cards whose key is still fully active and falls back to lower layers |
| P8 | Findings and learner state are not in packs. When every span a finding cites is inactive or unmapped, the finding gets the derived state `orphaned` and enters the review queue; nothing is deleted. Published lesson revisions stay immutable; inactive citations are shown as such |
| P9 | Removal costs one manifest entry on the write path. Its read-side cost is cache misses (P7), which rebuild in the background |
| P10 | Scope: one manifest per user. A shared course gives its members role-filtered read access to the course owner's packs (ADR-0016). A read that spans a member's own manifest and a course records both manifest versions |

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
| F1 | One facet, one principle of division (Ranganathan). The sector tree of ADR-0015 becomes the `topic` facet; other dimensions are separate facets, combined at query time. Sectors stay a tree, so the SectorMap of ADR-0013 and export by sector path in ADR-0017 are unchanged |
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

Chains are branches of `topic` (for example `bitcoin/keys`, `ethereum/transactions`), within its depth cap of 3 levels. Exact questions that name a chain filter on that branch.

Proposed facet, enabled only when its consumer lands: `knowledge_type` (fact, mechanism, procedure), for recall cards versus explain cells, if the owner adopts the explain-back proposal.

### B4. Linking a pair of packs

Runs for each new pair {P, Q} of active base packs that share a concept (B2). Each step is cheaper than the next and limits its work.

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

**Objective of the card-selection rows.** It is weighted coverage of the query's terms and facets, which is monotone submodular. In stage 3 the terms are identifiers and full-text terms from L1 and L2. Concepts (L3) join from stage 7. Source priorities enter only as non-negative element weights. Superseded cards and license limits enter only as filters applied before selection. Either way the bounds keep holding. Lexicographic re-ranking after selection is not used. On the judged path of B6, the judge's drops are a third filter between two greedy selections. Each selection keeps its bound over its own input set, but that path has no end-to-end bound against the full candidate pool; the gate and degradation paths keep the bound over their candidates. The judge's order sets only the order in which the selected cards are returned.

### B6. Retrieval cascade for `find`

A route = f(channel, question type). Routes, budgets and thresholds are declared as data and versioned in `policy_version` (B7a).

No step before the judge makes an LLM call (step 2 embeds the query with the local embedding model). The judge runs last and reads only the cards already selected (owner decision 18).

| # | Step | Budget |
| --- | --- | --- |
| 1 | Route by rules: channel (MCP, tutor, pipeline) and question type (identifiers found, wording) | 0 model calls |
| 2 | Candidates: manifest, role and license filters first, then full-text rank (`tsvector`) and dense vectors, merged by reciprocal rank fusion | ≤ 50 candidates. Exact distance on the filtered set while the vector step is fast enough. With HNSW at the default `ef_search = 40`, a filter that keeps 10% of rows leaves 40 × 10% ≈ 4 results by arithmetic (pgvector documents this overfiltering). Exact search keeps 100% recall |
| 3 | Merge duplicates by `same_point` (B5 pivot) | One card, several citations |
| 4 | Gate: if the fused scores separate clearly (margin rule in policy), go to step 7 | 0 model calls |
| 5 | Otherwise expand 1 hop over `next` and concept links, keep the top 50 of the expanded set by fused score, and select from them under 1.5 × the response budget (B5 budgeted greedy) | 0 model calls. The cap of 50 and the factor 1.5 are initial policy values |
| 6 | One `judge.find` call on the step-5 selection only: relevance order, sufficiency, and which cards to drop | Exactly 0 or 1 judge call per `find`; judge input ≤ 1.5 × the response budget (3,000 tokens at the initial 2,000 maximum) |
| 7 | Select under the response budget (B5 budgeted greedy): from the cards the judge kept when step 6 ran, otherwise from the merged candidates (step 3, or the capped expanded set of step 5). Cards are returned in the judge's order when it ran, otherwise in fused order | Within the response budget (B8) |

**`judged`.** Cards carry `judged: true` only when step 6 ran. When the gate skipped the judge or step 6 degraded, they carry `judged: false`, and the trace records which of the two happened.

**Degradation.** Step 6 is skipped on judge timeout or error, on an exhausted budget, or for a caller without a provider. Step 7 then selects from the capped expanded set of step 5.

**Why the judge comes last.** The judge used to read the top 20 candidates before the greedy selection. By the B1 budgets an L1 card is 250–600 tokens (200–500 of text plus a prefix of 50–100), and stage 3 runs on L1 and L2. A 2,000-token response then holds 3–8 cards, so 60–85% of the 5,000–12,000 tokens the judge read were cards that were never returned. Judging the 1.5× selection reads at most 3,000 tokens (5–12 cards), every one of them a card the response can contain, and the margin of 0.5 lets the judge drop cards without leaving the response short. The price is stated in B5: the judged path has no end-to-end bound against the full candidate pool. `judge-gate` compares this order with the earlier one (B10).

Whether a single gated judge call fits p95 < 2.5 s is a hypothesis owned by `judge-gate`.
- A cheap-model call costs on the order of 1.5 s in published measurements of other models: GPT-4o-mini averaged 1,549 ms per reranking call in one study.
- Haiku through the Agent SDK, which starts the Claude Code CLI as a child process, is not measured yet.

Initial route table (question type × channel modifiers):

| Question type | Layers | Filter |
| --- | --- | --- |
| Exact ("hardened index offset in BIP-32") | L3 → L1 | `role: normative`, identifier from step 1 |
| Thematic | L2 → L3 | None on form |
| Current state | L1, L4 | Newer `as_of`, `role: time_axis`, not superseded |
| Fact-check (ADR-0009 stage 4) | L1 only | Span required; reliability above a threshold in policy |

| Channel | Modifier |
| --- | --- |
| MCP | Caller's course roles; copyright limit on every card (B8); L4 and L1 preferred |
| Tutor | L5 boost: the learner's gaps first |
| Pipeline | No L5; reliability threshold from policy |

Layers L3–L5 come online with their specs (stages 5–7). Until then the routes run on L1 and L2, and the evaluation reports which layers were available.

### B7. Judges, credentials, cost and labels

Judges are **slots**: model task classes in `reader.yaml` (ADR-0005; ADR-0018 "model per task as data"). Our own code calls each judge through the Agent SDK and logs the result as an MLflow assessment. MLflow's built-in judges are used only if they accept such a model adapter; the `trace-backend` spike checks this.

| Slot | Decides | Runs | Paid by | v1 binding |
| --- | --- | --- | --- | --- |
| `judge.link_type` | Link type between two spans (B4 step 5) | Offline, per pair run | Pack owner; `index` line of the ledger | Claude Haiku |
| `ingest.context_prefix` | The context prefix of a span (B1) | Offline, per pack | Pack owner; `index` line of the ledger | Claude Haiku |
| `judge.find` | Relevance order, sufficiency and cards to drop, over the cards already selected (≤ 1.5 × the response budget), in one call | Online, gated (B6 steps 4 and 6) | The caller, on the caller's own provider (ADR-0005 invariant) | Claude Haiku |
| `judge.eval.*` | Retrieval relevance, sufficiency and groundedness, plus custom judges (card is self-contained; citation supports claim) | Offline, on the evaluation set and sampled traces | Owner; evaluation cap | A strong Claude model |

Rules:

- **Replacing Claude in a slot** with a local model is a config change. It is allowed only when the candidate matches or beats the current binding on the evaluation set (ADR-0018 tuning rule). This is how Claude is replaced in specific places without changing the method.
- **Caps.** Offline jobs have a USD cap per pack (prefixes) and per pair run (link judging). The initial values are the costs measured in B10 plus 20% (an initial value). Reaching 100% stops the job with an event (ADR-0005); the agent then blocks the task with a top-up ask to the owner (owner decision 15). Unjudged candidates stay unlinked, unprefixed spans keep their titles-only context, and the pack stays usable. The evaluation cap is set from the measured cost of one evaluation run (`judge-gate`).
- **Judge outputs** are an append-only overlay keyed by (input hash, judge@version). A new judge version sits next to the old one, so the two can be compared on the same inputs. Replays read the recorded outputs.
- **Prompt injection.** Judge prompts treat card and source text as data. Instructions found in material or findings are never followed. Unreviewed findings (`proposed`, including those written by external agents with `kb:write`) are excluded from `find`, from judge input and from `next`.
- **Ranking priors.** A card's prior weight in ranking comes only from the owner's labels and the evaluation set, never from live judge scores. Otherwise the judge would reinforce its own choices.
- **Tuning data.** Claude outputs never become tuning data (ADR-0018). Judges may use Claude for evaluation, which ADR-0018 allows.

Labels, the owner's time used as efficiently as possible:

1. A binary label per trace and judge, with an optional one-line reason, stored as an MLflow human assessment under the judge's name.
2. Sampling order: traces where two judges disagree; then judge scores within ±0.1 of the gate threshold; then the owner's thumbs-down in tutor sessions. Stratified across routes and packs.
3. Batches of 25 traces, about 10–12 minutes at 20–30 s each. The first target is 50–100 labels per judge: MLflow judge alignment needs at least 10 and recommends 50–100.
5. Alignment (SIMBA or GEPA in MLflow) runs once a judge has 50 labels, and again after each further 50. It rewrites judge instructions and trains no model weights (owner decision 14). An aligned judge is a new prompt file version and follows the no-regression rule of B7a.
4. Owner labels are owner data and may later feed local-model tuning (ADR-0018 excludes only Claude outputs).

### B7a. Prompts and policy versions

- **Source of truth.** Every prompt the system sends (judges, context prefix, link type, card text) is a template file in the repository. It is reviewed like code, and its file hash is the provenance (ADR-0018 run records).
- **MLflow registry.** Each file version is registered in the MLflow Prompt Registry, tagged with its commit, for lineage and comparison in the UI. Code loads prompts from the repository, not from MLflow, so MLflow stays an optional Compose profile at runtime (ADR-0018). The registry is never edited and has no runtime alias. MLflow is the dashboard where the owner compares prompt versions, traces, evaluation runs and labels (owner decision 16).
- **No-regression rule.** A prompt change merges only after the agent has run the evaluation job against the current version, with live model calls inside the evaluation cap, outside CI. The run must show no regression (B10 statistics), and the Haiku targets must hold. The result is recorded in MLflow and in the PR description. Reverting the commit is the rollback.
- **`policy_version`** = sha256 of the declared data only: the route table, thresholds, k and the RRF constant, the facets file, slot-to-model bindings, embedding model ids and prompt file hashes. The code version is a separate trace attribute, so an unrelated commit does not change `policy_version`.
- **Rules that need no model** (routes, facet filters, identifier regexes, budgets) stay data files: 0 calls, deterministic.

### B8. Agent contract `brain/v1`

The interface every agent sees. Tools and models behind it change freely (A5).

| Tool | Scope | Returns |
| --- | --- | --- |
| `brain_map()` (also a resource) | `kb:read` | Top of the brain within 1,500 tokens: sectors with counts, sources with roles, `brain_version`, how to search. Meant to be read once per agent session |
| `find(query, filters?, k?, budget?)` | `kb:read` | Cards chosen by B6 within `budget` tokens (default and maximum in policy; initial maximum 2,000; a larger `budget` is clamped to the maximum) |
| `open(id, depth?, budget?)` | `kb:read` | Any typed id: `src:`, `sec:`, `con:`, `fnd:`, `spn:`, `les:` (lesson outline, replacing `get_lesson_outline`); `depth ≤ 4`, same budget rule as `find` |
| `propose(...)` | `kb:write` | A proposed finding in the owner's review queue (unchanged from ADR-0015) |

Invariants of every `brain/v1` tool:

1. Results are filtered by the caller's course roles (ADR-0015, ADR-0016).
2. For `license: personal-use`, no card carries more than 300 characters of source text per citation (ADR-0015 limit; ADR-0008 Decision 4). L1 cards of such material return the quote within the limit plus the locator.
3. Every response respects its token budget.
4. Unreviewed findings are never returned.

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
- **Reproducibility.** Scope: layers L1–L3 on the MCP and pipeline channels. There, the same `brain_version`, `policy_version`, caller roles and request give the same response on the judge-free path, and also with a judge when the recorded judge output for that input is replayed. Findings, learner state and label priors change outside the manifest, so tutor responses and L4 results are traced but not promised to replay. Responses may be cached only under a key that includes the caller's roles.
- **Versioning.** Adding an optional field is allowed within `brain/v1`. Removing or renaming a field or a tool is `brain/v2`, served next to v1 until traces show no v1 caller.

### B9. Tracing and evaluation in MLflow

MLflow is the trace and evaluation backend (owner, 2026-11-25). MLflow 3.6 and later accepts OTLP/HTTP at `/v1/traces`, so Rotel (SRV-OBS) exports to it without changes. MLflow runs as the optional `observability` Compose profile; the request path never depends on it.

Span tree of one MCP call:

- `TOOL` root with the caller (agent and model), `brain_version`, `policy_version`, the code version and the tool.
- `ROUTER` (custom span type) with the route and filters.
- `RETRIEVER` per layer used, returning Documents with `doc_uri` = `reader://<id>`. `page_content` carries card text only for `license: shareable` material and a content hash for `personal-use`, matching ADR-0018 ("content capture always off for `personal-use`").
- The selection under 1.5 × the budget, then `judge.find` on it, when the gate does not skip the judge; then the final selection with the token count.

Pack builds and link runs are traced too: counts per B4 step (spans, mentions, pairs after blocking, judge calls, cost).

Evaluation:

- **Data set.** 30 questions per question type that the active sources can answer. The agent writes them and the owner reviews a sample of 30. Expected facts are written in the owner's or agent's own words, ≤ 300 characters each, never copied source text.
- **Judging `personal-use` material.** Judges run in a local evaluation job that reads card text from our store in memory; only scores are written to MLflow.
- **Matrix:** caller model (Claude Haiku, a strong Claude model) × `brain_version` × `policy_version`.
- **Metrics:** sufficiency, calls per answer, tokens per response and per answer (counting `brain_map` once per session), p95 latency.
- **Schedule.** LLM judges run on the data set on a schedule and on sampled traces. Live requests record only deterministic metrics: calls, tokens, empty results, latency.
- **Pack value.** The evaluation runs before and after a pack is activated, and the paired difference is the pack's measured contribution. A pack that loses significantly (B10 statistics) is deactivated with one manifest entry.

Targets (owner, 2026-11-25), each reported as a point estimate with its 95% interval:

| Target | Measured as |
| --- | --- |
| Haiku ≥ 90% of the strong model's sufficiency | Point estimate |
| Median ≤ 3 calls per answer | Median over the data set |
| ≤ 2,000 tokens per answer | `find` and `open` responses of one answer, summed; `brain_map` counted once per session (owner decision 17) |
| `find` p95 < 2.5 s | p95 over the data set |

### B10. Spikes and their decision rules

**Statistics for every rule.** Paired comparison on the same questions, with an exact McNemar test on the discordant questions. A difference is stated in whole questions per question type (30 questions per type). "Significant" means two-sided p < 0.05. At this size that needs a net gain of about 6 questions (6–0 gives p = 0.031; 5–0 gives 0.0625), so smaller gains are deliberately dropped: the system keeps only steps whose value the data can show.

| Spike | Runs | Question | Measure | Decision rule (A6) |
| --- | --- | --- | --- | --- |
| `retrieval-ablation` | Stage 3, after the evaluation set is written. Uses `ingest-docling` output; runs before SRV-INGEST and SRV-SEARCH are approved; extends `embeddings-local` | Which retrieval steps earn their place on one source | On exact, thematic and fact-check questions: recall@10 and sufficiency for L1; plus context prefix; plus L2 summaries; plus the `form` filter. Exact scan vs HNSW p95 at the measured size. Tokens and USD cost per pack of context prefixes and L2 summaries | Keep a step if, on its question type, its gain is significant (in practice a net ≥ 6 of 30 questions). Its measured cost per pack sets the initial cap (B7). Exact scan while the vector step p95 ≤ 150 ms (initial value) |
| `judge-gate` | Stage 3, after `retrieval-ablation` | Whether and when the online judge runs | Haiku `judge.find` p95 through the Agent SDK path. Gate fire rate and sufficiency for: fused order only; judge on every query; judge gated at several margins. Every judged variant places the judge after the selection (B6). As a reference, the earlier order (judge on the top 20 before the selection) runs once at the best margin. USD cost of one full evaluation run | Pick the cheapest variant that is not significantly worse than the best, is not worse by more than a net 2 of 30 questions on any type, and has `find` p95 < 2.5 s. If the judge-after-selection order is significantly worse than the reference, or worse by more than a net 2 of 30 on any type, the agent blocks the task for the owner with both measurements, because the order is an owner decision (18). The reference is not a candidate in the pick rule. The factor stays 1.5 in this spike, so the block also says that a loss may come from a factor that is too small; changing the factor is part of the owner's answer. The evaluation cap is set from the measured run cost |
| `pack-links` | Stage 7, before SRV-KB, on Bashir plus two openly licensed sources (BIP texts, Bitcoin Optech topics) | Variant A or B for storing pair link runs (B2); the facet-filter (`role`, `reliability`, `as_of`) and current-state rules, which need several sources | Property test over recorded model outputs: after random add, remove and rebuild sequences, the active link set equals a fresh build of the same pack set. Rows, lines of code and overlays rebuilt when one pack is rebuilt. The facet-filter and current-state measures use the `retrieval-ablation` method | Both variants must pass the property test. Pick the variant with fewer lines of code, unless the other rebuilds ≥ 30% fewer overlays on a pack rebuild (initial value). Facet filters use the `retrieval-ablation` rule |

**`trace-backend` (stage 0), once this ADR is accepted,** no longer chooses the backend. It checks MLflow on the owner's Mac:
- memory at idle and under one compile;
- nesting of Agent SDK spans under our stage span;
- a score attached to a trace;
- whether MLflow judges accept an Agent SDK model adapter.

If MLflow fails these checks, Phoenix covers traces only, and the Prompt Registry, assessments and evaluation need a new ADR.

### B11. Which spec owns what

| Piece | Spec | Stage |
| --- | --- | --- |
| Base packs, overlays (profile, tags, vectors, prefixes, L2 summaries), manifest, GC, re-anchoring, property test for packs and overlays | SRV-INGEST | 3 |
| `find` cascade, routes, policy, judge slot `judge.find`, evaluation set | SRV-SEARCH | 3 (L1–L2), extended as L3–L5 land; SRV-AGENT (stage 5) is built before it for the `Provider` port (owner, 2026-11-26) |
| Traces to MLflow, prompt registration | SRV-OBS, SRV-AGENT | 2 and 5 |
| Concept registry, linking (B4), pair link runs, property test for links, findings validity and `orphaned` | SRV-KB | 7 |
| `brain/v1` tools and invariants | SRV-MCP | 7 |
| Export of the registry and link runs | SRV-EXPORT | 7 |
| L5 boost in the tutor route | SRV-TUTOR | 6 |

## Amends

| ADR | Section | Was | Becomes | Why |
| --- | --- | --- | --- | --- |
| ADR-0001 | Process | No rule for changing an ADR; template has `supersedes` only | Part A rules A1–A9 (A9 also amends ADR-0019, row below); the template gains `amends: []` | Changes by delta keep decided ADRs readable and keep rework visible |
| ADR-0005 | Budgets and cost ledger | $200 credit split as compile ≤ $100 and tutor, per task class | Pack overlays (context prefixes, L2 summaries, link judging) are recorded on their own `index` line of the ledger, with per-pack and per-pair-run caps set from the costs `retrieval-ablation` measures; when a cap is reached the owner tops the credit up. `judge.find` runs on the caller's provider and counts against the caller's tutor or MCP use. `judge.eval.*` has an evaluation cap set from the run cost measured by `judge-gate` | 23 lessons × $4.35 = $100.05 already fills the compile share; new model work needs its own owner-set money and a cap. The ADR-0005 invariant forbids borrowing credentials |
| ADR-0007 | Decision, retrieval | Hybrid full-text plus pgvector HNSW, merged by RRF | Same fusion; exact distance on the filtered set until `retrieval-ablation` shows the vector step p95 > 150 ms; data organised as base packs and overlays behind a manifest (B2) | HNSW with filters loses most results at default settings; exact search has 100% recall |
| ADR-0008 | Decision 3, spans | Every chunk keeps its span | A span is one Docling element of a base pack and keeps the ADR-0008 locator as its identity, plus a quote hash for re-anchoring. Context prefixes and inherited tags are overlays (B2). Decision 4 (`license`, `personal-use` never exposed) is unchanged and becomes the `license` facet | Contextual Retrieval reduced failed retrievals by 49%, and by 67% with reranking (Anthropic, 2024). A locator identity keeps citations stable across overlay changes |
| ADR-0009 | Stage 1 output; drafting; stage 4 fact-check; order of the checks after drafting | Outline, chunks with spans, embeddings; claims cite spans; fact-check → lab verification → adversarial review | Stage 1 outputs a base pack and its overlays. Drafting puts the whole source section in context when its measured token count fits the policy budget. Fact-check uses the fact-check route (B6). After drafting, the checks run cheapest first and the first failure loops back to the draft: schema and citation presence (rules, 0 model calls) → lab verification (stage 5: sandbox runs; any model work is in building its independent reference path) → claim support (stage 4, with a model) → adversarial review (stage 6, last). The limit of 3 loops is unchanged, and every return to the draft counts as one loop, a rule failure included | A chapter of about 40 pages likely fits in context; the Russian token count is measured first. The adversarial review, a fresh-context agent pass, then reads only a draft that has passed every cheaper check, so no review is spent on a draft that a cheaper check would send back (owner decision 18) |
| ADR-0015 | Knowledge model; tool table | One sector hierarchy; six tools | Sectors become the `topic` facet and stay a tree. A concept registry (L3) is added. Findings gain validity (`valid_from`, `superseded_by`) and the derived state `orphaned`. The tool table is replaced by `brain/v1` (B8), which keeps role filtering, the 300-character limit and the review queue as invariants | Small models are more sensitive to similar tools. One card shape and `next` links are the hypothesis that cuts calls per answer, measured in B9 |
| ADR-0018 | Trace backend; v1/v2 split | Backend chosen by a stage 0 spike; evaluation harness in v2 | MLflow (owner, 2026-11-25), still an optional profile. A retrieval-only evaluation (B9) and the prompt no-regression rule (B7a) move into v1. The general regression gate for every prompt and model change stays in v2 | The owner wants the RAG traced and evaluated from the start; retrieval evaluation is the minimum that makes the Haiku target checkable |
| ADR-0019 | Merging pull requests | The agent merges when CI is green and the PR does not change an ADR status | Also excepted: a PR that adds or amends an ADR (A9); it is merged by the owner. AGENTS.md loop step 6 says the same | An ADR changes what later tasks may do; merging it is a decision, as a status flip is |
| ADR-0019 | What still stops a task (not the whole run) | Five conditions | Also blocks: `deep-reviewer` has not returned `VERDICT: approve` after two rounds on the same spec (owner, 2026-11-26). AGENTS.md "What blocks a task" says the same | Without a round limit the agent can loop on reviews without end |

Rework (A8):

| Changed in this PR | Count |
| --- | --- |
| New ADR | 1 |
| ADR template (`amends:` field) | 1 |
| Pointer lines in earlier ADRs | 8 |
| Roadmap rows or lines | 13 |
| AGENTS.md lines (A9 merge rule; stop after two `deep-reviewer` rounds; `docs/plans/` in the map) | 3 |
| `docs/architecture.md` lines | 2 |
| Execution plan `docs/plans/adr-0020-execution.md` | 1 |
| `docs/progress.yaml` tasks added (1 owner task, the evaluation set, 3 spikes) | 5 |
| `docs/progress.yaml` tasks edited | 7 |
| Existing code | 0 lines |

## Decision

- Knowledge is built from immutable base packs and versioned overlays, combined by an append-only manifest; removing a source costs one manifest entry.
- Tags are facets declared as data.
- Every selection over the graph or the embeddings is greedy with stated bounds.
- `find` is a cascade with at most one call to the online judge slot, made last and only on the cards already selected.
- Agents use the stable `brain/v1` contract.
- Everything is traced and evaluated in MLflow, with prompts as reviewed files registered there.
- Amendments to earlier ADRs follow Part A.

## Options

| Option | Gives | Costs (numbers) |
| --- | --- | --- |
| **Base packs, overlays, manifest, faceted tags, greedy cascade, brain/v1** | Adding or removing a source is one manifest entry; L1–L3 answers reproducible on the judge-free path and by replay; one interface for any model | View layer, manifest table, overlay bookkeeping, pair link runs |
| One mutable index, updated in place | Fewer concepts | Removing a source deletes its rows plus every link to it across the whole link table and stale mixed summaries; no rollback; no version to reproduce an answer |
| Pack id over everything (parse, tagger, models) | One id per build | Every tagger, prompt or model change creates new span ids, so every citation goes inactive and every finding becomes `orphaned` |
| Global GraphRAG communities | Strong thematic answers | Recomputing communities over the whole graph on every new source costs in proportion to the corpus, not to the new pack |
| LLM judge on every candidate | Best ranking per query | About 50 calls per `find`; at about 1.5 s per call in published measurements of other cheap models, far beyond 2.5 s |
| One judge call on the top 20 before the selection (first draft) | The judge can lift a candidate the greedy would not pick | 60–85% of the judge's input is cards the response never returns (B6) |

## Rejected options — why (numbers)

- **Mutable index.** The cost of removing a source grows with the whole link table, against one manifest entry. There is no `brain_version` to reproduce an answer.
- **Pack id over everything.** A single prompt edit would orphan 100% of the findings that cite that source.
- **Global GraphRAG communities.** Work per added source is proportional to the corpus. Pair linking with concept blocking is proportional to the two packs: about 15,000 pairs instead of 4 million in the B4 example.
- **LLM judge on every candidate.** About 50 calls per query against a budget that allows at most one call (B6).
- **Judge before the selection.** At the B1 card sizes, 20 judged cards against 3–8 returned: 60–85% of the judge's input per call is spent on cards that are dropped anyway. `judge-gate` measures whether lifting such cards is worth it; if it is, the owner decides (B10).
- **One raw `search` tool.** A cheap model would stitch context itself, spending calls and tokens. Self-contained cards with `next` remove that work (hypothesis, measured in B9).
- **One tool per layer.** Five or six similar tools are the case where tool-selection accuracy drops, most for small models.
- **Exact optimisation for selection.** Budgeted max coverage, set cover and correlation clustering are NP-hard. The greedy algorithms of B5 carry proven bounds at linear or near-linear work.
- **All tags embedded in text.** Every owner decision about a source's role or reliability would re-embed the whole index.
- **Decision thresholds of 0.05 or 0.02 on 30 questions.** One question is 3.3% of the set and about 14% of a 7-question type. The standard error at p = 0.8, n = 30 is about 0.073, so such thresholds sit below the resolution of the data.
- **A lower 95% bound for the 90% target.** At 90 questions (30 per type in stage 3) that bound would demand a point ratio near 0.98, far stricter than the owner's 90%.

## Consequences

- **Easier:**
  - adding a book is an increment with a measured value;
  - removing it is instant and reversible;
  - agents on cheap models get one stable interface;
  - a local model can take over a judge slot by passing the same evaluation.
- **Harder:**
  - every read goes through manifest views;
  - overlays and derived data need version keys;
  - each facet needs a consumer;
  - the evaluation set must be written before stage 3 decisions.
- **Forbidden:**
  - updating rows of another pack or overlay;
  - deleting on a request path;
  - enumerating subsets over graphs or embeddings;
  - free-form tags;
  - live judge scores as ranking priors;
  - Claude outputs as tuning data;
  - unreviewed findings in agent results;
  - MLflow on the request path.

## Open questions

None. The seven questions of the first draft were answered by the owner on 2026-11-25 (Context, decisions 11–17). One fact stays to measure: with one span per Docling element, the pilot may need about 9,000–10,000 context-prefix calls (an assumed 10 elements per page); `retrieval-ablation` measures the real count and cost, and the compile share (23 × $4.35 = $100.05) is already full, so indexing is a separate ledger line.

## Sources

- **Retrieval:**
  - Anthropic, Introducing Contextual Retrieval (2024): 49% and 67% fewer failed retrievals; a full prompt below about 200,000 tokens.
  - Sarthi et al., RAPTOR, ICLR 2024: +20% absolute accuracy on QuALITY with GPT-4.
  - pgvector 0.8.0 release notes and AWS Database Blog: overfiltering with `ef_search = 40`; iterative index scans; exact search gives 100% recall.
  - Virginia Tech, metadata-aware retrieval (arXiv 2601.11863): metadata prefixes require re-embedding the full index on update.
- **Tools and latency:**
  - Tool-DC (arXiv 2603.11495): tool-calling accuracy degrades as candidate tools grow, most for smaller models; similar tools confuse argument filling.
  - MemReranker (arXiv 2605.06132): bge-reranker-v2-m3 241 ms vs GPT-4o-mini 1,549 ms average.
  - docs/research/agent-tracing-2026-11.md: the Agent SDK runs the Claude Code CLI as a child process.
- **MLflow documentation:** OTLP ingestion since 3.6; RETRIEVER span type; retrieval judges; judge alignment (at least 10 traces, 50–100 recommended); GEPA alignment optimizer; Prompt Registry.
- **Approximation bounds:**
  - Nemhauser, Wolsey and Fisher (1978);
  - Khuller, Moss and Naor (1999);
  - Tang et al. (arXiv 2008.05391);
  - Greedy+Singleton ratio (arXiv 2611.13575, 2026 preprint, not peer-reviewed);
  - Feige (1998);
  - Ailon, Charikar and Newman (2005).
- **Classification:** Ranganathan, faceted classification (Colon Classification, 1933).
