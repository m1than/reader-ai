# Spike web-search — RESULT (desk research; no credit spent)

**Question (owner request 2026-11-23, source freshness check; owner decision: Claude's server-side web search):**
1. What is the server-side web search tool's exact name, version string, request shape, `max_uses`, domain options,
   citation shape and usage report, and what does one search cost?
2. Can the Claude Agent SDK used by SRV-AGENT (claude-agent-sdk 0.2.187, bundled CLI) use it through agent-proxy, or
   is a direct Messages API call through agent-proxy simpler?
3. What does one freshness check cost?

**Status:** facts from the documentation, fetched 2026-11-23 (sources below). Nothing was run. Two runs are planned
and not executed: a zero-credit stub run and one live call under $0.10 that waits for the owner's approval (§ 5).

## Sources (fetched 2026-11-23)

| Id | Source | Used for |
| --- | --- | --- |
| S1 | https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool | versions, parameters, response, citations, errors, usage, price |
| S2 | https://platform.claude.com/docs/en/agents-and-tools/tool-use/server-tools | `allowed_callers`, domain filtering rules, `pause_turn` |
| S3 | https://platform.claude.com/docs/en/about-claude/pricing | token rates, the web search fee, free code execution with web tools |
| S4 | https://code.claude.com/docs/en/tools-reference (section "WebSearch tool behavior") | the CLI's built-in `WebSearch` tool |
| S5 | https://code.claude.com/docs/en/agent-sdk/python (`WebSearch` input/output, `ModelUsage`, `can_use_tool`, hooks) | what the SDK exposes |
| S6 | https://code.claude.com/docs/en/agent-sdk/cost-tracking | what `total_cost_usd` covers |
| S7 | spikes/agent-proxy-handshake/RESULT.md (this repository) | the CLI's request shape through `ANTHROPIC_BASE_URL` |
| S8 | spikes/tutor-call/RESULT.md, spikes/compile-cost/RESULT.md | measured overheads and characters per token |

## 1. The server tool (S1, S2)

| Fact | Value | Source |
| --- | --- | --- |
| Tool `name` | `web_search` (in the request's `tools`; the API runs it) | S1 |
| Versions | `web_search_20250305` (basic, Claude calls it directly); `web_search_20260209` (adds dynamic filtering: Claude calls search from a code-execution container); `web_search_20260318` (adds `response_inclusion`) | S1 |
| Models | all current models, incl. `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-haiku-5-5` | S1 front matter |
| `allowed_callers` | `20260209`+ default to `["code_execution_20260120"]` (dynamic filtering); `["direct"]` turns it off. `20250305` defaults to `["direct"]` | S1, S2 |
| Request shape | `{"type": "<version>", "name": "web_search", "max_uses"?: int, "allowed_domains"?: [..] or "blocked_domains"?: [..], "user_location"?: {"type": "approximate", city?, region?, country?, timezone?}, "allowed_callers"?: [..]}` | S1 "Tool definition" |
| `max_uses` | limits searches in one **turn**; failed searches count; exceeding it gives a `web_search_tool_result` error `max_uses_exceeded` (direct calls) | S1 "Max uses" |
| Domains | `allowed_domains` **or** `blocked_domains`, never both (400); bare domains with an optional path, no scheme; subdomains included; wildcards only in the path; invalid formats 400; request lists must be a subset of the organisation's Console list; use ASCII-only names (homograph warning) | S1, S2 "Domain filtering" |
| Result block | `web_search_tool_result` with `content` = list of `web_search_result {url, title, page_age, encrypted_content}`; on error `content` is one object `{type: "web_search_tool_result_error", error_code}` | S1 "Response", "Errors" |
| Error codes | `too_many_requests`, `invalid_tool_input`, `max_uses_exceeded`, `query_too_long`, `request_too_large`, `unavailable`; all inside a 200 response | S1 "Errors" |
| Disabled for the org | a 400 `invalid_request_error` saying web search is not enabled | S1 |
| Citations | always on; `text` blocks carry `citations: [{type: "web_search_result_location", url, title, encrypted_index, cited_text}]`; `cited_text` is up to 150 characters of the page; `url`, `title`, `cited_text` are not billed as tokens | S1 "Citations" |
| Display rule | "When displaying API outputs directly to end users, citations must be included to the original source" | S1 |
| Multi-turn | `encrypted_content` / `encrypted_index` must be sent back unchanged; a modified one is a 400 | S1 |
| `pause_turn` | a long server-side loop can stop with `stop_reason: "pause_turn"`; continue by re-sending the assistant content | S1, S2 |
| Usage | `usage.server_tool_use.web_search_requests` (count of searches) | S1 "Usage and pricing" |
| Billed tokens | "Search results count as input tokens, both while Claude keeps searching within a turn and in later conversation turns" | S1, S3 |

**Choice for reader-ai:** pin `web_search_20250305` (direct calls only). Dynamic filtering adds a code-execution
container on Anthropic's side, which the check does not need; it is also "not eligible for ZDR" (S1), and the
code-execution caller ends its run on a `max_uses` overrun instead of returning `max_uses_exceeded` (S1). If the
CLI's own WebSearch sends another version, § 4's stub run shows it and the pin follows what the CLI sends (H-WS-1).

## 2. Price (S3)

| Item | Rate | Source |
| --- | --- | --- |
| Web search | **$10 per 1,000 searches = $0.01 per search**; a search that fails with an error is not billed; each search is one use regardless of the result count | S1, S3 |
| Code execution | free when used with `web_search_20260209`+ (not used here) | S3 |
| Claude Sonnet 5.5 | input $2, 5-min cache write $2.50, 1-h cache write $4, cache read $0.10, output $10 per MTok | S3 |
| Claude Opus 5.5 | input $4, 5-min write $5, 1-h write $8, read $0.20, output $20 per MTok | S3 |
| Claude Haiku 5.5 (≤ 100k-token prompts) | input $0.10, 5-min write $0.125, 1-h write $0.20, read $0.01, output $0.50 per MTok | S3 |

These token rates equal SRV-AGENT's `spec/fixtures/SRV-AGENT/pricing.yaml` source (cached 2026-11-03) for the three
models; SRV-AGENT's table has no per-search fee yet (SRV-AGENT amendment FR-AGENT-16).

## 3. Agent SDK or a direct Messages call?

### 3a. What the SDK offers (S4, S5, S6)

- The CLI has no pass-through for raw server tools: `ClaudeAgentOptions.tools` names the CLI's built-in tools. Its
  built-in **`WebSearch`** tool "runs a query against Anthropic's web search backend and returns result titles and
  URLs. It doesn't fetch the result pages", "may issue up to eight backend searches per call", and accepts
  `allowed_domains` or `blocked_domains` (S4).
- `WebSearch` input: `{query, allowed_domains?, blocked_domains?}`; output to the model:
  `{query, results: [str | {tool_use_id, content: [{title, url}]}], durationSeconds}` (S5). **`page_age` and
  `cited_text` do not reach the main loop**: dates the model reports are its own unless they appear in the titles or
  in the text entries.
- In `-p` and Agent SDK runs the CLI's search limit is per turn (`CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION`) (S4).
- `ModelUsage` has `webSearchRequests`; `ResultMessage.usage.server_tool_use.web_search_requests` exists (S5). S6
  says per-request fees "such as web search" are part of the SDK's cost estimate (they are not multiplied by the
  data-residency factor), so `total_cost_usd` is expected to include the $0.01 fees (H-WS-4).
- `can_use_tool` (called when a tool is not pre-approved) and `PreToolUse` hooks can inspect and deny a tool call
  before it runs (S5). This is a worker-side gate on every query before any search is sent.

**Hypotheses (not documented; § 5 measures them):**
- **H-WS-1** — the CLI's `WebSearch` makes a **nested** `POST /v1/messages` through `ANTHROPIC_BASE_URL`, whose body
  carries `tools: [{type: "web_search_<version>", name: "web_search", max_uses: 8, …}]` and the query; the main-loop
  request carries `WebSearch` as a client tool definition. So a freshness run through agent-proxy makes ≥ 2 kinds of
  request per run_token.
- **H-WS-2** — the nested request uses the same model as the main loop. If it uses another (a small model), the
  run_token's model binding (SRV-AGENT FR-AGENT-4) must allow that second, pinned id for freshness tokens.
- **H-WS-3** — one backend search adds `t` input tokens to the nested request; S1's own example shows 6,039 input
  tokens for one search including the prompt. Chosen planning range: `t` = 3,000–20,000; expected 6,000.
- **H-WS-4** — `total_cost_usd` includes $0.01 per search.
- **H-WS-5** — the nested response's `web_search_result` blocks (with `page_age`) pass through agent-proxy, so the
  proxy could record `{url, title, page_age}` per run_token for link checks; not needed if the worker validates
  recommendation URLs against the tool results it sees (SRV-FRESH FR-FRESH-8).

### 3b. A direct Messages API call through agent-proxy

Simpler on the wire: one request with `tools: [{"type": "web_search_20250305", "name": "web_search", "max_uses": n}]`,
structured `page_age` and citations, an exact `web_search_requests` count, no nested requests, and the proxy checks one
body. **But** the only provider reader-ai has is `anthropic-subscription` (ADR-0005). ADR-0005 records Anthropic's
terms: subscription OAuth tokens are intended for Claude Code and Anthropic's apps, and Agent SDK use draws from the
monthly Agent SDK credit. A hand-built Messages request carrying the subscription token is neither Claude Code nor the
Agent SDK. Whether the upstream accepts it, and whether it is allowed and billed to the Agent SDK credit, is not
documented (not tested; a test would itself be the questionable use). The direct path is clean only with an
`anthropic-api` key provider, which SRV-AGENT defers and which spends outside the subscription (ADR-0005: "no API
spending here").

### 3c. Verdict

| | Agent SDK `WebSearch` through agent-proxy | Direct Messages call through agent-proxy |
| --- | --- | --- |
| Credentials | the subscription, as today (ADR-0005 holds) | needs an `anthropic-api` key (new provider, new spend) or an undocumented use of the subscription token |
| Requests per check | main loop (≤ `turns_max`) + one nested request per search call | one (plus `pause_turn` continuations) |
| Proxy checks | two body shapes per freshness token (H-WS-1), `max_uses` rewritten per nested request | one body shape |
| Query gate before a search runs | yes: `can_use_tool` / `PreToolUse` in the worker | none: the model searches inside Anthropic's loop; only the prompt's content limits queries |
| Dates and citations | titles and URLs only; dates are the model's claims | `page_age` and `cited_text` structured |
| Cost reporting | SDK `total_cost_usd` (H-WS-4) | `usage` computed by our pricing table |

**Recommendation: the Agent SDK path.** It keeps ADR-0005 and the credential model unchanged and gives a query gate
that the direct path lacks. Its costs: a stub run to pin the request shapes (§ 5a) and recommendation dates that are
labelled "as reported", not verified. The direct path becomes the better choice if the owner adds an `anthropic-api`
provider (owner question 1 in ADR-0030).

## 4. Cost per check (estimate from S3 rates and the bounds in SRV-FRESH; not measured)

Model `claude-sonnet-5-5` for both stages. The CLI writes prompts to the 1-hour cache (S8: SRV-AGENT prices reserves
at `cache_write_1h`), so writes are priced at $4/MTok; characters per token 2.5 measured (S8), 2.0 for reserves.

**Stage (a), dated-claims scan, one call, no tools.** Sample ≤ 60,000 characters of spans (SRV-FRESH
`bounds.sample_chars_max`) ≈ 24,000 tokens, plus ≈ 1,200 tokens of system prompt and overhead.
- Expected: 25,200 × $4/M = $0.101 input + ≈ 2,600 output × $10/M = $0.026 → **≈ $0.13**.
- Reserve (SRV-AGENT FR-AGENT-9): (1,000 + ceil(63,000 / 2.0)) × $4/M + 4,000 × $10/M = $0.130 + $0.040 → **$0.17**.

**Stage (b), web check, Agent SDK path, `s` searches (≤ `searches_max` = 6), proxy cap of 1 backend search per
nested request.** Main loop: prompt P ≈ 4,000 tokens (system, metadata, ≤ 40 outline topics, ≤ 20 claim subjects)
plus the `WebSearch` definition (≤ 1,000, H) → turn 1 writes 5,000; each later turn writes ≈ 1,500 (tool call and
result) and reads the rest; final report ≤ 3,000 output tokens. Nested request per search: ≈ 500 + `t` input tokens,
≈ 500 output tokens, $0.01 fee.

| Case | Main input | Main output | Nested (× s) | Stage (b) |
| --- | --- | --- | --- | --- |
| s = 3, t = 3,000, plain input rate | $0.020 + 3 × $0.006 + $0.002 = $0.040 | 3,300 × $10/M = $0.033 | 3 × ($0.007 + $0.005 + $0.01) = $0.066 | **$0.14** |
| s = 6, t = 6,000, plain input rate | $0.020 + 6 × $0.006 + $0.005 = $0.061 | 3,600 × $10/M = $0.036 | 6 × ($0.013 + $0.005 + $0.01) = $0.168 | **$0.27** |
| s = 6, t = 20,000, 1-h write rate (worst planned) | $0.061 | $0.036 | 6 × ($0.082 + $0.005 + $0.01) = $0.582 | **$0.68** |

**Per check:** expected **≈ $0.27–0.40** (stage a $0.13 + stage b $0.14–0.27); worst planned **≈ $0.85**
(stage a reserve $0.17 + $0.68). Chosen bounds in SRV-FRESH: stage (b) reserve floor $0.80, run cap $1.00, daily cap
$2.00 per user. Without the proxy's one-search-per-request cap, six searches in one nested request would re-read
earlier results each iteration (S1: results count as input "while Claude keeps searching within a turn"): at t =
20,000, Σ i·t for i = 1..6 = 420,000 tokens ≈ $1.68 for that request alone, which is why the cap exists.

Cheaper lever, not chosen yet: stage (a) on `claude-haiku-5-5` costs ≈ 25,200 × $0.20/M + 2,600 × $0.50/M ≈ $0.006
(S3). Whether Haiku flags dated claims as well as Sonnet is a quality question for V.cost-optimize.

**Direct path, for comparison** (one request, `max_uses` 6, no caching): the server loop re-reads the context per
search, so over 7 iterations input ≈ 7 × 4,000 + t × (0 + 1 + … + 6) = 28,000 + 21t; at t = 6,000: 154,000 × $2/M
= $0.31 + $0.03 output + $0.06 fees ≈ $0.40.
Comparable to the SDK path; not a reason to choose it.

## 5. Planned runs (not executed)

### 5a. Stub run — zero credit (next step; the agent may run it)

Extend `spikes/agent-proxy-handshake` with a mode `websearch`: the stub upstream answers the first main-loop request
with an SSE `tool_use` for `WebSearch` (`{"query": "synthetic query"}`), answers a request whose `tools` hold a
`web_search_*` type with a canned `web_search_tool_result` (two results with `page_age`) and a short text, then
answers the next main-loop request with `end_turn`. `tools=["WebSearch"]`, `can_use_tool` allowing it,
`CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION=2`, `--network none`. Record per request: model, `tools` (types, names,
`max_uses`, domain keys), `max_tokens`, header names, which message holds the query, and the SDK's message stream
(init tools, tool_use, tool_result content, `ResultMessage.usage`, `model_usage`). Answers H-WS-1, H-WS-2, H-WS-5
and the exact `WebSearch` client-tool definition the proxy must pin (its sha256). A second mode returns
`max_uses_exceeded` to see how the CLI reports it.

### 5b. One live call — needs the owner's approval; cap $0.10

Same container pattern as `spikes/tutor-call/run.sh` (Keychain item read into the container, never printed; the
owner's permission is per spike), with `ANTHROPIC_BASE_URL` pointing at a local logging reverse proxy in the
container that forwards to `https://api.anthropic.com`, logs only header names, models, `tools` shapes, `max_uses`
and usage, and rewrites a nested request's `max_uses` to 1 (testing the rewrite FR-AGENT-15 plans).
- Model `claude-haiku-5-5` (main loop; H-WS-2 shows what the nested request uses), custom system prompt (≈ 200
  tokens), `tools=["WebSearch"]`, `max_turns=3`, thinking off, `max_budget_usd=0.10`,
  `CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION=1`.
- Prompt (public metadata only, no book text): "Is there an edition of *Mastering Blockchain* by Imran Bashir newer
  than the 4th edition (Packt, 2023)? Answer in one line with the source URL."
- Expected: 1 search $0.01 + main ≈ 2 × 1,500 tokens + nested ≈ 6,500 tokens at Haiku rates ≈ $0.002 → **≈ $0.012**;
  worst if the nested request runs on Sonnet 5.5 with t = 20,000 at 1-h write rates: $0.01 + $0.082 + $0.005 ≈
  **$0.097**. Under $0.10 in both cases.
- Measures: H-WS-1 to H-WS-4 against the real upstream, `t` for one real query, `web_search_requests`, whether
  `total_cost_usd` includes the fee, and that the upstream accepts the rewritten `max_uses`.
- Ledger: `budget.ledger` entry from `budget.test_budget_usd`.
