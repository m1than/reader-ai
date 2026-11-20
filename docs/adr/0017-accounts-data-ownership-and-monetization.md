---
id: ADR-0017
title: Accounts, data ownership with an always-available export, and freemium with paid managed transformation
status: proposed
date: 2026-11-20
verified_by: "SRV-EXPORT tests: export → import into an empty instance reproduces the user's knowledge base; export works on every tier and for read-only accounts. SRV-AGENT ledger tags every run with transformation kind and funding. Pricing check: plans.yaml prices ≥ measured cost × 1.7 for every transformation kind"
revisit_when: "Before open registration and billing go live with the VPS (v2); when measured cost per page moves by more than 20%; if a jurisdiction the product sells into requires a different export format"
---

## Context

Owner decisions, 2026-11-20:

- Users register so that they can always download the knowledge base they built.
- Revenue comes from the **transformation**: the work in which a person and the AI together turn material into knowledge (compiled lessons, verified findings, sector structure, tutoring).
- **Freemium:** a free user connects their own Claude or Codex agent, and that agent effectively runs the reader-ai pipeline.
- **Pro and Max:** reader-ai runs the transformation on its own models. Each plan includes token credits sized to an approximate amount of material, priced so that reader-ai keeps a 70% spread on top of model cost. Paid plans also get custom models, local models and the rest of the model zoo (ADR-0018).
- v1 lays the foundation; open registration, plans and payment ship in v2 together with the VPS.
- Export is an open archive that opens without reader-ai.

Constraints:

- Anthropic's terms (ADR-0005):
  - Consumer-plan credentials (Free, Pro, Max) must not be routed through a third-party service on behalf of its users.
  - The Commercial Terms permit using an API key to power products offered to the customer's own users.
  - Under the Commercial Terms the customer owns the Outputs, and Anthropic does not train on Customer Content.
- A free user's "own agent" therefore cannot mean "log in to reader-ai with your Claude subscription". It means one of two things:
  - the agent runs in the user's own client (Claude Code, Codex CLI or app, under the user's own plan) and connects to reader-ai over MCP;
  - the user gives reader-ai their own API key.
- Claude Code and Codex both act as MCP clients for remote Streamable HTTP servers with OAuth. Codex keeps that configuration in `~/.codex/config.toml`, shared by the CLI, the IDE extension and the ChatGPT desktop app.

## Decision

### Tiers

| | Free | Pro | Max |
| --- | --- | --- | --- |
| Who runs the models | The user's own agent (Claude Code or Codex) over MCP, or the user's own API key | reader-ai, on its organisation API keys | reader-ai, on its organisation API keys |
| Pipeline | The same stages and gates, exposed to the user's agent as MCP tools, prompts and a skill pack; reader-ai validates every stage's output (schemas, citations, lab runs in our sandbox) | Fully managed, including verification | Fully managed, including verification |
| Credits | — | Monthly allowance sized in pages of material plus tutor sessions | Larger allowance, priority queue |
| Models | Whatever the user's agent runs | Claude models | Claude models, plus custom and local models, routing and per-agent control (v2 zoo, ADR-0018) |
| Export | Always | Always | Always |
| Platform limits (our infrastructure cost) | Quotas on ingestion pages, sandbox minutes and storage | Higher | Highest |

Free still costs reader-ai infrastructure: ingestion CPU, sandbox minutes, storage and the verification runs. Those are bounded by quotas, not by credits.

### Pricing rule

Prices are data in `plans.yaml`. The rule that every price must satisfy:

```
price(transformation) ≥ model_cost(transformation) × (1 + spread),   spread = 0.70
```

`model_cost` comes from the ledger: the measured average cost of that transformation kind, compile per page of material or tutor per session. CI checks every plan against the latest measured costs. A price that falls below the rule fails the check.

**Interpretation to confirm.** "A 70% spread on top of cost" is read as a markup: price = cost × 1.7, which is a gross margin of 41% of revenue. If the owner means a 70% gross margin, then price = cost ÷ 0.3, which is cost × 3.33.

| Example (hypothesis until `compile-cost` measures it) | Model cost | Price at markup 1.7 | Price at margin 70% |
| --- | --- | --- | --- |
| One compiled lesson at the pilot ceiling | $4.35 | $7.40 | $14.50 |
| Per page of material (≈ 39 pages per lesson) | ≈ $0.11 | ≈ $0.19 | ≈ $0.37 |
| The whole Bashir book (890 pages, 23 lessons) | ≤ $100 | ≤ $170 | ≤ $333 |

Credits are denominated in what the user understands, pages of material and tutor sessions, not in tokens. Tokens stay an internal unit in the ledger.

### Ownership and export (v1)

What a user brings and what is produced for them belongs to them and can be exported at any time. This includes materials they uploaded, lessons, findings, sectors, notes, recall history and their own tutor transcripts. Export works on every tier, including after a paid plan lapses: the account then becomes read-only, and export keeps working.

Archive layout (`reader-export-<user>-<date>.zip`):

| Path | Content | Opens in |
| --- | --- | --- |
| `manifest.json` | Format version, counts, sha256 of every file, export time | Any JSON reader |
| `knowledge/<sector path>/<finding>.md` | One finding per file. YAML frontmatter holds kind, sectors, citations, verified and dates; links are `[[wikilinks]]` | Obsidian, any Markdown editor |
| `sectors.json` | Sector tree with descriptions | — |
| `lessons/<course>/<lesson>.ipynb` | Published lesson revisions | JupyterLab, VS Code, GitHub |
| `notes/<course>/<lesson>.md` | The learner's notes per cell | Markdown |
| `recall/cards.csv`, `recall/reviews.csv` | Cards and full review log, so FSRS state can be rebuilt | Spreadsheets, Anki import |
| `transcripts/<date>-<session>.md` | The user's own tutor sessions | Markdown |
| `materials/` | Only files this user uploaded. Material shared with them under `personal-use` is listed by title and sha256, not included | — |

Invariant: importing an export into an empty instance reproduces the user's knowledge base.

### v1 foundation

- **Accounts.** Every user-owned row carries `owner_id` from the first migration. Self-service sign-up exists behind a disabled flag; v1 stays invitation-only on the owner's Mac.
- **Metering.** Every agent run in the ledger records:
  - `transformation_kind`: `compile_lesson`, `fact_check`, `kb_synthesis`, `tutor_turn`, `ingest_assist`;
  - `funding`: `owner_personal`, `own_agent`, `byok` or `managed`;
  - pages of material processed, tokens and cost.
- **Dogfooding the free tier.** The owner's own Claude Code on the Mac connecting to reader-ai over MCP (stage 7) is exactly the free-tier path, and it is exercised from v1.

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **Freemium with own agent over MCP + paid managed tiers** | Free users bring their own model cost; paid users get convenience and the model zoo; compliant with provider terms | The pipeline must also work as MCP tools for an external agent |
| Free tier on reader-ai's models | Simplest onboarding | Every free user costs model spend; at ≤ $100 per book, 100 free users compiling one book each cost ≤ $10,000 |
| Free tier by logging in with the user's Claude subscription on our servers | What users might expect | Forbidden: consumer-plan credentials may not be routed through a third-party service |
| Platform fee for BYOK (previous version of this ADR) | Revenue from every user | Owner chose a free entry tier instead |

## Rejected options — why (numbers)

- **Free on our models.** Model cost scales with free usage: ≤ $100 per 890-page book, before any revenue.
- **Subscription login on our servers.** Not a cost question: the provider terms forbid it.
- **Pricing in tokens.** Users cannot estimate tokens; they can estimate pages. A 23-lesson book is about 890 pages, which is a number a learner can read off the material.

## Open questions

1. Spread: markup (price = cost × 1.7) or margin (price = cost ÷ 0.3)? See the table above.
2. Free-tier quotas: pages ingested per month, sandbox minutes, storage. Proposed: set them from stage 9 capacity numbers.
3. Plan allowances and prices for Pro and Max, once `compile-cost` and stage 5 have measured real costs.
4. Seller entity and payment provider (merchant of record or direct payments), which depends on where the product sells.
