# Spike: compile-cost — RESULT

**Question (docs/roadmap.md 0.3; ADR-0005):** What does a full compile of one lesson (draft, fact-check, review) cost on the owner's subscription? Measure tokens, $ and minutes for L02.
**Pass criterion:** ≤ $4.35 and ≤ 45 min per lesson.
**Verdict: PASS for one review loop: $2.42–2.97 and 8.3–9.4 min (2 runs).** The loop did not converge: the second review still had 2–3 blocking findings in both runs. ADR-0009 allows up to 3 loops; at the measured cost per loop, 3 loops would exceed $4.35 (estimate below).

Date: 2026-11-20. Command: `bash spikes/compile-cost/run.sh`. Numbers per stage: `results/<run-id>/*.json` and `summary.json`. Lesson text and the source pages stay in the gitignored `data/spikes/compile-cost/`. Spend for this spike: **$5.395** of the $8.00 cap the owner set.

Note: ADR-0005 line 51 says the compile cost is measured "on L01"; the roadmap row says L02. This spike measured L02.

## Setup

| Item | Value |
| --- | --- |
| Lesson | L02 "Децентрализация" (`pilot/bashir/course.yaml`): chapter 2, PDF pages 60–84 (25 pages), 5 sections, no labs (stage S01 has `code_h: 0`) |
| Source | Docling output of the 25 pages, natively, configuration D from `spikes/ingest-docling` (formula model on, code model off): 60,034 characters, one page per block marked `=== PDF page N ===` |
| Runtime | `claude-agent-sdk` 0.2.186 (bundled CLI) on `python:3.14-slim-trixie`, non-root, read-only root, tmpfs home, cap-drop ALL, 2 GiB; no tools (all built-in tools disallowed), `setting_sources=[]`, `max_turns=3` |
| Credential | Keychain item `reader-ai/claude-oauth-token` (owner allowed it for this spike, 2026-11-20). It is piped to the container **on stdin** and exported inside only, so unlike `spikes/agent-sdk-container` it is not in `docker inspect`. Never printed |
| Caps | `max_budget_usd` per stage = min(ADR-0011 budget of its task class, what is left of $8.00), checked again before each stage; `timeout -s KILL 3000` for the container |
| Models | ADR-0011's example config: draft `claude-opus-5-5` ($2.00), fact_check `claude-sonnet-5-5` ($1.00), review `claude-opus-5-5` ($1.20); revise uses the draft task class |

Stages (ADR-0009), each a separate SDK query with a fresh context; prompts are in `prompts.py`:

| # | Stage | ADR-0009 | Model |
| --- | --- | --- | --- |
| 1 | draft: notebook JSON (ADR-0004 kinds: diagnostic, route, explain with cited claims, tradeoff, erratum, recall, open) | 3 | Opus |
| 2 | fact-check every claim against the source pages | 4 | Sonnet |
| 3 | adversarial review (fresh context; sees the fact-check report) | 6 | Opus |
| 4 | revise (findings loop back to the draft stage) | 3 | Opus |
| 5 | fact-check again | 4 | Sonnet |
| 6 | review again | 6 | Opus |

Left out (does not cost tokens here, or not applicable): stage 1 ingest and 2 plan (done once per course), stage 5 lab verification (L02 has no labs), gates G1 and G2 (owner actions), `changed` cells with web citations (no network tools were given, so web verification cost is **not** included), loops 2 and 3.

## Numbers

| Stage | Run 1 USD | Run 2 USD | Run 1 wall | Run 2 wall | Output tokens (1 / 2) | Cache write (1 / 2) | Cache read (1 / 2) | Outcome (1 / 2) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 draft | 0.605 | 0.296 | 138 s | 117 s | 17,591 / 14,374 | 31,370 / 0 | 0 / 31,370 | 39 cells, 80 claims / 37 cells, 59 claims |
| 2 fact-check | 0.237 | 0.202 | 33 s | 38 s | 4,601 / 4,703 | 46,774 / 37,562 | 0 / 6,803 | 87/87 supported, pass / 66 of 67, 1 unsupported |
| 3 review | 0.496 | 0.451 | 84 s | 68 s | 8,517 / 7,171 | 40,022 / 37,859 | 6,803 / 6,803 | 16 findings, 3 blocking / 13, 2 blocking |
| 4 revise | 0.871 | 0.738 | 190 s | 148 s | 25,825 / 20,214 | 43,658 / 41,106 | 6,803 / 6,803 | 50 cells, 83 claims / 49 cells, 60 claims |
| 5 fact-check | 0.242 | 0.235 | 44 s | 46 s | 5,997 / 6,617 | 44,379 / 41,025 | 6,803 / 6,803 | 91 of 94, 2 unsupported, 1 contradicted / 64 of 68, 4 unsupported |
| 6 review | 0.520 | 0.501 | 74 s | 78 s | 7,720 / 8,027 | 44,981 / 41,936 | 6,803 / 6,803 | 18 findings, 3 blocking / 16, 3 blocking |
| **Total** | **2.971** | **2.424** | **563 s** | **496 s** | 70,251 / 61,106 | 251,184 / 199,488 | | not converged |
| First pass (1–3) | 1.338 | 0.949 | | | | | | |
| One loop (4–6) | 1.634 | 1.475 | | | | | | |

All stage outputs parsed as JSON; every stage finished in 1 turn with `subtype: success`; no stage hit its budget.

## Findings

1. **Cost is dominated by cache writes of the source.** Every stage writes 31–47k tokens of cache (the 60k-character source plus stage-specific text) and reads only 6,803 (the CLI's system prompt), because each prompt puts the source *after* stage-specific instructions, so no two stages share a prefix. The draft of run 2 shows what reuse is worth: its prompt was identical to run 1's, read 31,370 tokens from cache instead of writing them, and cost $0.296 instead of $0.605. **Putting the source first, as an identical prefix in every stage, should turn 5 of 6 source writes into reads; not measured.**
2. **The first fact-check of run 1 rubber-stamped.** It reported 87/87 supported in a 113-character JSON with no itemized checks, while the second fact-check of the same lesson's revision found unsupported and contradicted claims. SRV-PIPELINE must require itemized output per claim (and reject a report without it).
3. **One loop does not converge.** Both runs ended with 2–3 blocking review findings after the revision, and the second fact-check found 1–4 unsupported claims. Whether loops 2 and 3 converge, and how the owner's G2 would judge the result, is not measured.
4. **Variance between runs:** total 2.42 vs 2.97 (−18%); about $0.31 of the difference is the draft's cache hit, and the draft of run 2 was also shorter (59 vs 80 claims).
5. **Time is not a constraint:** 8.3–9.4 min, against 45.

## Budget arithmetic (ADR-0005: 23 lessons, ≤ $100, ≤ $4.35 per lesson)

Measured for L02 (25 PDF pages):
- First pass (stages 1–3): $0.95–1.34. One loop (4–6): $1.48–1.63. One loop in total: **$2.42–2.97**.
- 23 lessons × $2.42–2.97 = **$56–68** for the course with one loop each, within the $100 target.

Estimates (not measured):
- Up to 3 loops, as ADR-0009 allows: $0.95–1.34 + 3 × $1.48–1.63 = **$5.4–6.2 per lesson**, above $4.35. The course at that worst case: 23 × $5.4–6.2 = $124–143, above $100.
- Bashir chapters average 39 pages against L02's 25 (ADR-0009). Input and cache tokens scale with the source, but output does not, so a larger chapter would cost somewhat more per loop; this is not measured.
- With the source as a shared cached prefix (finding 1), the cache-write share of each stage after the first would mostly become reads; how much that saves is not measured.

So the ≤ $4.35 target holds for a single review loop; extra loops, larger chapters and web verification are the risks, and the cache layout is the main lever.

## Hands to stage 5 (SRV-AGENT, SRV-PIPELINE)

- Per-lesson budget for one loop: about $2.4–3.0 and under 10 minutes for a 25-page chapter.
- Put the source first, as an identical prefix in every stage, to make it cacheable.
- The fact-check must return an itemized verdict per claim; a summary-only report is a failure.
- Decide what happens when loop 1 does not converge: more loops (cost above), or turning the remaining findings into `open` cells for G2.
- `max_budget_usd` works as a per-stage cap in `claude-agent-sdk` 0.2.186 (present in `ClaudeAgentOptions`; no stage reached it here, so the stop behaviour itself was not exercised).
- Delivering the token on stdin keeps it out of `docker inspect`; this is the pattern to compare with ADR-0011's `/run/secrets` in OPS-DEPLOY.
