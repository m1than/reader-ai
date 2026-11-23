---
id: ADR-0030
title: Server-side web search for the source freshness check — one task-scoped tool allowance in agent-proxy; every other request keeps "tools must be empty"
status: proposed
date: 2026-11-23
verified_by: "SRV-AGENT tests: a run_token minted for any task other than `freshness_web` gets 403 `tools_mismatch` for any non-empty `tools`; a `freshness_web` token forwards only the pinned WebSearch shapes, rewrites each nested request's `max_uses` to min(cap per request, remaining) and refuses with 403 `search_cap` once the token's search allowance is used; credential injection still never applies to a non-provider host. SRV-FRESH tests: no 6-word run of any sampled span reaches the web stage's prompt, a WebSearch query or any forwarded body (canary); a recommendation URL absent from this run's search results is dropped; ledger rows carry web_search_requests and the per-search fee"
revisit_when: "The owner adds an `anthropic-api` provider (the direct Messages path becomes available); or a stub or live run shows the CLI's WebSearch request shapes differ from the pinned ones; or a freshness check costs more than its $1.00 run cap twice; or agent-proxy needs a second tool allowance for another task"
supersedes: []
---

> Amends [ADR-0025](0025-agent-sandbox-and-credential-separation.md) rule 5 and SRV-AGENT's design decision "no tools
> for any compile stage" (for one task only). ADR-0025 rule 2 (tools by allow-list) already permits a declared tool;
> rule 5's "web fetches go through a separate web-fetch proxy" is about fetches **from the Mac**, which this decision
> does not make. Notes for the owner to add on acceptance.

## Context

**Owner request (2026-11-23):** "validate how current the source is; if it's out of date, recommend another resource
to study from." **Owner decision (2026-11-23):** use Claude's server-side web search tool, so the search runs at
Anthropic, egress from the Mac stays limited to `api.anthropic.com`, and recommendations come with real links and
dates.

Constraints from accepted and followed ADRs:
- **ADR-0025 (accepted):** provider tokens live only in agent-proxy; the worker holds a run_token; tools in a
  container with a secret are allow-listed; credential injection only for the pinned provider host.
- **SRV-AGENT (approved):** agent-proxy forwards only `POST /v1/messages` to `https://api.anthropic.com` and refuses a
  body whose `tools` or `mcp_servers` is non-empty (FR-AGENT-4); the init tool list must be empty (FR-AGENT-7); a
  run_token binds one user, one model and one output cap (FR-AGENT-3).
- **ADR-0005 (proposed, followed):** the only provider is the owner's subscription; Agent SDK usage draws from the
  monthly Agent SDK credit; per-user budgets and a ledger.
- **ADR-0008 Decision 4:** materials may be `personal-use`; book text is never committed and never leaves the owner's
  stack except to the model provider for the owner's own processing.

Facts (spikes/web-search/RESULT.md, sourced 2026-11-23): the tool is `web_search` (`web_search_20250305` basic;
`20260209`/`20260318` add dynamic filtering through code execution); `max_uses` caps searches per turn;
`allowed_domains` or `blocked_domains`; results carry `url`, `title`, `page_age`; citations carry ≤ 150 characters of
page text; `usage.server_tool_use.web_search_requests`; **$0.01 per search** plus tokens, and results count as input
tokens. The Agent SDK cannot pass a raw server tool; its CLI exposes a built-in `WebSearch` tool backed by the same
search, returning titles and URLs to the model (hypothesis H-WS-1: through a nested `/v1/messages` request that
carries the server tool). A direct Messages call would need an `anthropic-api` key or an undocumented use of the
subscription token outside Claude Code.

## Options

| Option | Gives | Costs (numbers) |
| --- | --- | --- |
| **A. Server-side search through agent-proxy, Agent SDK `WebSearch`, one task-scoped allowance** | Egress unchanged; subscription credentials as today; a worker-side query gate before any search; real URLs from the search | Two pinned body shapes in the proxy (H-WS-1, stub run planned, $0); ≈ $0.27–0.40 per check, ≤ $0.85 worst planned (RESULT.md § 4); dates are model-reported |
| B. Server-side search, direct Messages call through agent-proxy | One request shape; structured `page_age` and citations; exact search count | An `anthropic-api` key and API spend outside the subscription (ADR-0005), or an untested and possibly disallowed use of the subscription token; no gate before a search runs |
| C. Model only, no tools | No new proxy rule; ≈ $0.13 per check (stage a only) | Knowledge stops at the model's cutoff; links and dates invented or stale; cannot say "a 5th edition came out" |
| D. Host-side search API (a search provider's HTTP API from the worker or a new proxy) | Any provider; structured results | A new egress host and a new secret (API key), against the owner's "egress stays limited to api.anthropic.com"; ADR-0025 rule 5 needs a second proxy |
| E. The owner pastes links | No search, no new rule | The owner does the search the feature exists to do; no freshness verdict without it |

## Decision

**Option A.**

1. **One allowance, one task.** agent-proxy allows tools only on run_tokens minted with `task: "freshness_web"` (the
   web stage of SRV-FRESH). For such a token it forwards exactly two body shapes, both pinned to the CLI version
   SRV-AGENT pins (claude-agent-sdk 0.2.187) by the stub run (RESULT.md § 5a):
   - a **main-loop request** whose `tools` is exactly the CLI's `WebSearch` client-tool definition, compared by
     sha256 of its canonical JSON;
   - a **nested search request** whose `tools` is exactly one object of `type` equal to the pinned web search version
     (`web_search_20250305` unless the stub run shows the CLI sends another) and `name` `web_search`, with only the
     keys `type`, `name`, `max_uses`, `allowed_domains` or `blocked_domains` (and `user_location` only if the CLI
     sends it, as pinned).
   Every other token, and every other shape on a freshness token, keeps "tools must be empty" (403 `tools_mismatch`).
   `mcp_servers` stays refused everywhere.
2. **`max_uses` is enforced by the proxy, not trusted from the client.** The token carries `searches_max` (SRV-FRESH:
   6). For each nested request the proxy rewrites `max_uses` to min(`searches_per_request_max` = 1,
   remaining allowance) — the only field the proxy ever rewrites — and subtracts it at forwarding; at 0 remaining it
   refuses with 403 `search_cap`. The rewrite bounds the billed searches by `searches_max` whatever the CLI or the
   model asks, and one search per request keeps the input cost linear in the number of searches (RESULT.md § 4).
   The token also carries `requests_max` (main turns + nested requests).
3. **Queries carry metadata and topic terms only, by construction and by a gate.**
   - The web stage's prompt holds only bibliographic metadata, outline titles and short claim subjects (≤ 5 words
     each), never span text (SRV-FRESH FR-FRESH-5).
   - The worker's `can_use_tool` callback (and a `PreToolUse` hook) denies any `WebSearch` call whose query is longer
     than 120 characters, contains a 6-word run of any sampled span, or contains a word outside the vocabulary of
     that prompt plus a fixed list of query words (`edition`, `latest`, `documentation`, years, ...).
   - A canary test proves both: canary words planted in span text never appear in the prompt, a query or any body
     forwarded through a stub upstream.
4. **Egress unchanged.** The search runs at Anthropic. The worker and agent-proxy reach no new host; injection still
   applies only to `api.anthropic.com`. ADR-0025 rule 5's separate web-fetch proxy is still the only route for any
   future fetch from the Mac.
5. **Budget in the ledger.** The search fee is priced (`web_search_usd` = 0.01 in SRV-AGENT's pricing table) and each
   settle row stores `web_search_requests`. A check has a run cap ($1.00) and a per-user daily cap for the task
   ($2.00, SRV-AGENT FR-AGENT-12's daily scope); the cost estimate is shown before the owner runs it.

## Threats and controls

| Threat | Control | Test |
| --- | --- | --- |
| **Prompt injection from web results into the report** (a page tells the model to call the book current, to recommend a link, or to emit markup) | The report is JSON validated against a closed schema; verdict is an enum; free text fields are length-bounded and stored as plain text, never rendered as HTML or Markdown (WEB-FRESH); a recommendation URL must be one the searches returned in this run (link check); the model has no other tool to act with | SRV-FRESH VEC-FRESH-2 injection cases; INV-FRESH-3 |
| **Injection into later prompts** (if the report later feeds compile or the tutor) | Out of scope now: the report feeds no prompt. A later consumer must treat it as untrusted material inside the source-delimiter scheme of SRV-PIPELINE VEC-PIPELINE-5 and pass only enum fields and our own outline titles, never web text (a defer in SRV-FRESH) | — (defer) |
| **Link spoofing** (display text vs target, look-alike hosts, `javascript:` or `data:` URLs, redirects) | Only `http`/`https` URLs, ≤ 2,048 characters, no credentials in the URL; the UI shows the URL as text with the host in ASCII/punycode beside the title and links the same string (`rel="noopener noreferrer nofollow"`, new tab); the link must be in this run's search results | SRV-FRESH VEC-FRESH-3; WEB-FRESH INV-FRESHUI-1 |
| **Book text in search queries** (a query is sent to a search engine outside the owner's provider relationship) | Decision 3: by construction and by the query gate; canary test | SRV-FRESH INV-FRESH-1 |
| **Copyright of web content** | Stored: titles, URLs, dates, the model's own reasons (≤ 300 characters), our outline section titles. Never stored: `encrypted_content`, `cited_text`, page text, snippets. Nothing of it is committed | SRV-FRESH INV-FRESH-2 |
| **Cost** (a model looping on searches, large results) | Proxy-enforced `searches_max` and one search per request; `requests_max`; run cap $1.00 and daily cap $2.00 in the ledger; reserve before each call (SRV-AGENT FR-AGENT-9); the estimate before the run | SRV-AGENT FR-AGENT-15, INV-AGENT-6; SRV-FRESH FR-FRESH-9 |
| **Token or credential exposure** (unchanged surface) | ADR-0025 unchanged; the WebSearch tool runs inside the CLI and reaches only `ANTHROPIC_BASE_URL` (H-WS-1, checked by the stub run under `--network none`) | SRV-AGENT INV-AGENT-1/3 |
| **Data shared with the search backend** | Only queries built from metadata and topic terms; the search is part of the owner's Anthropic usage | INV-FRESH-1 |

## Rejected options — why (numbers)

- **B (direct call).** Needs an API key (ADR-0005 allows only the subscription today) or an undocumented use of the
  subscription token; it also has no point at which reader-ai can refuse a query before it runs. Cost is comparable
  (≈ $0.40 per check at t = 6,000 vs ≈ $0.27 for A, RESULT.md § 4), so cost does not favour it. It becomes the
  better option once an `anthropic-api` provider exists (revisit_when).
- **C (model only).** It cannot know anything after its cutoff, which is the question asked; it would produce links
  nobody checked. Kept as stage (a) of the check, which costs ≈ $0.13 and needs no tool.
- **D (host-side search API).** One new egress host and one new secret, both of which the owner's decision excludes.
- **E (owner pastes links).** Moves the work to the owner; the "Add as source" hook (SRV-FRESH defers) keeps this
  path open for links the owner already has.

## Consequences

- agent-proxy gains task-scoped tokens, a pinned-shape check, one field rewrite and a per-token search counter
  (SRV-AGENT FR-AGENT-14..16). Everything else in FR-AGENT-4 is unchanged; a test refuses tools for every other task.
- The worker's SDK adapter gains, for this task only: `tools=["WebSearch"]`, a `can_use_tool` gate, `max_turns` > 1,
  and an init check that the tool list equals `["WebSearch"]`.
- Multi-turn runs make several upstream 2xx per run_token; FR-AGENT-9's "more than one 2xx → max(SDK cost, reserve)"
  rule would then always settle at the reserve. The amendment settles freshness tokens at the SDK cost when the
  record shows only pinned shapes and no refusal.
- Hypotheses H-WS-1..5 (RESULT.md § 3) must hold before Red: the stub run (zero credit) first, then one live call
  (≤ $0.10, owner approval).
- Freshness reports are owner-only data like materials (ADR-0008 Decision 4).

## Open questions

1. **Path:** the Agent SDK path (A) is chosen so that the subscription stays the only credential. Would you rather add
   an `anthropic-api` key for this feature (B: structured dates and citations, API spend outside the subscription)?
2. **Live call:** approve one live call under $0.10 (RESULT.md § 5b) from `budget.test_budget_usd`?
3. **Caps:** run cap $1.00, daily cap $2.00, 6 searches per check, report TTL 90 days. Change any?
4. **Search backend data:** queries (title, edition, topics) go to Anthropic's search backend. Acceptable for
   `personal-use` materials?
