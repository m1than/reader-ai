# Spike tutor-call — NOT RUN YET

**Question (SRV-TUTOR review round 1, blocker 1):** how many output and thinking tokens, how much wall time and how
many dollars does one tutor-shaped single-turn Sonnet 5.5 call take, with extended thinking off and with the CLI
default? The answers set SRV-TUTOR's `max_output_tokens`, reserve, `question_cap_usd` and NFR thresholds.

**Why it matters (measured elsewhere):** the compile-cost spike's Sonnet fact-check stages printed 113–2,134
characters but reported 4,601–6,617 output tokens (`spikes/compile-cost/results/*/[25]-fact_check.json`), so thinking,
not the visible answer, dominates output tokens with the default settings. A 1,200-token cap would truncate.

**Method:** `bash spikes/tutor-call/run.sh`. 5 synthetic questions (`synthetic.json`: English and Russian, one with a
cell, one the sources cannot answer), each called twice (thinking off via `MAX_THINKING_TOKENS=0`, and default),
`max_turns=1`, no tools, non-essential traffic off, output cap 8,000 tokens. Cap $0.60 from the owner's $30 test budget.

**Status:** not run. The runner reads the Keychain item `reader-ai/claude-oauth-token`; the owner allowed that only for
three named spikes (2026-11-20), and `security find-generic-password` is denied for the agent. It runs when the owner
runs it (one command) or extends the Keychain permission to tutor-call.

**Until then SRV-TUTOR uses provisional numbers** (marked as such in its `bounds`): thinking off for task `tutor`
(cross-spec SRV-AGENT), `max_output_tokens` 2,000, reserve and caps derived in `spec/fixtures/SRV-TUTOR/cost.yaml`.

## Results

(to be filled by the run: per call `wall_s`, `total_cost_usd`, `usage.output_tokens`, `output_tokens_details`,
`output_chars`, `json_ok`; summary total)
