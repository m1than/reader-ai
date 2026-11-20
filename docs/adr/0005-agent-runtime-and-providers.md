---
id: ADR-0005
title: Agent runtime, per-user providers and cost budgets
status: proposed
date: 2026-11-20
verified_by: "SRV-AGENT tests: provider credentials are bound to one user (INV); budget exhaustion stops a job with an event; cost ledger sums match SDK-reported costs"
revisit_when: "Anthropic changes the subscription terms for the Agent SDK, or a second user needs a non-Anthropic model"
---

> Proposed amendment: [ADR-0025](0025-agent-sandbox-and-credential-separation.md) (2026-11-20) — the provider token lives only in a credential-injecting proxy; agent containers that hold any secret run with code and filesystem tools disabled.

## Context

Owner decision, 2026-11-20: only the owner's $200 subscription, no API spending here; "not everyone fits this, then different providers must be configurable".

Anthropic's terms as of 2026-11-20 (code.claude.com, Legal and compliance; Agent SDK billing notice):

1. OAuth tokens from Free, Pro and Max plans are intended for Claude Code and Anthropic's native apps. Developers building products or services, including with the Agent SDK, should use API keys.
2. Anthropic does not permit third parties to offer Claude.ai login or to route requests through Free, Pro or Max credentials on behalf of their users.
3. Since 2026-06-15, Agent SDK and `claude -p` usage on subscription plans draws from a separate monthly Agent SDK credit; for Max 20x it is $200 per month, billed at API rates. Advertised limits assume ordinary, individual use.

Consequences for reader-ai:

- The owner running reader-ai for himself on his own subscription is individual use, metered against his $200 monthly Agent SDK credit.
- Sharing and paired programs (ADR-0016) must **never** route another person's requests through the owner's subscription. Every user brings their own credentials.

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **Claude Agent SDK behind a provider port; credentials per user** | Tools, MCP, subagents, streaming and cost reporting from the SDK; subscription, API key, Bedrock and Vertex through one runtime | Anthropic models only until another adapter is written |
| Anthropic Messages API directly | Full control | Re-implements the agent loop, tool execution and MCP client the SDK provides; cannot use a subscription |
| Multi-vendor framework (LangGraph, LiteLLM) from day one | Any model | Second abstraction layer for a single-provider v1; still cannot use a subscription |

## Decision

All agent work (pipeline stages, tutor, review) runs through the Claude Agent SDK for Python behind a `Provider` port. Credentials are declared per user in `reader.yaml` (ADR-0011):

| Provider | Credential | Who may use it |
| --- | --- | --- |
| `anthropic-subscription` | `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` | Only the user who owns the subscription |
| `anthropic-api` | API key | That key's owner |
| `bedrock`, `vertex` | Cloud credentials | That account's owner |

Invariant: a request is always executed with credentials owned by the user it is attributed to. A shared course, a pair session or an MCP call never borrows another user's credentials.

Model choice is declared per task in config (`tasks.draft.model`, `tasks.fact_check.model`, ...), not in code.

## Budgets and cost ledger

- Every SDK result reports its cost; the worker appends it to a `cost_ledger` row (user, task, lesson, tokens, cost).
- Budgets are declared per user and per task class: monthly cap, per-lesson compile cap. Reaching 80% emits a warning event; reaching 100% stops new jobs of that class with an event, never mid-stage.
- Pilot target: compiling all 23 Bashir lessons costs at most 50% of one month's $200 credit, so tutoring keeps the other half. That is ≤ $100 in total, ≤ $4.35 per lesson. Chapter 1 is 135,935 characters of text (measured); the token count and the cost of one full compile are measured by `spikes/compile-cost` on L01 before SRV-PIPELINE is approved.

## Rejected options — why (numbers)

- **Messages API directly.** Cannot use the subscription at all, which the owner requires; also rewrites the agent loop the SDK already ships.
- **Multi-vendor framework now.** Today there is 1 provider family in use and 0 users asking for another; the port keeps the door open at the cost of one interface instead of a framework.

## Resolved questions (owner, 2026-11-20)

1. reader-ai on the owner's own subscription is personal use, metered by his Agent SDK credit.
2. Every invited person connects their own materials and their own provider; every agent action runs on the credentials of the person it acts for. A person without a provider gets read-only access (ADR-0016).
