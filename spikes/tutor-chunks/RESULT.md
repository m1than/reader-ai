# Spike tutor-chunks — bytes per hit when the tutor sends a hit's whole chunk (2026-11-23)

**Question (SRV-TUTOR FR-TUTOR-5/-6 revision):** the first live run (3 questions) sent only each hit's primary span;
one answer was `insufficient` because the missing fact sat in a neighbouring span of the top chunk (investigator,
2026-11-23). If every hit sends all spans of its chunk, how many bytes does that take, and what alias cap and prompt
budget follow under the $0.30 per-question cap?

**Method:** read-only, no model spend. `measure.py` re-converts the pilot PDF (sha256 `61e8d4c3…`) on the host with
the stack's own pinned, confined converter (SRV-INGEST, docling 2.151.1+r2; deterministic for a converter version, so
the spans equal the stack's `material_spans`) into a scratch directory outside the repository (deleted afterwards),
chunks them with SRV-SEARCH's own `chunk_spans` (size 1,200, overlap 200, min 80), and measures each source as the
tutor sends it: cut to span_bytes_max (2,000) and JSON-escaped (`ensure_ascii=False`). Numbers only: `results.json`.
The owner session needed for `POST /api/search` and `GET /api/materials/{id}/spans` was not available to this agent
(the owner's passkey flow is the live agent's), so the stack's spans were reproduced instead of read over HTTP.

## Results (bytes as sent)

| | n | mean | p50 | p90 | p95 | p99 | max |
|---|---|---|---|---|---|---|---|
| one span | 9,463 | 342 | 204 | 866 | 1,155 | 1,849 | 2,018 |
| spans per chunk | 2,247 | 5 | 4 | 9 | 11 | 17 | 55 |
| primary span of a chunk (sent today) | 2,247 | 942 | 842 | 1,779 | 1,987 | 2,006 | 2,018 |
| whole chunk: all its spans (proposed) | 2,247 | 2,145 | 2,206 | 3,119 | 3,422 | 4,037 | 5,462 |

79 spans (0.8 %) exceed 2,000 bytes and are cut. Conversion took 665 s on the host.

## Findings

1. Sending whole chunks multiplies the bytes per hit by 2.3 at the mean (942 → 2,145). The live run's 6–10 KB of
   sources per question becomes about 8 × 2.2 KB ≈ 17 KB for 8 typical hits (fewer when hits share spans).
2. Budget under the cap: question_cap_usd 0.30 with attempts_max 2 needs a worst-case reserve ≤ $0.15 per attempt.
   With VEC-TUTOR-3's rule (every byte one token at the 1-hour cache-write rate, + 1,000 CLI tokens + 2,000 output
   tokens at Sonnet 5.5's rates) that is system + prompt ≤ 31,500 bytes. The system prompt file is 1,440 bytes, so
   system_bytes_max 4,000 → 2,000 frees 2,000 bytes: prompt_bytes_max 29,000, reserve (1,000 + 2,000 + 29,000) ×
   4.00e-6 + 2,000 × 10.00e-6 = $0.148, two attempts $0.296 ≤ $0.30.
3. Sources then get 29,000 − 8,000 (question 3,000 + cell 4,000 + JSON 1,000) = 21,000 bytes = 8 × 2,625: 8 hits of
   median size fit; at p90 size (3,119) about 6–7 hits fit and the lowest ranks are dropped (FR-TUTOR-6). Sending 8
   hits at p95 (8 × 3,422 = 27.4 KB) would need a ≈ 35 KB prompt, a $0.17 reserve and a second attempt over the cap:
   the budget is deliberately the smaller one.
4. Alias cap: 8 hits × a mean of 5 spans = 40 aliases (sources_max); a chunk of p90 size has 9 spans.

## Limits

- The distribution is over all 2,247 chunks, not over the chunks real questions retrieve; retrieved chunks may be
  longer or shorter than average. The first full bench (n ≥ 40) records the bytes and sources actually sent.
- This spike's chunk count (2,247) differs from SRV-SEARCH's [local] bench (2,285), which used pypdfium2 page text as
  spans; the stack chunks the converter's spans, as here.
- Bytes are an upper bound on tokens (measured 0.3–0.5 tokens per byte in spikes/tutor-call); the reserve keeps the
  one-token-per-byte bound of VEC-TUTOR-3.
