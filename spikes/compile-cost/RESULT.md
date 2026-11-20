# Spike: compile-cost — RESULT

**Question (docs/roadmap.md 0.3; ADR-0005):** What does a full compile of one lesson (draft, fact-check, review) cost on the owner's subscription? Measure tokens, $ and minutes for L02.
**Pass criterion:** ≤ $4.35 and ≤ 45 min per lesson.

**Verdict:**
- **PASS for draft + fact-check + review with one revision loop:** L02 cost about $2.7–3.0 cold and took 8.3–9.4 min.
- **The cost to a G2-eligible lesson was not measured.** 0 of 2 runs converged.
- **More loops go over $4.35.** Projected: at 3 loops for both runs, and already at 2 loops for run 1 (arithmetic below).

Date: 2026-11-20. Command: `bash spikes/compile-cost/run.sh`.
- **Numbers per stage:** `results/<run-id>/[1-6]-*.json` and `summary.json`.
- **Lesson text and source pages:** in the gitignored `data/spikes/compile-cost/`.
- **Spend for this spike:** **$5.395** (run 1 $2.9714, run 2 $2.4236) of the $8.00 cap the owner set.

**Open question for the owner, L01 vs L02:** ADR-0005:51 and ADR-0009:51 say the compile cost is measured on L01, the roadmap row says L02, and this spike measured L02. L01 is the larger chapter: PDF pages 27–59, 33 pages, 135,935 characters per ADR-0005, against 60,034 characters for L02. So L02 is the optimistic sample.

## Setup

| Item | Value |
| --- | --- |
| Lesson | L02 "Децентрализация" (`pilot/bashir/course.yaml`): chapter 2, PDF pages 60–84 (25 pages), 5 sections, no labs (stage S01 has `code_h: 0`) |
| Source | Docling output of the 25 pages, natively, configuration D from `spikes/ingest-docling` (formula model on, code model off): 60,034 characters, each page marked `=== PDF page N ===` |
| Runtime | `claude-agent-sdk` 0.2.186 (bundled CLI) on `python:3.14-slim-trixie`; non-root, read-only root, tmpfs home, cap-drop ALL, `no-new-privileges`, 2 GiB, 256 PIDs; `setting_sources=[]`, `max_turns=3` |
| Credential | Keychain item `reader-ai/claude-oauth-token` (owner allowed it for this spike, 2026-11-20). It is piped to the container **on stdin** and exported inside only, so unlike `spikes/agent-sdk-container` it is not in `docker inspect`. Never printed |
| Spend cap | A running-total check before each stage against $8.00 minus every recorded stage (`results/*/[0-9]*.json`), so overshoot is bounded by one stage (≤ ≈ $1.5 here). Plus the SDK's per-stage `max_budget_usd` = min(ADR-0011 budget of the task class, what is left), which no stage reached, so its stop is **untested**. The container has `timeout -s KILL 3000` |
| Tools | The recorded runs used a denylist of 11 built-in tool names (`disallowed_tools`) and `allowed_tools=[]`. Evidence that no tool ran: `num_turns` is 1 in all 12 stages, `server_tool_use` (`web_search_requests`, `web_fetch_requests`) is 0 in all 12, and the container has no network beyond the API and no writable root. `pipeline.py` now also sets `tools=[]` (`ClaudeAgentOptions.tools`, present in 0.2.186), added after the runs and not exercised by a model call |
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

Left out, either because they cost no tokens here or because they don't apply:
- stages 1 (ingest) and 2 (plan), which run once per course;
- stage 5, lab verification: L02 has no labs;
- gates G1 and G2, which are owner actions;
- `changed` cells with web citations: no network tools were given, so the cost of web verification is **not** included;
- loops 2 and 3.

## Numbers

| Stage | Run 1 USD | Run 2 USD | Run 1 wall | Run 2 wall | Output tokens (1 / 2) | Cache write (1 / 2) | Cache read (1 / 2) | Outcome (1 / 2) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 draft | 0.605 | 0.296 | 138 s | 117 s | 17,591 / 14,374 | 31,370 / 0 | 0 / 31,370 | 39 cells, 80 claims / 37 cells, 59 claims |
| 2 fact-check | 0.237 | 0.202 | 33 s | 38 s | 4,601 / 4,703 | 46,774 / 37,562 | 0 / 6,803 | 87/87 supported, pass / 66 of 67, 1 unsupported |
| 3 review | 0.496 | 0.451 | 84 s | 68 s | 8,517 / 7,171 | 40,022 / 37,859 | 6,803 / 6,803 | 16 findings, **3** blocking / 13, **2** blocking |
| 4 revise | 0.871 | 0.738 | 190 s | 148 s | 25,825 / 20,214 | 43,658 / 41,106 | 6,803 / 6,803 | 50 cells, 83 claims / 49 cells, 60 claims |
| 5 fact-check | 0.242 | 0.235 | 44 s | 46 s | 5,997 / 6,617 | 44,379 / 41,025 | 6,803 / 6,803 | 91 of 94: **2 unsupported + 1 contradicted** / 64 of 68: **4 unsupported** |
| 6 review | 0.520 | 0.501 | 74 s | 78 s | 7,720 / 8,027 | 44,981 / 41,936 | 6,803 / 6,803 | 18 findings, **3** blocking / 16, **3** blocking |
| **Total** | **2.971** | **2.424** | **563 s** | **496 s** | 70,251 / 61,106 | 251,184 / 199,488 | | not converged |
| First pass (1–3) | 1.338 | 0.949 | | | | | | |
| One loop (4–6) | 1.634 | 1.475 | | | | | | |

- **Output and errors:** all stage outputs parsed as JSON; every stage finished in 1 turn with `subtype: success`; no stage hit its budget.
- **Cache type:** all 450,672 cache-write tokens are `ephemeral_1h`; none are `ephemeral_5m`.
- **Nothing improved after the revision.** Blocking review findings went 3 → 3 (run 1) and 2 → 3 (run 2). Fact-check problems went from 0 to 3 (run 1) and from 1 to 4 (run 2).

### Cold and warm

**Run 1 is a cold compile.** Run 2's draft prompt was byte-identical to run 1's and still in the 1-hour cache, so its 31,370 prompt tokens were read instead of written. Run 2 is therefore a compile with a **warm draft cache**.

**Rates, inferred from these results, not from a price sheet.** I assumed the standard multipliers: cache read 0.1×, 1-hour cache write 2×, output 5× the input rate. The two drafts then give a consistent Opus input rate of about $4.0 per million tokens (4.02 and 3.95).

**The two drafts differ by $0.309:**
- ≈ $0.24 from the cache: 31,370 tokens at a write cost minus a read cost;
- ≈ $0.06 from the shorter output: 3,217 fewer tokens.

**Run 2 cold-equivalent:** 2.42 + 0.24 ≈ **$2.66**. The per-lesson cold-compile range is therefore **≈ $2.7–3.0**.

## Findings

1. **Cost is dominated by cache writes that nothing re-reads.**
   - Every stage writes 31–47k cache tokens (the 60k-character source plus stage-specific text).
   - Within a run, cache reads cover only the CLI's 6,803-token system prompt, in stages 3–6 of run 1 and 2–6 of run 2. The exception is run 2's draft, which hit run 1's cache.
   - The cause: each prompt puts the source *after* stage-specific instructions, so no two stages share a prefix.
   - Two levers, both **unmeasured**:
     - Put the source first, as an identical prefix in every stage, so that 5 of 6 source writes become reads.
     - Every write is `ephemeral_1h`, which costs about 2× plain input at the inferred rates. Each stage pays that premium for a cache nothing re-reads; plain input or 5-minute writes would be cheaper unless the prefix is shared.
2. **The first fact-check of run 1 rubber-stamped.** It reported 87/87 supported in a 113-character JSON with no itemized checks, while the second fact-check found unsupported and contradicted claims. SRV-PIPELINE must require an itemized verdict per claim and reject a report without one. Per-claim output will raise fact-check output above the measured 4.6–6.6k tokens.
3. **Non-convergence looks structural, not like noise.** From the reviewer's reading of the stage outputs:
   - **New findings each time:** the second-review findings are new ones, not survivors from the first.
   - **Against the book:** several object to statements the book itself makes.
   - **Prompt design:** the revise prompt turns non-blocking findings into open cells.
   So more loops may not converge either, and each later loop costs more because the notebook grows (39 → 50 cells in run 1).
4. **Variance between runs:** totals of $2.42 vs $2.97 (−18%). Most of the gap is the draft's warm cache and its shorter output (59 vs 80 claims).
5. **Time is not a constraint:** 8.3–9.4 min, against 45.

## Budget arithmetic (ADR-0005: 23 lessons, ≤ $100, ≤ $4.35 per lesson)

**Measured for L02 (25 PDF pages):**
- First pass (stages 1–3): $0.95–1.34.
- One loop (stages 4–6): $1.47–1.63.
- One loop in total: $2.42 warm, $2.97 cold; ≈ $2.7–3.0 cold.

**Estimates (not measured):**
- **2 loops:** 0.95 + 2 × 1.47 ≈ **$3.90** (run 2); 1.34 + 2 × 1.63 ≈ **$4.61** (run 1, above $4.35).
- **3 loops,** the ADR-0009 maximum: 0.95 + 3 × 1.47 ≈ **$5.37**; 1.34 + 3 × 1.63 ≈ **$6.24**. Both above $4.35.
- All loop figures use the unrounded stage sums (0.9489, 1.4747, 1.3377, 1.6337); with the rounded values shown, run 1's 2-loop figure reads 4.60.
- **Course, one loop each, at L02's size:** 23 × $2.42–2.97 = **$56–68**.
- **Course scaled to the book's chapter size.** The book has 890 pages over 23 chapters, 39 pages on average (ADR-0009), against L02's 25. Factor 39/25 = 1.56.
  - **Source input only.** Assume about 29,900 source tokens per stage (the draft's 31,370 prompt tokens minus about 1,500 of instructions). Extra per lesson at the inferred rates:
    - run 1 (6 cold writes): 0.56 × 29,900 × 6 × $8.0/M ≈ $0.80 → $3.77 per lesson → **≈ $87** for 23;
    - run 2 (5 writes, 1 read): 0.56 × 29,900 × (5 × $8.0 + 1 × $0.40)/M ≈ $0.67 → $3.10 per lesson → **≈ $71**.
  - **All cost scaled:** 890 / 25 × $2.42–2.97 = **$86–106**.
- **Against ADR-0005's $100,** with one loop per lesson: from about $56 at L02's size to about $106 when everything scales with page count. The single-loop compile fits only if costs do not scale fully with chapter size. Any second loop, web verification of `changed` cells, or labs push it further. The caching levers in finding 1 work in the other direction.

## Owner asks

- **(a) Non-convergence policy:** allow more loops (cost above), hand open findings to G2, or revise the review bar (for example, objections to the book's own claims become `erratum`/`open` cells, not blocking findings). This triggers the roadmap fallback "fewer review loops; ADR-0005 budget amendment".
- **(b) L01 vs L02:** ADR-0005:51 and ADR-0009:51 say L01, and the roadmap says L02. L02 is the optimistic sample (25 against 33 pages, 60,034 against 135,935 characters). Which document gets corrected? If L01 is the reference, its compile should be measured.

## Hands to stage 5 (SRV-AGENT, SRV-PIPELINE)

- Per-lesson cost of one loop: about $2.7–3.0 cold, and under 10 minutes for a 25-page chapter.
- Put the source first, as an identical prefix in every stage, and choose the cache TTL deliberately. Neither is measured.
- The fact-check must return an itemized verdict per claim; a summary-only report is a failure.
- The convergence policy (ask a) must be decided before SRV-PIPELINE's loop limit is set.
- `max_budget_usd` exists in `ClaudeAgentOptions` (0.2.186), but its stop was not exercised. SRV-AGENT's budget tests must exercise it.
- Delivering the token on stdin keeps it out of `docker inspect`. This is the pattern to compare with ADR-0011's `/run/secrets` in OPS-DEPLOY.
