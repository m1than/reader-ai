---
id: ADR-0030
title: Server-side web search for the source freshness check — one task-scoped tool allowance in agent-proxy; every other request keeps "tools must be empty"
status: accepted
date: 2026-11-23
verified_by: "SRV-AGENT tests: any token without a `web_search` allowance (every task but `freshness_web`, and a `freshness_web` token without one) gets 403 `tools_mismatch` for non-empty `tools`; an allowance token forwards only the two pinned shapes, rewrites `max_uses` to min(1, remaining), refuses `search_cap` and `request_cap`, and refuses any other model; credential injection still never applies to a non-provider host. SRV-FRESH tests: queries are only code-planned ones (canary INV-FRESH-1); every stored URL is a URL string the searches returned (INV-FRESH-3); ledger rows carry web_search_requests and the per-search fee"
revisit_when: "The owner adds an `anthropic-api` provider (the direct Messages path becomes available); or the CLI pin changes or a stub or live run shows other WebSearch shapes or a web search version other than web_search_20250305; or a freshness check costs more than its $1.00 run cap twice; or agent-proxy needs a second tool allowance for another task"
supersedes: []
---

> Amends SRV-AGENT's "tools must be empty" (FR-AGENT-4, FR-AGENT-7) for one task, and ADR-0025 as notes for the
> owner to add on acceptance:
> - rule 2 (tools by allow-list) already permits a declared tool;
> - rule 5's "web fetches go through a separate web-fetch proxy" is about fetches **from the Mac**, which this decision
>   does not make.
>
> **For the owner (ADR-0005):** with this ADR agent-proxy, for the first time, **rewrites a field** of a request it
> forwards. It is one field, `max_uses` on the nested search tool, and only ever lowered. The proxy still never adds
> tools, changes prompts or changes the model.

## Context

**Owner request (2026-11-23):** "validate how current the source is; if it's out of date, recommend another resource
to study from." **Owner decision (2026-11-23):** use Claude's server-side web search tool, so the search runs at
Anthropic, egress from the Mac stays limited to `api.anthropic.com`, and recommendations come with real links.

Constraints:
- **ADR-0025 (accepted):** provider tokens live only in agent-proxy; the worker holds a run_token; tools in a container
  with a secret are allow-listed; credential injection only for the pinned provider host.
- **SRV-AGENT (approved):** agent-proxy forwards only `POST /v1/messages` to `https://api.anthropic.com` and refuses a
  non-empty `tools` or `mcp_servers` (FR-AGENT-4); the init tool list must be empty (FR-AGENT-7); a run_token binds
  one user, one model and one output cap (FR-AGENT-3).
- **ADR-0005 (proposed, followed):** the only provider is the owner's subscription, through the Agent SDK.
- **ADR-0008 Decision 4:** materials may be `personal-use`; book text never leaves the owner's stack except to the
  model provider for the owner's own processing.

Facts (spikes/web-search/RESULT.md, sourced and measured 2026-11-23):
- The tool is `web_search` (`web_search_20250305` basic), at **$0.01 per search** plus tokens; results count as input
  tokens.
- The Agent SDK cannot pass a raw server tool. Its CLI's built-in `WebSearch` makes a **nested** `/v1/messages`
  request through `ANTHROPIC_BASE_URL`, carrying `tools: [{type: "web_search_20250305", name: "web_search",
  max_uses: 8}]`, on the main model, uncached and without a `thinking` key. The worker gets the structured result
  `{query, results: [{content: [{title, url}]}, summary]}`; `page_age` does not reach it.
- A `PreToolUse` hook deny stops the search before any nested request.
- `ResultMessage.usage` covers the main loop only; `total_cost_usd` and `model_usage` include the nested request and
  the fee.
- A direct Messages call would need an `anthropic-api` key, or a use of the subscription token outside Claude Code and
  the Agent SDK that is not documented.

## Options

| Option | Gives | Costs (numbers) |
| --- | --- | --- |
| **A. Server-side search through agent-proxy, Agent SDK `WebSearch`, code-planned queries, one search per run, a tool-less report call** | Egress unchanged; subscription credentials; queries fixed by code before any search; worst case ≈ $0.39 per check by formula | Two pinned body shapes and one rewritten field in the proxy; dates are model-reported ("as reported") |
| A′. As A, but one multi-turn tool loop that writes its own queries and the report | Adaptive searching | Worst case ≈ $0.93 on Sonnet (deep-reviewer round 1), above the $1.00 cap once the scan is added; model-written queries need a vocabulary gate with a denial rate |
| B. Server-side search, direct Messages call through agent-proxy | One request shape; structured `page_age` and citations | An `anthropic-api` key and API spend outside the subscription, or an undocumented use of the subscription token; no gate before a search runs |
| C. Model only, no tools | No new proxy rule; ≈ $0.13 per check | Knowledge stops at the model's cutoff; links and dates invented or stale |
| D. Host-side search API | Any provider | A new egress host and a new secret, which the owner's decision excludes |
| E. The owner pastes links | Nothing new | The owner does the search the feature exists to do |

## Decision

**Option A.**

1. **One allowance, one task.** agent-proxy allows tools only on run_tokens minted with `task: "freshness_web"` **and**
   a `web_search` allowance (SRV-AGENT proposed FR-AGENT-14). A `freshness_web` token without the allowance is
   tool-less. On an allowance token, a body is forwarded only in one of two **structurally pinned** shapes:
   - **main loop:** `WebSearch` with keys `name`, `description`, `input_schema` and no `type`. Its `input_schema` is
     pinned by sha256 and its description length is bounded. The description is not pinned by bytes, because the CLI
     may put dated text in it. At most one `DeferredToolPlaceholder` (`defer_loading: true`) may accompany it.
   - **nested search:** exactly `{type: "web_search_20250305", name: "web_search", max_uses?}`, with no domain
     lists, `user_location`, `allowed_callers` or `search_profile`.

   Every request's model must equal the token's model. Anything else fails closed (403) and settles by FR-AGENT-9's
   record rule. This covers the CLI paths the deep-reviewer saw in the binary but that are not on the wire (a
   small-model flag, other search services, an auxiliary request). **Any other web search version, or a new CLI pin,
   needs a new deep-reviewer round.**
2. **`max_uses` and request count are enforced by the proxy, not trusted from the client.**
   - The proxy rewrites `max_uses` to min(1, remaining allowance), the only field it ever rewrites, and refuses at 0
     remaining (`search_cap`).
   - It counts every forwarded request, the CLI's own 429/529 resends included, against `requests_max` (3 for a search
     run).
   - The worker sets `abort_on_retry`, so a retry ends the run instead of spending the allowance.
3. **Queries carry metadata and topic terms only — by construction.**
   - Code plans at most 6 queries from fixed templates over the bibliography, outline titles and ≤ 5-word claim
     subjects (SRV-FRESH FR-FRESH-5).
   - Each query runs as its own short SDK run with one search (Haiku 5.5).
   - A `PreToolUse` hook allows only that query, verbatim, with no domain lists (FR-FRESH-6).
   - The guarantee is stated exactly in INV-FRESH-1:
     - bibliography and outline strings are exempt;
     - at most 5 other book-derived words per query;
     - no 6-word run of a non-front span.
   - A canary test proves it.
4. **The report is written by a tool-less call** (Sonnet 5.5) over the search results, delimited as untrusted data.
   - The model sees each result only as `r<k> host title` and cites ids.
   - Code stores the URL string, host and title that the search returned for that id (INV-FRESH-3).
   - The searches and the report run in **one job**. The link set and the summaries live only in that job's memory
     and are never stored. A worker restart restarts the searches.
5. **Egress unchanged.** The search runs at Anthropic. The worker and agent-proxy reach no new host.
6. **Budget in the ledger.**
   - The fee is priced (`web_search_usd` = 0.01).
   - Settle rows store `web_search_requests` and settle from `total_cost_usd`, which includes nested requests and the
     fee.
   - Caps: $1.00 per check and $2.00 per user per day, with the estimate shown before a run (owner-approved).
   - The worst case by formula is ≈ $0.39 per check. The tokens a search adds cannot be enforced; their planning
     bound is a chosen 20,000.

## Threats and controls

| Threat | Control | Test |
| --- | --- | --- |
| **Prompt injection from web results into the report** | The report call has no tools; web text sits inside a delimiter it cannot forge; output is JSON against a closed schema with enum verdicts and bounded, sanitised text; stored URLs and titles come from the search results, never from the model; nothing is rendered as HTML or Markdown | SRV-FRESH VEC-FRESH-2; INV-FRESH-2/-3 |
| **Injection into the search runs** | The search-run model's reply is discarded; only the structured tool result is read; the gate allows only the planned query | VEC-FRESH-5 |
| **Injection into later prompts** | None today: INV-FRESH-5 forbids any other module from reading the report. A later consumer gets only enum fields and our own outline titles inside SRV-PIPELINE's source delimiter (defer) | INV-FRESH-5 |
| **Link spoofing** | Only `http`/`https` URL strings from R, no userinfo, ≤ 2,048 characters; the UI shows the ASCII host beside the title and links the same string (`noopener noreferrer nofollow`) | VEC-FRESH-3; WEB-FRESH INV-FRESHUI-1 |
| **Poisoned but genuine results** (residual) | A result that the search really returned can still be a bad or malicious page (SEO spam, a look-alike publisher). The link check cannot catch it. The card shows the host and "as reported"; the owner judges; per-domain allow lists are a defer | — (residual, stated) |
| **Book text in search queries** | Decision 3: code-planned queries, verbatim gate, the bounded guarantee of INV-FRESH-1 | INV-FRESH-1 (canary) |
| **Copyright of web content** | Stored: titles and URLs that are cited or recommended, dates as reported, the model's own reasons (≤ 300 characters), our outline section ids, the queries. Never stored: summaries, `cited_text`, `encrypted_content`, page text | INV-FRESH-2 |
| **Cost** | Proxy-enforced search allowance, one search per request, `requests_max`, `abort_on_retry`; per-call reserves; run and daily caps; a call is retried only after a $0 settle, except one paid retry of the report call (worst $0.51 per check) | SRV-AGENT INV-AGENT-6, VEC-AGENT-4; SRV-FRESH FR-FRESH-9/-12 |
| **Token or credential exposure** | ADR-0025 unchanged; the stub run under `--network none` saw no host but `ANTHROPIC_BASE_URL` | SRV-AGENT INV-AGENT-1/3 |

## Rejected options — why (numbers)

- **A′ (one multi-turn loop).** Its worst case (≈ $0.93 on Sonnet, plus the $0.17 scan reserve) exceeds the owner's
  $1.00 cap. Model-written queries also need a vocabulary gate with an unmeasured denial rate.
- **B (direct call).** Needs a key the owner declined (2026-11-23), or an undocumented use of the subscription token.
- **C (model only).** Cannot know anything after its cutoff, which is the question asked. It survives as stage (a),
  the scan.
- **D (host-side search API).** A new egress host and a new secret, both excluded by the owner.
- **E (owner pastes links).** Moves the work to the owner.

## Consequences

- agent-proxy gains allowance tokens, structural shape checks, one field rewrite, a search counter and a request cap
  (SRV-AGENT FR-AGENT-14..16). For every other task FR-AGENT-4/7 are unchanged.
- The SDK adapter gains, for `("freshness_web", "search")` only: `tools=["WebSearch"]`, a `PreToolUse` hook,
  `max_turns` 2, `abort_on_retry`, and an init check for `["WebSearch"]`.
- Tool calls settle from `total_cost_usd`. If the live call shows that `model_usage` does not match the proxy-observed
  requests (H-WS-6), they settle at their reserve.
- Observed on the side: the CLI sends `thinking: {type: adaptive}` on main-loop requests even with
  `MAX_THINKING_TOKENS=0` (spikes/web-search § 3a; relevant to V.agent-thinking).
- Freshness reports are owner-only and quarantined (INV-FRESH-4/-5).

## Owner decisions (2026-11-23)

1. **Search path:** subscription only, through the Agent SDK `WebSearch`; dates labelled "as reported".
2. **Live call:** approved, at most $0.10 from `test_budget_usd`, one search, preferably on Haiku 5.5. The owner runs
   `spikes/web-search/run-live.sh`.
3. **Caps:** as proposed — $1.00 per run, $2.00 per day, 6 searches per check, 90-day TTL.
4. **Privacy:** queries may contain title, edition, authors and outline topics; never book text.

## Open questions

- None for the owner. The ADR's status is the owner's call (H3).
