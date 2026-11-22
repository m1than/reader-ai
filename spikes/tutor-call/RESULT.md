# Spike tutor-call

**Question (SRV-TUTOR review round 1, blocker 1):** how many output and thinking tokens, how much wall time and how
many dollars does one tutor-shaped single-turn Sonnet 5.5 call take, with extended thinking off and with the CLI
default? The answers set SRV-TUTOR's `max_output_tokens`, reserve, `question_cap_usd` and NFR thresholds.

**Run:** `results/20261122T220848/` (the agent, 2026-11-22, with the owner's permission `budget.tutor_spike_by_agent`).
10 calls, **$0.038299** in total, against a cap of $0.60 from the owner's $30 test budget.

## Method

`bash spikes/tutor-call/run.sh`: the compile-cost image (python 3.14, claude-agent-sdk), read-only root, cap-drop ALL,
2 GiB. 5 synthetic questions (`synthetic.json`: three English, one Russian, one with a cell, one the sources cannot
answer), each called twice: thinking off (`MAX_THINKING_TOKENS=0`) first, then the CLI default. Model
`claude-sonnet-5-5`, `max_turns=1`, `tools=[]`, `system_prompt` = `system.md` (816 bytes), user prompt = the JSON
document SRV-TUTOR FR-TUTOR-6 specifies, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, output cap 8,000 tokens.
No book text: the sources are synthetic.

## Results

| Q | Mode | Wall s | USD | Output tokens | Thinking tokens | Prompt tokens (cache write / read) | JSON valid |
| --- | --- | --- | --- | --- | --- | --- | --- |
| q1 | off | 3.42 | 0.006480 | 310 | 0 | 844 / 0 | yes |
| q1 | default | 3.08 | 0.003138 | 305 | 0 | 0 / 844 | yes |
| q2 | off | 2.23 | 0.005496 | 212 | 0 | 843 / 0 | yes |
| q2 | default | 2.65 | 0.002188 | 210 | 0 | 0 / 843 | yes |
| q3 (ru) | off | 2.38 | 0.005972 | 250 | 0 | 867 / 0 | yes |
| q3 (ru) | default | 2.26 | 0.002591 | 250 | 0 | 0 / 867 | yes |
| q4 (cell) | off | 2.07 | 0.005386 | 213 | 0 | 813 / 0 | yes |
| q4 (cell) | default | 2.09 | 0.002135 | 205 | 0 | 0 / 813 | yes |
| q5 (unanswerable) | off | 1.54 | 0.003904 | 92 | 0 | 745 / 0 | yes |
| q5 (unanswerable) | default | 3.50 | 0.001008 | 93 | 0 | 0 / 745 | yes |

Input tokens outside the cache were 2 per call; all cache writes were `ephemeral_1h`.

## Findings

1. **No thinking in either mode** for this call shape: `output_tokens_details.thinking_tokens` is 0 in all 10 calls,
   and outputs are 92–310 tokens. The compile spike's 4.6–6.6k output tokens for 113–2,134 printed characters came
   from long fact-check stages, not from this shape. SRV-TUTOR keeps thinking off anyway (it costs nothing and removes
   the risk), and `max_output_tokens` 2,000 is 6.5× the largest measured output.
2. **A custom `system_prompt` replaces the CLI's own system prompt.** Prompt tokens were 745–867, against
   SRV-AGENT's measured `cli_system_tokens` of 6,803 for stages that used the default prompt. The fixed overhead beyond
   our own bytes is about 410 tokens (from q1 vs q5: 99 tokens for 310 more bytes, so 0.32 tokens per ASCII byte and
   ≈ 411 tokens at zero bytes). Russian text ran at 1.9 bytes per token, ASCII JSON at about 3. This matters beyond the
   tutor: every SRV-AGENT stage that passes its own `system_prompt` saves the 6,803-token CLI prompt (input for
   V.cost-optimize and SRV-AGENT's reserve).
3. **Prices recompute to within $0.000004 per call.** For example, q1 off: 844 × $4.00/M (1-hour cache write) +
   310 × $10.00/M = $0.006476, reported $0.006480. No auxiliary request was billed with non-essential traffic off.
4. **The CLI writes the whole prompt to the 1-hour cache at 2× the input price**, even for a one-shot prompt that is
   never reused. The "off" calls ran first and paid that write; the "default" calls of the same question read it back
   (cache read $0.10/M), so the cost difference is cache, not thinking. For a tutor, whose prompt changes with every
   question, an uncached send at 1× input would halve the input cost (a V.cost-optimize lever, if the CLI allows it).
5. **Latency:** 1.5–3.5 s per call for 92–310 output tokens, end to end in the container.
6. **Format:** 10 of 10 outputs were valid JSON in the requested shape.

## What SRV-TUTOR takes from this

- `max_output_tokens` 2,000 (measured max 310), thinking off.
- Reserve: tokens ≤ `cli_overhead_tokens` (1,000, about 2.4× the measured ≈ 410) + system bytes + prompt bytes
  (measured 0.3–0.5 tokens per byte, so counting one token per byte is an upper bound).
  Worst case per attempt = (1,000 + 4,000 + 24,000) × $4.00/M + 2,000 × $10.00/M = **$0.136**; two attempts $0.272,
  so `question_cap_usd` = **$0.30**.
- Typical L02-shaped question (about 19,000 bytes of Russian sources, ≈ 10,000 tokens, ≈ 400 output tokens):
  about **$0.048**, an extrapolation from the measured rates: every new question writes its whole prompt to the
  1-hour cache (the first call of each question read nothing from the cache). NFR-TUTOR-2 measures it on L02.
- `call_deadline_s` 20 is about 6× the slowest measured call.

## Limits of this spike

- Prompts were short: at most 867 tokens, against about 10,000 for an L02-shaped question with 8 sources. Latency,
  output length and thinking behaviour at that size are not measured here; the SRV-TUTOR bench (NFR-TUTOR-1..3)
  measures them on L02 and records thinking tokens per call.
- Quotes were not verified: the outputs are not stored (numbers only), so whether the model's quotes pass SRV-TUTOR's
  verbatim check is unmeasured. The bench records the share of blocks with a verified quote.
- Thinking off was not shown to have an effect: thinking tokens were 0 with the default as well, for this short shape.
  The tutor keeps `MAX_THINKING_TOKENS=0` as a safeguard for longer prompts, not because this spike proved it needed.
