# Spike web-search — RESULT

**Question (owner request 2026-11-23, source freshness check; owner decision: Claude's server-side web search):**
1. What is the server-side web search tool's exact name, version string, request shape, `max_uses`, domain options,
   citation shape and usage report, and what does one search cost?
2. Can the Claude Agent SDK used by SRV-AGENT (claude-agent-sdk 0.2.187, bundled CLI) use it through agent-proxy, or
   is a direct Messages API call through agent-proxy simpler?
3. What does one freshness check cost, and what is its worst case?

**Status (2026-11-23):**
- §§ 1–2: documentation facts, sourced below.
- § 5a: the zero-credit stub run is done (results in `spikes/agent-proxy-handshake/results/*-websearch*`).
- § 5b: the owner ran the live call: Haiku 5.5, one search, **$0.012588**. H-WS-1, -2, -4, -6 (without the tier part)
  and H-FRESH-2 held, observed once; t ≈ 13,950 tokens.
- Owner decisions of 2026-11-23 are in ADR-0030, "Owner decisions".

## Sources (fetched 2026-11-23)

| Id | Source | Used for |
| --- | --- | --- |
| S1 | https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool | versions, parameters, response, citations, errors, usage, price |
| S2 | https://platform.claude.com/docs/en/agents-and-tools/tool-use/server-tools | `allowed_callers`, domain filtering rules, `pause_turn` |
| S3 | https://platform.claude.com/docs/en/about-claude/pricing | token rates, the web search fee |
| S4 | https://code.claude.com/docs/en/tools-reference (section "WebSearch tool behavior") | the CLI's built-in `WebSearch` tool |
| S5 | https://code.claude.com/docs/en/agent-sdk/python (`WebSearch` input/output, `ModelUsage`, hooks) | what the SDK exposes |
| S6 | https://code.claude.com/docs/en/agent-sdk/cost-tracking | what `total_cost_usd` covers |
| S7 | spikes/agent-proxy-handshake/RESULT.md and this spike's stub run (§ 5a) | the CLI's requests through `ANTHROPIC_BASE_URL` |
| S8 | spikes/tutor-call/RESULT.md, spikes/compile-cost/RESULT.md | measured overheads and characters per token |
| S9 | deep-reviewer round 1 (2026-11-23), read-only inspection of the pinned CLI binary | the CLI's other search paths and flags (§ 3a) |

## 1. The server tool (S1, S2)

| Fact | Value | Source |
| --- | --- | --- |
| Tool `name` | `web_search` (in the request's `tools`; the API runs it) | S1 |
| Versions | `web_search_20250305` (basic, Claude calls it directly); `web_search_20260209` (adds dynamic filtering: Claude calls search from a code-execution container); `web_search_20260318` (adds `response_inclusion`) | S1 |
| Models | all current models, incl. `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-haiku-5-5` | S1 front matter |
| `allowed_callers` | `20260209`+ default to `["code_execution_20260120"]` (dynamic filtering); `["direct"]` turns it off. `20250305` defaults to `["direct"]` | S1, S2 |
| Request shape | `{"type": "<version>", "name": "web_search", "max_uses"?: int, "allowed_domains"?: [..] or "blocked_domains"?: [..], "user_location"?: {...}, "allowed_callers"?: [..]}` | S1 "Tool definition" |
| `max_uses` | limits searches in one **turn**; failed searches count; exceeding it gives a `web_search_tool_result` error `max_uses_exceeded` (direct calls) | S1 "Max uses" |
| Domains | `allowed_domains` **or** `blocked_domains`, never both (400); bare domains, optional path, no scheme; subdomains included; request lists must be a subset of the organisation's Console list; ASCII-only names (homograph warning) | S1, S2 |
| Result block | `web_search_tool_result` with `content` = list of `web_search_result {url, title, page_age, encrypted_content}`; on error one object `{type: "web_search_tool_result_error", error_code}` | S1 |
| Error codes | `too_many_requests`, `invalid_tool_input`, `max_uses_exceeded`, `query_too_long`, `request_too_large`, `unavailable`; all inside a 200 response | S1 |
| Disabled for the org | a 400 `invalid_request_error` saying web search is not enabled | S1 |
| Citations | always on; `citations: [{type: "web_search_result_location", url, title, encrypted_index, cited_text}]`, `cited_text` ≤ 150 characters | S1 |
| Display rule | "When displaying API outputs directly to end users, citations must be included to the original source" | S1 |
| `pause_turn` | a long server-side loop can stop with `stop_reason: "pause_turn"` | S1, S2 |
| Usage | `usage.server_tool_use.web_search_requests` | S1 |
| Billed tokens | "Search results count as input tokens, both while Claude keeps searching within a turn and in later conversation turns" | S1, S3 |

**Pin:** `web_search_20250305`. It is what the CLI sends (§ 5a), and it needs no code-execution container, which is
"not eligible for ZDR" under dynamic filtering (S1). **Any other version needs a new deep-reviewer round** before
the proxy pin changes.

## 2. Price (S3)

| Item | Rate | Source |
| --- | --- | --- |
| Web search | **$10 per 1,000 searches = $0.01 per search**; a failed search is not billed; one search is one use regardless of result count | S1, S3 |
| Claude Sonnet 5.5 | input $2, 5-min cache write $2.50, 1-h cache write $4, cache read $0.10, output $10 per MTok | S3 |
| Claude Opus 5.5 | input $4, 5-min write $5, 1-h write $8, read $0.20, output $20 per MTok | S3 |
| Claude Haiku 5.5 (≤ 100k-token prompts) | input $0.10, 5-min write $0.125, 1-h write $0.20, read $0.01, output $0.50 per MTok | S3 |
| Claude Haiku 5.5 (> 100k-token prompts) | input $0.50, 5-min write $0.625, 1-h write $1.00, read $0.05, output $2.50 per MTok | S3 |

These token rates equal SRV-AGENT's `spec/fixtures/SRV-AGENT/pricing.yaml` source (cached 2026-11-03) for Opus and
Sonnet. That table has no Haiku 5.5 row and no per-search fee yet; proposed FR-AGENT-16 adds both. A search run's
prompt stays far below 100k tokens (§ 4), so the lower Haiku tier applies. Whether the $0.01 fee draws from the
subscription's Agent SDK credit is not documented. § 5b asks the owner to compare his credit before and after the
live call.

## 3. Agent SDK or a direct Messages call?

### 3a. What the SDK and the CLI do

Documented (S4–S6):
- The CLI has no pass-through for raw server tools. Its built-in **`WebSearch`** "returns result titles and URLs. It
  doesn't fetch the result pages", "may issue up to eight backend searches per call", and accepts `allowed_domains`
  or `blocked_domains` (S4).
- Per-turn search limit in SDK runs: `CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION` (S4).
- `ModelUsage.webSearchRequests`; per-request fees are part of the SDK's cost estimate (S5, S6).
- `PreToolUse` hooks can deny a tool call before it runs (S5).

Measured by the stub run (§ 5a), now facts for claude-agent-sdk 0.2.187 (bundled CLI `cc_version=2.1.330.355`):

| Item | Observed |
| --- | --- |
| H-WS-1: request kinds | **Holds.** A `WebSearch` call makes a **nested** `POST /v1/messages?beta=true` through `ANTHROPIC_BASE_URL`, between two main-loop requests. No other host, no other path |
| Main-loop `tools` | **two** elements: `DeferredToolPlaceholder` (`defer_loading: true`, 83-char description, input_schema sha256 `efddc7bd…a49`) and `WebSearch` (keys `name`, `description`, `input_schema`; no `type`; description 334 chars, ends with an instruction to list sources "as markdown links" and says "US-only"; input_schema sha256 `ce035ada7312377e4219f1f4d74d325e1db5b9e3b9c4bf9b8e46e7e119b7cbbb`: `query` (string, minLength 2), `allowed_domains`, `blocked_domains`, `additionalProperties: false`). This CLI's description contains no month: "The current month is (provided in the conversation below)" |
| Main-loop body | keys `context_management` (`clear_thinking_20251015`), `max_tokens`, `messages`, `metadata` (`user_id`), `model`, `output_config` (`effort: medium`), `stream`, `system`, `thinking`, `tools`; **`thinking: {type: adaptive, display: updates}` even with `MAX_THINKING_TOKENS=0`** (relevant to V.agent-thinking) |
| Nested body | keys `max_tokens`, `messages`, `metadata`, `model`, `output_config` (`effort: medium`), `stream`, `system`, `tool_choice` (`{type: auto}`), `tools`; **no `thinking` key, no `cache_control`** (uncached); `tools` = exactly `[{type: "web_search_20250305", name: "web_search", max_uses: 8}]`; system = 3 text blocks (a billing header line, "You are a Claude agent…", "You run a web search for another Claude model… Search once, then reply with a brief summary"); user message `Perform a web search for the query: <query>` |
| H-WS-2: nested model | **Holds in this configuration:** the nested model equals the main model (Sonnet 5.5 and Haiku 5.5 runs) |
| `max_tokens` | `CLAUDE_CODE_MAX_OUTPUT_TOKENS` sets **both** the main and the nested `max_tokens` (4000 → 4000; unset → 128000) |
| Tool result to the model | text `Web search results for query: "…"\n\nLinks: [{title,url},…]\n\n<nested summary>\n\nREMINDER: You MUST include the sources above in your response to the user using markdown hyperlinks.` |
| Structured result to the worker | `UserMessage.tool_use_result = {query, results: [{tool_use_id, content: [{title, url}]}, "<nested summary>"], durationSeconds, searchCount}`. This is the source of the link set R. `page_age` does not reach the worker |
| Search error | `results: ["Web search error: max_uses_exceeded", "<summary>"]`, `webSearchRequests` 0 |
| Hook deny | `PreToolUse` `permissionDecision: deny` → no nested request; the model gets `is_error` tool_result `PreToolUse:WebSearch hook error: <reason>` and takes one more main turn. Works without `allowed_tools` and without `can_use_tool` (string prompt) |
| H-WS-4: fee in SDK cost | **Holds against the stub:** `total_cost_usd` 0.01021 = tokens + $0.01; `model_usage[model].webSearchRequests` = 1 |
| H-WS-6: what `usage` covers | **`ResultMessage.usage` covers the main loop only** (input 20 = 2 main requests; `web_search_requests` 0). **`model_usage` and `total_cost_usd` include the nested request** (input 30 = 3 requests). Settlement and the cross-check must use `total_cost_usd` / `model_usage`, not `usage`. To be confirmed against real usage in § 5b |

From the deep-reviewer's read of the binary (S9), **not observed on the wire** in these runs and therefore outside
the pin:
- a `shouldDefer` tool flag (it becomes `defer_loading` on the wire, as seen on `DeferredToolPlaceholder`);
- a `search_profile` field;
- a small-model flag `tengu_plum_vx3` that may move the nested request to another model;
- three other search paths: search-elsewhere, `searchViaSearchService` and a ccr-proxy;
- an auxiliary request.

Any of them changes a body shape or model away from the pin, and **agent-proxy refuses it** (403 `tools_mismatch` or
`model_mismatch`, SRV-AGENT proposed FR-AGENT-14). The search run then fails and settles by FR-AGENT-9's record rule:
no 2xx → $0; a 2xx before the refusal → that call's reserve.

Hypotheses, with the live call's single observation (§ 5b):
- **H-WS-3** — t, the input tokens one backend search adds. A chosen planning bound `t_max` = 20,000 (S1's example
  shows 6,039 input tokens for one search including the prompt). **Observed once: t ≈ 13,950** (14,452 nested input).
  **t cannot be enforced**: the API adds the results server-side. FR-FRESH-14 records t for every live search.
- **H-WS-6 (real upstream)** — `total_cost_usd` and `model_usage` equal the sum of the per-request usage that the
  proxy records, per model; and `total_cost_usd` knows Haiku 5.5's two tiers (a request over 100k prompt tokens
  priced at the higher tier). **Observed once: the first part holds** (SDK = proxy to the cent, 18,518 input-side
  tokens each). The tier part stays open until a request over 100k is observed; until then SRV-AGENT's cross-check
  flags such requests (FR-AGENT-16).
- **H-FRESH-2** — the search run passes the planned query verbatim (SRV-FRESH FR-FRESH-6 denies anything else).
  **Observed once: 1 of 1 verbatim**, denial rate 0.0.

### 3b. A direct Messages API call through agent-proxy

One request, structured `page_age` and citations, one body shape. **But** it needs an `anthropic-api` key, or a use
of the subscription token outside Claude Code and the Agent SDK that is not documented (ADR-0005's terms). The owner
chose the subscription-only path on 2026-11-23 (ADR-0030).

### 3c. Verdict (owner-confirmed 2026-11-23)

The Agent SDK `WebSearch` path, with dates labelled "as reported".

The design that bounds its worst case (§ 4):
- each planned query runs as its **own** short SDK run with one search: task `freshness_web`, Haiku 5.5, `max_turns`
  2, one search allowed;
- the report is written by a separate **tool-less** call: task `freshness_report`, Sonnet 5.5.

So there is no long multi-turn tool loop to bound.

## 4. Cost per check (formulas from the bounds; expected values not measured)

Rates per MTok from § 2. Prompt tokens are bounded by characters / 2.0, SRV-AGENT's reserve floor; 2.5 chars/token
was measured (S8). Worst cases price every prompt token at the 1-hour cache-write rate (no cache hits), except the
nested search request, which is uncached (§ 3a) and is still priced at the 1-hour rate for safety.

**Stage (a) scan** (Sonnet 5.5, one call, no tools; sample ≤ 60,000 chars):
- reserve: (1,000 + ceil(63,000 / 2.0)) × $4/M + 4,000 × $10/M = **$0.170**;
- expected: ≈ 25,200 × $4/M + 2,600 × $10/M ≈ $0.13.

**Stage (b) one search run** (Haiku 5.5, `max_turns` 2, output cap O_s = 1,000 tokens per request, which applies to
the nested request too, § 3a). Planned prompt P_s ≤ 2,000 tokens. Links L ≤ 1,500 tokens is a planning bound for the
CLI's own tool-result text in this Haiku run. We do not format that text, so it is not enforced here: at 10× this
bound the run would cost $0.003 more.

| Part | Formula | Worst |
| --- | --- | --- |
| Main input | P_s + (P_s + O_s + L + O_s) = 7,500 tokens × $0.20/M | $0.0015 |
| Main output | 2 × O_s × $0.50/M | $0.0010 |
| Nested input | (500 + t_max) × $0.20/M = 20,500 × $0.20/M | $0.0041 |
| Nested output | O_s × $0.50/M | $0.0005 |
| Fee | 1 search (proxy-enforced) | $0.0100 |
| **Per run** | | **$0.0171 → reserve floor $0.02** |

Six runs: ≤ $0.103.

**Stage (c) report** (Sonnet 5.5, one call, no tools; output cap 4,000):
- Prompt ≤ 1,500 instructions + 300 bibliography + 40 topics × 40 + 20 claims × 40 + 6 × (L 1,500 + summary 750 +
  query 60) + 1,000 CLI overhead ≈ 19,100 tokens.
- Here L **is enforced by construction** (SRV-FRESH FR-FRESH-7): at most 10 results per search, each rendered as
  `r<k> host title` with host ≤ 100 and title ≤ 120 characters (≤ 240 characters per result). That is ≤ 2,400
  characters, so ≤ 1,200 tokens at 2.0 characters per token. The model cites `r<k>` ids, never URLs.
- Worst: 19,100 × $4/M + 4,000 × $10/M = **$0.116**. FR-AGENT-9 computes its reserve from the actual prompt.

**Per check, worst: $0.170 + $0.103 + $0.116 = $0.39** against the owner's run cap of $1.00.
- Retries: a call is retried only after a $0 settle, except the report call, which may have one paid retry.
- With it: $0.39 + $0.116 = $0.51.
- The cap would even fit one paid retry of every stage ($0.78).
- A reservation without a settle row counts as paid, so it is never retried (FR-FRESH-12).
- A worker restart restarts the web job: at most one more web stage, ≤ $0.22.
- **Combined worst case** (a restart plus a paid report retry in each attempt): $0.170 + 2 × ($0.103 + 2 ×
  $0.116) ≈ $0.84. With the reserve floors ($0.02 per search run): $0.170 + 2 × ($0.12 + 2 × $0.116) ≈ $0.87. Both
  are under $1.00 (FR-FRESH-12).

**Expected ≈ $0.27:**
- scan $0.13;
- 6 × (fee $0.01 + ≈ 6,000 tokens of results at Haiku rates ≈ $0.002) ≈ $0.07;
- report ≈ 12,000 × $4/M + 2,500 × $10/M ≈ $0.07.

**Tail: t above t_max.** If t exceeds t_max, a search run overshoots its reserve, and past 100,000 prompt tokens
Haiku 5.5's **tier jump** applies (§ 2). A nested prompt over 100k tokens is billed in full at the higher tier:
$0.50/M plain input, or $1.00/M as a 1-hour write, which this uncached request does not use. At t = 200,000 that
request costs ≈ 200,500 × $0.50/M ≈ $0.10, plus output and the fee. That is not the +$0.036 the lower tier would
give.

This tail cannot be enforced: the API adds the results server-side. It is bounded per call only by what one search
returns. The run-cap check before the next call stops any further spend.

The daily cap of $2.00 holds ≥ 5 worst-case checks. It relies on one running check per user and worker concurrency 1
(SRV-EVENTS FR-EVENTS-9).

## 5. Runs

### 5a. Stub run — done, zero credit (2026-11-23)

Command, from the repository root:

```
STUB_SCENARIO=websearch MODES="websearch websearch-deny" TAG=websearch bash spikes/agent-proxy-handshake/run.sh
```

Variants:
- `MAX_OUT=4000 TAG=websearch-maxout`
- `MODEL=claude-haiku-5-5 TAG=websearch-haiku`
- `STUB_WS_ERROR=max_uses_exceeded TAG=websearch-error`

Setup:
- the container runs with `--network none` and a fresh random handle, never a real token;
- the stub answers the first main request with a `WebSearch` tool_use, the nested request with a synthetic
  `server_tool_use` + `web_search_tool_result`, and the rest with a final text;
- the probe gates through a `PreToolUse` hook.

Results: `spikes/agent-proxy-handshake/results/20261123T2055{49,53,56,58}-websearch*/`. Findings: § 3a.

### 5b. Live call — approved by the owner; run by the owner

Command: `bash spikes/web-search/run-live.sh` (owner only: it reads the Keychain item and pipes it on stdin into the
container, the pattern of `spikes/tutor-call/run.sh`). It runs `spikes/web-search/live.py` in the 0.2.187 image:
- **Recording reverse proxy** on 127.0.0.1:8080 in front of `https://api.anthropic.com`.
  - It forwards only `POST /v1/messages`, a fixed header allow-list, and Haiku and Sonnet 5.5 bodies.
  - It rewrites the nested `max_uses` to 1 and allows one search.
  - It refuses any request whose worst case would pass the remaining cap ($0.10 minus earlier runs).
  - It records, per request: kind, model, body keys, tool shapes, `thinking`, `tool_choice`, whether `cache_control`
    is present, status, the SSE event names, content-block types and key names, the full usage numbers, result counts
    and how many results carry `page_age`.
- **One SDK run** on Haiku 5.5, `max_turns` 4, `CLAUDE_CODE_MAX_OUTPUT_TOKENS` 1000, one search allowed, a
  `PreToolUse` gate, `max_budget_usd` = the cap.
  - The question: does *Mastering Blockchain* (Imran Bashir, 4th ed., Packt, 2023) have a newer edition? Public
    metadata only.
  - The system prompt gives one code-built query (FR-FRESH-6). The gate allows only that query verbatim, with no
    domain lists, and records the verbatim rate (H-FRESH-2) plus, for comparison, how many tokens fall outside the
    metadata's vocabulary.
- **Output:** `results/<run>/live.json`, with numbers, key names and shapes only. No token, header value, prompt,
  query text, title, URL or model text.

| Hypothesis | Recorded as |
| --- | --- |
| H-WS-1 | a `search` request seen, its shape |
| H-WS-2 | the nested request's model |
| H-WS-3 | the nested request's `input_tokens` per search (t + ≈ 500) |
| H-WS-4 | SDK cost − the proxy's token-only sum (≈ $0.01 if the fee is included) |
| H-WS-6 | the per-model proxy sums vs `model_usage`, and SDK vs proxy dollars |
| H-FRESH-2 | the gate's denial rate (one query) |
| `max_uses` rewrite accepted | the nested request's HTTP status |
| SSE names | events and content-block key names |
| US-only | the WebSearch description says "US-only": a `web_search_tool_result_error` or zero results would show it |
| Fee and the subscription credit | a manual comparison by the owner, printed at the end of the run |

One call gives one observation per hypothesis. Results are recorded as "observed once", not as established, and
FR-FRESH-14's live check records t and denials again for every search.

**Expected spend** ≈ $0.012: one search $0.01 plus about 10,000 Haiku tokens. The worst case is refused by the proxy
above the cap. Record the spend in `budget.ledger` from `test_budget_usd`.

**Not verified before the run:** a zero-credit dry run of `live.py` against the stub (with `DRY_UPSTREAM_PORT`) was
refused by the agent session's permission system. `live.py` compiled, and `run-live.sh` passed `bash -n`.

#### Result — observed once (owner run, 2026-11-23, script at d9f96f0)

File: `results/20261123T214429/live.json`. It holds numbers, key names and shapes only, with no text, title, URL,
query or header value, and it is committed.

The run: Haiku 5.5, 9.7 s wall, `terminal_reason: completed`, 2 SDK turns, 3 upstream requests, 0 refused, no
error. **Spend: $0.012588** (SDK `total_cost_usd` 0.01258811 = the proxy's sum 0.012588), recorded in `budget.ledger`
from `test_budget_usd`.

| Request | Usage | Other |
| --- | --- | --- |
| main 1 | input 2, 1-h write 1,281, output 76 (thinking 0) | stop `tool_use`; `cache_control` present; $0.000294 |
| search (nested) | **input 14,452**, no cache, output 835 (**thinking 290**), `web_search_requests` 1 | 10 results, 5 with `page_age`; `max_uses` sent 8 → rewritten to 1, **status 200**; blocks: thinking, `server_tool_use`, `web_search_tool_result`, thinking, 11 text; $0.011863 incl. the $0.01 fee |
| main 2 | input 2, 1-h write 1,500, cache read 1,281, output 236 (**thinking 152**) | stop `end_turn`; $0.000431 |

| Hypothesis | Observed once |
| --- | --- |
| H-WS-1 nested request | **holds:** one nested search request through the base URL, in the stub's shapes (tool `web_search_20250305` with `max_uses` 8, `tool_choice` auto, no `thinking` key, no `cache_control`; main loop `DeferredToolPlaceholder` + `WebSearch`) |
| H-WS-2 nested model | **holds:** `claude-haiku-5-5`, equal to the main model |
| H-WS-3 t | **t ≈ 13,950** (14,452 nested input minus ≈ 500 of the CLI's nested prompt), under the chosen `t_max` of 20,000 (×1.43 margin) |
| H-WS-4 fee in SDK cost | **holds:** SDK − the proxy's token-only sum = $0.0100 |
| H-WS-6 SDK = proxy | **holds for this run:** `model_usage` input 14,456 + cache write 2,781 + read 1,281 = 18,518 = the proxy's per-request sum; output 1,147 both; 1 search both; dollars equal. `ResultMessage.usage` again covered the main loop only (`web_search_requests` 0). The tier part stays open (all prompts < 100k) |
| H-FRESH-2 verbatim query | **holds:** 1 of 1 query verbatim (68 characters, 0 tokens outside the metadata vocabulary), no domain lists, denial rate 0.0 |
| `max_uses` rewrite | **accepted** (200) by the real upstream |
| SSE names | events `message_start`, `content_block_start`/`_delta`/`_stop`, `message_delta`, `message_stop`, `ping`; `web_search_result` keys `encrypted_content`, `page_age`, `title`, `type`, `url`; `web_search_tool_result` and `tool_use` carry a `caller` key; citation keys `cited_text`, `encrypted_index`, `title`, `type`, `url` |
| SDK structured result | `tool_use_result` keys `durationSeconds`, `query`, `results`, `searchCount`; 10 links and a 1,125-character summary (under `summary_chars_max` 1,500) |
| US-only | results came back (10, no error code) for the owner's account |
| Fee and the subscription credit | **unknown**: the owner has not reported the credit comparison |

**Side findings:**
- **Thinking.** Thinking ran with `MAX_THINKING_TOKENS=0`: 152 tokens on the main loop, which sent
  `thinking: {type: adaptive}`. The nested request has no `thinking` key, so Haiku 5.5 runs adaptive thinking by
  default (290 tokens). This is evidence for V.agent-thinking. Thinking tokens are billed inside `output` and are
  bounded by the output cap.
- **Nested output near its cap.** The nested output used 835 of the 1,000-token cap. A longer reply would stop at
  `max_tokens` and shorten the summary. That cannot raise the cost, because the cap is already in the formula.

**Effect on spec numbers:**
- No bound changes. `t_max_tokens` 20,000 stays: one observation at ≈ 13,950 does not justify lowering it, and it
  remains a planning bound, not a limit. `search_output_tokens` 1,000 stays: it is a cost cap, and 835 fits.
- The expected cost per search run was ≈ $0.012; observed $0.0126. So 6 runs are ≈ $0.076 and the per-check
  estimate stays ≈ $0.27.
- The worst-case formulas (§ 4) already price output at the cap and the main loop at the 1-hour rate observed here,
  so ≈ $0.39 per check and ≈ $0.84–0.87 combined stand.
