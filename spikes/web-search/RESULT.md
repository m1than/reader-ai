# Spike web-search — RESULT

**Question (owner request 2026-11-23, source freshness check; owner decision: Claude's server-side web search):**
1. What is the server-side web search tool's exact name, version string, request shape, `max_uses`, domain options,
   citation shape and usage report, and what does one search cost?
2. Can the Claude Agent SDK used by SRV-AGENT (claude-agent-sdk 0.2.187, bundled CLI) use it through agent-proxy, or
   is a direct Messages API call through agent-proxy simpler?
3. What does one freshness check cost, and what is its worst case?

**Status (2026-11-23, round 2):**
- §§ 1–2: documentation facts, sourced below.
- § 5a: the zero-credit stub run is done (results in `spikes/agent-proxy-handshake/results/*-websearch*`).
- § 5b: the live call is approved by the owner (≤ $0.10, one search, Haiku 5.5). The owner runs
  `spikes/web-search/run-live.sh` himself; it has not run yet.
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

These token rates equal SRV-AGENT's `spec/fixtures/SRV-AGENT/pricing.yaml` source (cached 2026-11-03) for the three
models. SRV-AGENT's table has no per-search fee yet (proposed FR-AGENT-16). Whether the $0.01 fee draws from the
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

Still hypotheses:
- **H-WS-3** — t, the input tokens one backend search adds. A chosen planning bound `t_max` = 20,000 (S1's example
  shows 6,039 input tokens for one search including the prompt). **t cannot be enforced**: the API adds the results
  server-side. § 5b records t for every live search.
- **H-WS-6 (real upstream)** — `total_cost_usd` and `model_usage` equal the sum of the per-request usage that the
  proxy records, per model.
- **H-FRESH-2** — the search run passes the planned query verbatim (SRV-FRESH FR-FRESH-6 denies anything else). The
  denial rate is measured in § 5b.

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
the nested request too, § 3a). Planned prompt P_s ≤ 2,000 tokens; links L ≤ 1,500 tokens.

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
- Worst: 19,100 × $4/M + 4,000 × $10/M = **$0.116**. FR-AGENT-9 computes its reserve from the actual prompt.

**Per check, worst: $0.170 + $0.103 + $0.116 = $0.39** against the owner's run cap of $1.00. The cap therefore also
covers a retry, but SRV-FRESH retries a paid stage only after a $0 settle (FR-FRESH-12).

**Expected ≈ $0.27:**
- scan $0.13;
- 6 × (fee $0.01 + ≈ 6,000 tokens of results at Haiku rates ≈ $0.002) ≈ $0.07;
- report ≈ 12,000 × $4/M + 2,500 × $10/M ≈ $0.07.

If t exceeds t_max, a search run overshoots its reserve by (t − 20,000) × $0.20/M. Even t = 200,000 adds $0.036.
The run-cap check before the next call stops further spend.

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

**Expected spend** ≈ $0.012: one search $0.01 plus about 10,000 Haiku tokens. The worst case is refused by the proxy
above the cap. Record the spend in `budget.ledger` from `test_budget_usd`.

**Not verified:** a zero-credit dry run of `live.py` against the stub (with `DRY_UPSTREAM_PORT`) was refused by the
agent session's permission system. `live.py` compiles, and `run-live.sh` passes `bash -n`.
