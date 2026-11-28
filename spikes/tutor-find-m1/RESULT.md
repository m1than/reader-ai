# Spike tutor-find-m1 — which sources the tutor would send through find (2026-11-28)

**Question (SRV-TUTOR kl_revision measurement_plan.free, M1):** over the 40 VEC-TUTOR-7 questions, how often does
a gold page reach the tutor's sources, after FR-TUTOR-6's 29,000-byte drop rule, for (a) today's merged path
`search(k=8)` with single spans, (b) find's L1 primaries, (c) primaries + each card's next span (the revision),
(d) primaries + next + previous? And what does an L1 card cost in the find budget?
**Model calls: 0. USD: 0.** The owner's stack and database were not touched.

## Method

- **Data.** The pilot PDF (sha256 `61e8d4c3…c9e2b2`) as converted by the stack's pinned converter (docling
  2.151.1+r2) for spikes/retrieval-ablation, in the gitignored `data/` of that worktree: 9,463 spans, 494 outline
  entries, `spans_sha256 8b1b3db9…` (the same as V.live's stack). Never committed.
- **Vectors.** `embed_m1.py` in `reader-embed:local` (`--network none`; the embed service's own encoder,
  BAAI/bge-m3@5617a9f6): every production window (FR-SEARCH-3, 1,200/200/80, no prefix; 9,316 windows,
  8,962 distinct texts) and the 40 questions. A window that is a whole span reused that span's vector from the
  retrieval-ablation spike (8,678, same image, encoder and text); 284 windows and the questions were encoded here.
- **Shipped code, throwaway database.** `m1.py` starts the search tests' pinned pgvector image through
  testcontainers and uses the SRV-SEARCH tests' own helpers: `world` (migrations), `add_material` (spans, kinds,
  outline; sealed active r2 pack, profile language ru), `embed_material` (the real `search.embed` handler). The
  embed service is the real `reader.search.embed_service` server whose encoder returns the precomputed vector by
  the text's sha256 (a missing text fails; no stand-in vectors). Then per question:
  (a) `store.search(k=8)` → `candidate_span_ids`; (b–d) `find(channel tutor, budget 10,500)` with `StoreCorpus`,
  judge None, the shipped policy (gate never_judge), L1 card ids in response order, then (c) each card's first
  `spn:` next id, then (d) each card's previous span in reading order, no repeats. Every variant goes through
  `views.active_span_texts` and `reader.tutor.answer.build_prompt` (no cell, the bench's case).
- **Measures.** `gold_in_sources`: a gold page among the sent sources' pages; gold spans and their bytes as sent;
  `gold_in_find`: a gold page among the L1 card pages (NFR-TUTOR-3's revised predictor); the L1 card cost
  `estimate(text_of(card, judged false)) + 0.5`, as `cards.cost`.
- Run: `bash spikes/tutor-find-m1/run.sh <artifact dir> [<retrieval-ablation work dir with spans.npy>]`.
  `results.json` holds question ids, counts, bytes and flags only.

## Results (`results.json`, n = 40)

The recorded numbers below come from commit 1dc2990 (before the tutor's Green). Since SRV-TUTOR's code review
round 1, m1.py runs at HEAD: variant (a) keeps the pre-revision rule inlined as recorded (search k = 8, primaries then
chunk spans), and variant (c) is the shipped `reader.tutor.jobs.candidate_ids`, asserted equal to the recorded rule.
Not re-run since (no spend either way; the re-run against the Green function is part of P1's preparation).

| Variant | gold in sources | sources (mean) | source bytes (mean) | gold spans (mean) | gold bytes (mean) | prompt bytes (max) |
|---|---|---|---|---|---|---|
| (a) search(k=8), single spans (today) | 21 | 8.0 | 3,214 | 1.15 | 567 | 8,517 |
| (b) find primaries | 28 | 25.4 | 3,723 | 1.65 | 385 | 9,816 |
| (c) primaries + next (revision) | **28** | 49.2 | 12,804 | 2.92 | 922 | 21,306 |
| (d) primaries + previous + next | 28 | 71.7 | 20,564 | 4.05 | 1,263 | 28,836 |

- `gold_in_find` 28 of 40 (equals (b): the drop rule never cut a primary). L1 cards per response: mean 25.4.
- **Mean L1 card cost: 400.6 estimator tokens** (the spec's estimate was about 437), so 10,500 tokens hold about
  25 cards. The drop rule never bound for (c) (0 of 40 questions lost a candidate); it bound for (d) in 4.
- (c) against (a) per question: 10 questions only (c) hits (v01, v09, v11, v13, v16, v17, v20, v24, v28, v36),
  3 only (a) hits (v22, v30, v32); exact two-sided McNemar p ≈ 0.09. (c) misses 12: v05, v12, v15, v21, v22, v26,
  v29, v30, v31, v32, v38, v40.
- Neighbours add gold-page text (gold bytes 385 → 922 → 1,263) but no new question: every question with a gold
  page in (c) or (d) already had one among the primaries.
- find wall time here (p50 47 ms) excludes a real query embedding; it is not a latency measurement.

## Verdict

- **Build threshold met:** (c) 28 ≥ (a) 21 and ≥ the chunk baseline's 26 of 40. The revision is promoted and built.
- **P1 threshold not met:** (c) reaches 28, not 40 of 40. By the baseline's conditional rates
  (21/26 · 28 + 4/14 · 12 ≈ 26.0 grounded, 0.65) the 0.8 gate would likely fail; P1 (paid, about $1.07–1.84)
  waits for the owner's explicit go or a change (a larger budget, variant (d), the judge, or the gate).
- Variant (d) adds nothing to `gold_in_sources` here, so "previous span" stays deferred.
