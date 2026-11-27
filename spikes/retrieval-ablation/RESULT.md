# Spike retrieval-ablation — free arms measured, paid arms projected (2026-11-26)

**Question (ADR-0039 B10; docs/plans/adr-0039-execution.md V.spike.retrieval-ablation):** which retrieval steps earn
their place on one source. This run covers the arms that need no model call: V0 spans against today's chunks, the
`form` filter, RRF hybrid on spans, exact vs HNSW vector-step latency, the span re-embedding wall time. Context
prefixes (V1), L2 summaries (V2), judge-scored sufficiency and the fact-check route against the whole-section
baseline need model calls; they are **projected only** (below) and wait for the owner's spend approval.
**Model calls made: 0. USD spent: 0.**

## Method

- **Spans.** The pilot PDF (sha256 `61e8d4c3…c9e2b2`) re-converted on the host with the stack's own pinned
  converter (`reader.ingest.host.convert`, docling 2.151.1+r2, the `+r2` converter venv and models of `reader-ingest
  --setup`) into the gitignored `data/spikes/retrieval-ablation/convert/out`. Same artifact as the stack's
  `material_spans`: 9,463 spans, 494 outline entries (bookmarks), code line 360/10/7/0, `spans_sha256 8b1b3db9…`
  (V.live recorded 9,463 spans, 494 outline entries and the same code line). 702 s wall, peak RSS 4.36 GB. The
  owner's stack and database were not touched.
- **Variants** (recall@10: a question hits when any of the top 10 results' pages is a gold page):
  - `C_chunks`: today's reference: `search/chunking.py` `chunk_spans` (loaded from the repository file) over every
    span, 1,200/200/80 characters → 2,247 chunks; dense top 10.
  - `V0_spans`: one Docling element per span (B1); the 359 spans with empty text (figures without caption,
    undecoded formulas) cannot be embedded and are not indexed → 9,104 spans; dense top 10.
  - `V3_form`: `form` by rule from span kind (B3): text and list_item → prose, code → code or test_vector (≥ 32
    non-space characters, ≥ 60% hex digits), formula, table, figure; a heading is structure (L2) and has no form.
    Route filter from the B6 table: exact and fact-check keep {prose, code, test_vector, formula, table};
    thematic has no form filter. Declared in `analyze.py` before any result was computed.
  - `H_w5` / `H_w1`: RRF over dense top 50 and the full-text list, exactly as SRV-SEARCH builds it (OR of the
    query's russian lexemes, `ts_rank_cd`, top 50; `search/fusion.py` rrf, k = 60) with w_dense 5 (today's
    `params.W_DENSE`, primary) and 1 (equal weight). `V3H_*`: the same after the form filter.
  - `V0_spans_eqchars` (informative, not a B10 variant): spans in dense order until they hold as many characters
    as that question's top 10 chunks.
- **Embedding.** The embed service's own image (`reader-embed:local`) and encoder class (`SentenceEncoder`,
  BAAI/bge-m3@5617a9f6, CPU fp32, max_seq_length 1,024, normalised), one `encode` per batch of 8 in seq order
  as the `search.embed` job sends them; a new container with `--network none`, Docker VM 13 CPUs, 13 torch threads.
- **Latency.** A throwaway `pgvector/pgvector` container (pgvector 0.8.7, Postgres 18.7, digest pinned in
  `run.sh`, `--network none`, cluster on tmpfs, default `shared_buffers` as in the stack): the vector step alone,
  `SELECT seq FROM vec ORDER BY embedding <=> q LIMIT 50` (the candidate count), timed inside plpgsql, 90 queries ×
  5 repetitions after one warm-up pass. HNSW with the defaults (m 16, ef_construction 64); the plan files confirm a
  sequential scan for exact and an index scan for HNSW.
- **Statistics.** Exact two-sided McNemar on the discordant questions, stdlib (`math.comb`), per question type
  (30 each), as B10 states.
- **Copyright.** Book text exists only in memory and in the gitignored `data/`. `results/*.json` hold ids, page
  numbers, counts and numbers. Run: `bash spikes/retrieval-ablation/run.sh data/spikes/retrieval-ablation/convert/out`.

## Results: recall@10, hits of 30 per type (`results/recall.json`)

| Variant | exact | thematic | fact-check | all (90) | mean chars in top 10 |
| --- | --- | --- | --- | --- | --- |
| C_chunks (today) | 27 | 20 | 29 | 76 | 10,454 |
| V0_spans | 25 | 17 | 29 | 71 | 2,750 |
| V3_form | 25 | 17 | 30 | 72 | 3,509 |
| H_w5 (spans + RRF) | 24 | 16 | 29 | 69 | 4,196 |
| H_w1 | 24 | 17 | 28 | 69 | 9,620 |
| V3H_w5 | 23 | 16 | 30 | 69 | 4,744 |
| V3H_w1 | 23 | 17 | 28 | 68 | 9,884 |
| full text alone | 17 | 5 | 18 | 40 | 15,971 |
| V0_spans_eqchars (informative) | 30 | 23 | 30 | 83 | 11,705 |

No full-text list was empty (0 of 90).

## McNemar between neighbouring variants (`results/mcnemar.json`)

Gains / losses are discordant questions (new hit, base missed / base hit, new missed).

| Base → new | exact: g/l, p | thematic: g/l, p | fact-check: g/l, p |
| --- | --- | --- | --- |
| C_chunks → V0_spans | 1/3, 0.625 | 1/4, 0.375 | 1/1, 1.0 |
| V0_spans → V3_form | 0/0, 1.0 | 0/0, 1.0 | 1/0, 1.0 |
| V0_spans → H_w5 | 0/1, 1.0 | 0/1, 1.0 | 1/1, 1.0 |
| V0_spans → H_w1 | 0/1, 1.0 | 2/2, 1.0 | 1/2, 1.0 |
| V3_form → V3H_w5 | 0/2, 0.5 | 0/1, 1.0 | 0/0, 1.0 |
| V3_form → V3H_w1 | 0/2, 0.5 | 2/2, 1.0 | 0/2, 0.5 |
| C_chunks → V0_spans_eqchars | 3/0, 0.25 | 4/1, 0.375 | 1/0, 1.0 |

No comparison is significant (all p ≥ 0.25).

## Vector step and re-embedding (`results/latency.json`, `results/projection.json` `embed`)

| Vector step, 9,104 spans, LIMIT 50 | p50 ms | p95 ms | max ms | overlap with exact top 10 / top 50 |
| --- | --- | --- | --- | --- |
| exact (sequential scan) | 26.44 | 31.35 | 41.22 | 1 / 1 |
| HNSW ef_search 40 (default) | 0.63 | 0.98 | 1.67 | 0.967 / 0.810 |
| HNSW ef_search 100 | 1.13 | 1.41 | 1.98 | 0.986 / 0.975 |

Table 117.1 MB, HNSW index 68.4 MB, built in 3.35 s. The 150 ms threshold applies to this vector step alone; the
6.6 ms measured earlier was 2,285 chunks, the 173 ms end-to-end search p95 is a different quantity.

| Re-embedding (13 CPUs, batches of 8) | texts | wall | texts/s |
| --- | --- | --- | --- |
| spans (V0) | 9,104 | 1,607.6 s (26.8 min) | 5.66 |
| chunks (today) | 2,247 | 1,125.2 s (18.8 min) | 2.00 |

Query embedding: 111 ms mean per question. 72 spans exceed 1,200 characters (today's `MAX_PASSAGE_CHARS`, so the
embed service's limit must rise for span-level vectors) and 1 exceeds 1,024 bge-m3 tokens (truncated).
Span sizes: mean 223 characters (p50 129, p95 654), about 89 Claude tokens at the mean; bge-m3 mean 54 tokens.

## Decisions for the free arms (B10 rule: keep a step only if its gain is significant, p < 0.05)

1. **`form` filter: not kept.** Net 0, 0 and +1 of 30 (p = 1.0 on every type). It only drops headings and
   uncaptioned-figure spans from exact and fact-check routes, which rarely reach the top 10 anyway.
2. **RRF hybrid on spans: not kept; stays off on every route.** w 5: net −1, −1, 0; w 1: net −1, 0, −1; after the
   form filter up to −2. Same direction as the chunk measurement (24 vs 27 of 40). Full text alone gets 40 of 90.
3. **Vector step: exact scan stays.** p95 31.35 ms ≤ 150 ms at 9,104 spans. HNSW is 30× faster but loses recall
   (top-50 overlap 0.81 at the default ef_search), which the rule does not need to buy yet. At linear growth the
   exact p95 reaches 150 ms near 44,000 spans, about 4–5 books of this size.
4. **V0 spans vs today's chunks: no significant difference** (net −2, −3, 0; p 0.625, 0.375, 1.0), while the top
   10 spans carry 3.8× less text. With the same amount of text, spans hit 83 of 90 against 76 (net +3, +3, +1;
   not significant). This is not a B10 "step" (spans are B1's decision), so the rule neither keeps nor drops it;
   the owner should know that at 10 cards the thematic route sits 3 questions below today's chunks.
5. **What recall can still decide.** V0 misses 5 exact, 13 thematic and 1 fact-check question. A significant gain
   needs a net of at least 6, so by recall@10 a context prefix or an L2 summary can be kept **only on thematic**;
   on exact (5–0 gives p = 0.0625) and fact-check it cannot pass whatever its effect. Sufficiency from the judge is
   the only measure that can keep them there.

## Paid arms: cost projection only (`results/projection.json`)

Prices from `server/src/reader/agent/pricing.yaml` (Haiku 5.5: input 0.10, output 0.50, 1-hour cache write 0.20,
5-minute write 0.125, cache read 0.01 USD per million; Sonnet 5.5 2.00 / 10.00; Opus 5.5 4.00 / 20.00), Decimal,
quantised to micro-dollars with ROUND_HALF_UP as `pricing.micro`, each call priced on its own (the >100k tier is
per request; the largest prefix call is 45,691 tokens, below it).

Inputs: real span and chapter sizes from the artifact; Claude tokens = 0.398 × characters (`spikes/compile-cost`:
L02's 60,034 characters ≈ 23,900 tokens), which puts the book at about 808,000 tokens and a chapter at 20,100–45,200;
410 CLI overhead tokens per call (`spikes/tutor-call`, custom system prompt); instruction sizes are assumptions
(prefix 120, summary 150, judge 600 tokens; judge output 200). Chapters are the 23 "Глава N" bookmarks; 705 spans
outside them are one more group with a mean-chapter context (≈ 35,100 tokens; their own text is 188,492 characters,
228,699 bytes, ≈ 75,000 tokens by the 0.398 ratio, which the runner sends as that group's context: 705 cache reads of ≈ 40,000 more tokens each add
≈ $0.29, so the `prefixes` stage projects ≈ $3.63 against its $4.01 cap). Wall time at 1.82–2.10 s per Haiku call, sequential.

| Arm | Calls | Input tokens | Output tokens | USD | Wall time |
| --- | --- | --- | --- | --- | --- |
| Context prefixes, whole book, chapter context, **no caching** | 9,104 | 305.6 M | 0.46–0.91 M | 30.79–31.02 | 4.60–5.31 h |
| Context prefixes, whole book, chapter cached (1-hour write, as the CLI writes) | 9,104 | 305.6 M (≈ 98% cache reads) | 0.46–0.91 M | **3.50–3.73** | 4.60–5.31 h |
| same, 5-minute cache write | 9,104 | | | 3.45–3.67 | |
| Prefixes, 2 chapters first (6 and 13: most questions, 13 of 90), cached 1 h | 822 | 32.7 M | 0.04–0.08 M | **0.37–0.39** (uncached 3.29–3.31) | 25–29 min |
| L2 summaries, one per outline node + root (379 leaf, 88 inner, 27 level-1) | 495 | 1.20 M | 0.07 M | **0.16** | 15–17 min |
| judge.eval sufficiency, Sonnet 5.5, 6 variants (C, V0, V3, H_w5, V1, V2), identical top-10 sets judged once | 477 | 1.49 M | 0.10 M | **3.94** (Opus 7.87) | not measured for Sonnet |
| Fact-check route vs whole-section baseline: 30 judge calls with the question's chapter, Sonnet 5.5 | 30 | 0.98 M | 0.006 M | **2.02** (Opus 4.04) | not measured |

Prefix output is the B1 range 50–100 tokens. Without caching the prefixes alone exceed what is left of the test
budget, so caching is a condition, not an optimisation: the chapter must be the leading, byte-identical part of
every call's prompt, and the 2-chapter stage checks that the CLI actually reads it from cache across processes
(compile-cost run 2 observed such a cross-process hit, with 1-hour writes).

**Initial `index` caps (B7: cost + 20%), provisional until measured:** prefixes $4.48 per pack (3.73 × 1.2), L2
summaries $0.19 per pack.

## Proposed design and spend request

Budget: `test_budget_usd` $30; spent from it $3.2966 (ledger: tutor-call 0.0383, V.live 1.2587 + 0.90 + 1.0870,
V.freshness 0.012588); left $26.70.

Cheapest order that still answers B10, each stage with a stop rule:

1. **Prefixes for chapters 6 and 13** (822 Haiku calls, ≈ $0.39, ≈ 27 min). Record real input, cache-read and
   output tokens, USD and wall time. **Stop and re-ask** (as `paid.py` enforces it): if 3 consecutive later calls
   of a chapter read under 80% of that chapter's context from cache (the stage stops at once), if the stage's cache
   reads over all its calls are under 80% of expected, if a chapter's first call neither writes nor reads a cache, or
   if the stage would pass its cap of projection + 20% ($0.47).
2. **Prefixes for the rest of the book** (superseded by "Stage 1 measured" below: $4.29, cap $4.72) (8,282 calls,
   ≈ $3.63 with group 0's real context, cap $4.01 =
   projection + 10.5%, ≈ 4.2–4.8 h sequential), re-embed (≈ 27 min or
   more, local, free), recall@10 for V1. Needed because the 90 questions cover all 23 chapters.
3. **L2 summaries** (495 calls, ≈ $0.16), recall@10 for V2.
4. **judge.eval sufficiency** on the six variants, Sonnet 5.5 (477 calls, ≈ $3.94): the only measure that can keep
   a prefix or summary on exact and fact-check (Decision 5).
5. **Fact-check route vs whole-section baseline**, Sonnet 5.5 (30 calls, ≈ $2.02). The baseline is a **proxy**:
   the question's whole chapter as one card stands in for today's whole-section fact-check (the lesson's section
   sent to the fact-check stage); the judge's verdict is compared with `claim_holds`. A claim judged `undecided`, or
   none, counts as not correct, and its question as not sufficient unless the judge said sufficient.

**Spend request: approve $12.00 from `test_budget_usd`** for stages 1–5 (projection $9.85 + 20%), leaving $14.70.
Stage 1 alone is $0.50 with margin if the owner prefers to approve in two steps (stage 1 now; stages 2–5 after its
measured numbers). Every stage's spend goes to `budget.ledger`.

## Stage 1 measured (owner's run, 2026-11-26; `results/paid/stage1/`)

| Measure | Projected | Measured |
| --- | --- | --- |
| Calls, chapters 6 and 13 | 822 | 822 (0 failed, 0 empty, full coverage) |
| USD | 0.37–0.39 | **0.4493** (SDK 0.44929147; recomputed from tokens with pricing.yaml 0.449298) |
| USD per call | 0.00045–0.00048 | 0.000547 |
| Cache reads / expected | ≥ 80% | 31,080,627 / 31,397,244 = **0.9899** |
| Cache writes | ≈ 80,000 (2 chapters) | 463,399 |
| Output tokens | 41,100–82,200 | 91,282 (111 per call) |
| Uncached input tokens | ≈ 73,000 | 1,644 |
| Chapter context, tokens per character | 0.398 (L02) | 0.392 (ch. 6), 0.382 (ch. 13) |
| Wall per call, p50 / p95 | 1.82–2.10 | 1.71 / 2.31 s |
| Run wall, concurrency 4 | 25–29 min sequential | 367 s (0.447 s per call) |
| HOME left per call | — | 40 bytes (0.36 MB over 9,104 calls; tmpfs check passed) |

**Why it cost 15% more.** The CLI also writes the span (the user message) to the 1-hour cache, so the span is billed
at the write rate (2× input) instead of as plain input (1,644 uncached input tokens in total), and the prefixes
average 111 output tokens, above B1's 50–100. The cache itself works: 99% of the chapter context was read.

**`prefixes` re-projected from stage 1** (per chapter: one write of the chapter at the measured 0.387 tokens per
character, later calls read it at the cache rate, plus the measured $0.000150 per call of span write and output;
group 0 with its own 228,699-byte context): **$4.29 for 8,282 calls, ≈ 62 min at concurrency 4 (≈ 4.05 h
sequential at the measured 1.76 s mean)**. That was above the stage's $4.01 cap (the stage would have stopped at
about 93% coverage). The whole-book prefix cost becomes $0.45 + $4.29 = **$4.74 per pack**, an initial `index` cap
of **$5.69** (+20%).

**Owner decision (2026-11-27):** the paid-arm total is raised from $12.00 to **$12.51**, the `prefixes` cap from
$4.01 to **$4.72** ($4.29 + 10%), other caps unchanged, because stage 1 measured +15% from the CLI's cache writes
of each span at 2× the input price. Stage 1's $0.4493 + the caps of the four remaining stages = $12.51.

## `prefixes` run 1 (owner's run, 2026-11-27; `results/paid/prefixes/`): stopped, diagnosed, fixed

Exit 3 after 3,992 of 8,282 calls (chapters 0–10 done, 7 calls of chapter 11), 1,929.6 s at concurrency 4 (0.483 s
per call), p50 1.59 s, p95 3.83 s per call.

1. **Cost: $2.1445 is the billed figure** (the SDK's sum 2.14448713, rounded half-up to 4 decimals as the ledger
   rows are; at micro-dollars, ROUND_HALF_UP, 2.144487). Recomputing every row from its tokens at pricing.yaml's
   base Haiku rates gives the same 2.144487. The runner's $2.8368 was a bug in its cross-check: the CLI's usage is
   a sum over all API requests of one call, and 67 group-0 rows, 59 continued to 4 requests (≈ 167,000 cache-read
   tokens = 4 × ≈ 42,000) and 8 to 3 requests (≈ 126,000), passed 100,000 only as sums; the check applied the
   above_100k tier (5×) to them, $0.69 too much. Each request stayed under 100,000. Fixed: a summed usage is priced
   at the base tier, as `pricing.cost(summed=True)` does with `rates(model, 0)`; paid.py separately stops a stage
   whose per-request context estimate passes 100,000.
2. **54 failed calls**, all `ResultError`, all in group 0 (front and back matter; rows 42–681 of the run, spread
   over the group rather than clustered in time), each continued to 4 requests with 1,200 output tokens, and all
   billed ($0.145 together; tokens present). They were not long answers: 50 of the 54 stored no answer of the
   model but the same 155-character text, the other 4 short texts; their spans are 1 to 112 characters (one
   12,847), 34 text and 20 heading spans. Where the ≥ 1,200 output tokens went is **not measured** (no answer text
   was kept and the CLI reports no per-request detail). The old code stored those texts as prefixes. Now a span
   whose latest call failed is not done; on resume such spans run **last**, after every span never tried, so they
   cannot hold up chapters 11–23; a second failure records `prefix: none` (the span keeps its titles-only context,
   the ADR-0039 B7 caps rule) and does not count towards the stop after 3 consecutive errors. The output cap stays
   300, as in run 1: a higher cap has no measured basis.
3. **Chapter 11's "cache misses" were false.** Its first call was continued once (374 output tokens): request 1
   wrote the context (20,425), request 2 read it (20,060), and the summed usage made the expected context 40,485.
   The next six calls read 19,669 each, which is the whole context, but under 80% of the doubled number. Not a TTL
   expiry, eviction, concurrency or a changed context. Fixed: the context is never a sum. A chapter with earlier
   successful rows reuses their modal cache read (group 0: 41,839–42,231; chapter 11: 19,669); otherwise a cold
   first call (one that wrote at least half the chapter's estimated tokens) gives its write, whatever its number of
   requests, and a warm first call gives the modal cache read of the next 3 calls. The 100,000 stop applies to that
   value. A read counts at most once per call in the stage ratio (whose 1.0655 was inflated the same way).
   `dry_run.sh` covers cold first calls continued to 2, 3 and 4 requests and a warm one continued to 3.
4. **Remaining work at the measured cost:** 4,290 spans of chapters 11, 12 and 14–23 (12 chapter-first cache
   writes included; per call the chapter read at the measured 0.4005 tokens per character plus $0.000145 of span
   write and output) ≈ **$2.18**, then the 54 retries with group 0's cache write: ≈ $0.04 if they succeed,
   ≈ $0.15 if each is continued to 4 requests again (run 1's premium, $0.0027 per call). **≈ $2.22–2.33, ≈ 35 min**
   at concurrency 4. The stage cap leaves $4.72 − $2.1445 = $2.58, a margin of ≥ 10%; spent so far $2.5938, and
   with the other caps the total stays at $12.51. Checked without spend: the fixed `paid.py` resumed a copy of this
   run's real `calls.jsonl` and prefixes against the stub CLI (`--network none`): 4,344 calls in the order
   chapters 11, 12, 14–23, then group 0, the 54 retries as the last 54 calls; all 9,104 indexed spans done, cache
   ratio 0.9952, no stop, exit 0. `dry_run.sh` (38 of 38) also makes run 1's 54 spans fail every time (the stub
   gets their sha256 prefixes, not their text): the stage completes in one run, each is retried once after every
   other span and ends as `prefix: none`. To keep such clusters from stopping a stage, it now stops after 10
   consecutive failed calls instead of 3 (at most ≈ $0.03 on a systemic failure); calls without a reported cost
   still stop it after 3.

## `prefixes` run 2 (owner's run, 2026-11-27): stage complete

| Measure | Value |
| --- | --- |
| Calls this run | 4,344: 4,290 never tried (chapters 11, 12, 14–23, then group 0) + 54 retries of run 1's failures, last |
| Stage coverage | 8,282 of 8,282 stage spans; the book's 9,104 indexed spans all done (stage 1 + prefixes) |
| `prefix: none` | **30** spans, all in group 0: retries that failed again (titles-only context); 24 of the 54 retries succeeded |
| Failed calls, stage | 84 = run 1's 54 + run 2's 30 retries (all `ResultError`, group 0); no failure in chapters 1–23 |
| USD | run 2 **$2.19591864** (projected $2.22–2.33); stage total $4.34040577, recomputed from tokens $4.340415 |
| Cache reads / expected, stage | 271,921,823 / 275,073,322 = **0.9885** |
| Wall | run 2 2,000.4 s at concurrency 4 (0.46 s per call); per call p50 1.63 s, p95 2.45 s |

**Whole-book context prefixes (one pack): $4.7897** (stage 1 $0.4493 + prefixes $4.3404) for 9,104 spans and
9,158 calls (822 + 8,336, of which 84 failed), about 1 h 12 min of runs at concurrency 4 (367 + 1,930 + 2,000 s);
sequential at the measured p50 that is about 4.2 h. Initial `index` cap per pack for prefixes (B7, cost + 20%):
**$5.75**. Paid so far $4.7897 of the $12.51 total ($7.72 left); caps left: summaries $0.19, judge $4.73, factcheck $2.42.

**Next, in order:** the owner runs `paid.sh summaries`; the agent then runs `v12.sh` itself (free: local
embedding in the embed image with `--network none`, no token, no model call); then the owner runs `paid.sh judge`
and `paid.sh factcheck`.

## `summaries` run 1 (owner's run, 2026-11-27; `results/paid/summaries/`): stopped, diagnosed, fixed

Exit 3, "10 consecutive failed calls", after 23 calls, all leaves (level 4 runs first): 5 succeeded, 18 failed,
$0.02656115 (recompute agrees), output 15,568 tokens, per call p50 8.73 s, p95 18.37 s.

- **What failed:** every failure is `ResultError` with exactly 800 output tokens = 4 × the 200-token output cap
  (`TOKENS["leaf"] × 2`). The answers ran past the cap and the CLI continued each call to 4 requests, then failed it;
  the long wall times are those 4 sequential generations (thinking is off), not thinking. The successes used 158–195
  output tokens (one 476, continued and finished). The text stored for a failure is the CLI's 155-character error
  message (19 words, ASCII, not an answer in the book's language), identical up to its digits (the cap) to the text
  stored for the 54 prefix failures of `prefixes` run 1 (compared by hash, digits masked), whose 1,200 output tokens
  are 4 × that stage's 300 cap: those were the same output-cap continuation, which settles the cause item 2 above
  left unmeasured; the code no longer stores any text of a call whose result is
  not a success.
- **Cause:** the prompt asked for 70 words; this model writes about 3 tokens per Russian word (57 words = 171
  tokens), so a leaf answer of ≈ 210 tokens needed more than the 200-token cap and more than B1's leaf budget of
  100. The runner's caps did not match B1: 200 / 600 / 600 / 1,000 output tokens against budgets of
  100 / 300 / 300 / 500 for leaf / inner / chapter / root, with prompts asking for ≈ 2× the budget.
- **Fix:** the prompt asks for 30 / 90 / 90 / 150 words (≈ 90 / 270 / 270 / 450 tokens, inside the budgets); the
  output cap is 4× the budget (400 / 1,200 / 1,200 / 2,000), so an answer that overruns the asked length still ends
  in one request (the budget is enforced at read time, B1); a node's first failure is retried once after the rest
  of its level (children still before parents), a second failure records no summary (the node is not indexed in
  V2); an empty answer counts as a failed call. `dry_run.sh` reproduces the shape (stub answers longer than the
  cap are continued to 4 requests and fail): 210-token answers fail under the old 200 cap and finish under the new
  caps; answers over every leaf cap stop the stage after 10 in a row.
- **Re-projection:** the original $0.16 priced the input as plain input; the CLI writes it to the 1-hour cache at
  2× (as stage 1 showed), and the measured prompt is ≈ 0.38 tokens per character of a node's text + ≈ 500 tokens of
  overhead. For the 490 remaining calls (1.22 M prompt tokens with the children's summaries, ≈ 72,000 output
  tokens at the budgets): **≈ $0.28**, about 15 min at concurrency 4. The stage cap of $0.19 leaves $0.163, which
  does not fit. **Needed: a summaries cap of $0.36** ($0.28 + 20% + the $0.0266 spent). It fits the $12.51 total:
  spent $4.8163 + summaries $0.36 + judge $4.73 + factcheck $2.42 = $12.33 (prefixes finished $0.38 under its cap).
  Checked without spend: the fixed `paid.py` resumed a copy of run 1's rows against the stub (171-token answers,
  `--network none`): 490 calls (472 new + run 1's 18 failures retried), all 495 nodes summarised, exit 0;
  `dry_run.sh` 40 of 40.
- **Owner decision (2026-11-27):** the summaries cap is raised from $0.19 to **$0.36**; the total stays $12.51.

## Paid runner (approved by the owner 2026-11-26, $12.00 staged; raised to $12.51 on 2026-11-27; the owner runs it)

`paid.sh <stage> [concurrency]` follows `spikes/tutor-call/run.sh`: the Keychain item is piped on stdin into a
hardened container of the compile-cost image (read-only root, cap-drop ALL, no-new-privileges, tmpfs home, 4 GiB,
`timeout -s KILL`), exported only inside its process tree, never printed or written. `paid.py` makes one CLI call
per item with no tools and thinking off. Stages and caps (projection + 20%, except `prefixes`, whose $4.72 is stage 1's measured re-projection $4.29 + 10%):
`stage1` $0.47, `prefixes` $4.72
(refused unless stage1 passed), `summaries` $0.19, then the free `v12.sh` (V1/V2 embedding, recall, `topk.json`),
`judge` $4.73, `factcheck` $2.42; stage 1's spend plus the other caps = $12.51, the owner's total. Gates (`gates.sh`): `prefixes` needs stage1 passed
(not stopped, every stage span prefixed, cache rule met); `judge` and `v12.sh` need stage1, prefixes and summaries
passed, every indexed span prefixed and every outline node summarised; a rerun of a passed stage makes no calls and
stays passed. Inside each concurrency slot, before a call starts, the runner reserves its estimated cost and starts
it only if recorded spend + reservations in flight + the estimate fit the stage cap and what is left of the owner's total, so
a stage makes at most cap / price + concurrency calls. A prefix stage stops after 3 consecutive later calls of a
chapter that read under 80% of its context from cache (calls in flight finish: at most concurrency − 1 more) and
fails if its cache reads over all its calls, all runs, are under 80% of expected (`ctx_expected` per row). A stage
also stops after 3 consecutive failed calls or calls without a reported cost; `summary.json` is written in a
`finally` and recomputes the cost from the tokens with `pricing.yaml` as a cross-check. The system prompt (for
prefixes the whole chapter, up to 194,793 bytes, 228,699 for group 0, over Linux's 131,072-byte single-argument limit) goes to the tmpfs and
reaches the CLI as `--system-prompt-file`, so it is the cached prefix and the span the only uncached input; stage1
prints the tmpfs usage at the end. Committed outputs: `results/paid/<stage>/calls.jsonl` and `summary.json` (ids,
tokens, USD, wall time, cache ratio; judge verdicts only as booleans, fact indices and the claim enum, anything
else is a parse failure); the prefixes and summaries stay in the gitignored `data/spikes/retrieval-ablation/paid/text`.
The last line is the `budget.ledger` row to add.

Checked without spend: `bash spikes/retrieval-ablation/dry_run.sh <scratch dir>` runs every stage in the real image
and SDK with `--network none`, no token, against a stub CLI (`stub/claude`: speaks the SDK's stream-json protocol,
refuses any argument of 131,072 bytes or more, a missing `--system-prompt-file` and a missing
`--no-session-persistence`, simulates the cache and, on request, files left in HOME). It never builds the image (that
needs the network) and refuses to run without it. 32 of 32 checks pass: 822 / 495 / 477 / 30 calls with full
coverage; a rerun of stage1 keeps its pass with 0 calls; every stage stops at a low cap within cap / price +
concurrency; with no cache hits stage1 stops (exit 3) after 4–7 calls; stage1 fails when HOME grows by 100 kB per
call (extrapolated past the 512 MB tmpfs over 9,104 calls); empty answers fail coverage; the gates refuse
incomplete, capped or HOME-failed stages; no committed row holds free text.

Without `--no-session-persistence` the real CLI writes a session transcript per call into the home tmpfs (about
2× the system prompt), which would fill 512 MB after about 1,500 prefix calls; paid.py passes the flag, stage1
records the home bytes per call and fails if they extrapolate past the tmpfs. Every call's row is written in a
`finally`, so a spend is recorded even if parsing fails; a chapter-first prefix call that reports over 100,000
prompt tokens (Haiku's higher tier) stops the stage.

## Limits

- recall@10 on gold pages is page-granular: a span on a gold page counts even if it lacks the fact; sufficiency
  (paid) closes that gap.
- Token counts for Claude are estimated from one measured ratio; stage 1 replaces them with measured numbers.
- Latency is measured in a fresh container with only the spike's tables; the live database has other tables and
  load, and its exact-scan p95 will be somewhat higher.
- Instruction sizes of prefix, summary and judge prompts are assumptions; they move the totals by under 1%
  (prefix, summary) and about 15% (judge).
