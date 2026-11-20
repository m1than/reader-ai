---
id: ADR-0017
title: Accounts, data ownership with an always-available export, and monetization of knowledge transformation
status: proposed
date: 2026-11-20
verified_by: "SRV-EXPORT tests: export → import into an empty instance reproduces the user's knowledge base; export works for a user with no plan and no provider; SRV-AGENT ledger tags every run with transformation kind and funding source"
revisit_when: "Before open registration and billing go live with the VPS (v2), or if a jurisdiction the product sells into requires a different export format"
---

## Context

Owner decisions, 2026-11-20:

- Users register so that they can always download the knowledge base they built.
- Revenue comes from the **transformation**: the work in which a person and the AI together turn material into knowledge (compiled lessons, verified findings, sector structure, tutoring).
- Payment model: hybrid. A user either brings their own provider and pays a platform fee for transformation, or buys credits that run on reader-ai's own API key with a margin.
- v1 lays the foundation; open registration, plans and payment ship in v2 together with the VPS.
- Export is an open archive that opens without reader-ai.

Constraints:

- Anthropic's Commercial Terms permit using the API to power products offered to the customer's own users; consumer plans are governed by the Consumer Terms instead. Managed credits therefore run on a reader-ai organisation API key, never on the owner's subscription (ADR-0005).
- Under the Commercial Terms, the customer owns the Outputs, and Anthropic does not train models on Customer Content from the Services. This is what lets reader-ai promise users that the knowledge built for them is theirs.

## Decision

### Ownership and export (v1)

What a user brings and what is produced for them belongs to them and can be exported at any time. This includes materials they uploaded, lessons, findings, sectors, notes, recall history and their own tutor transcripts. Export is free on every plan, including after a plan lapses: the account then becomes read-only, and export keeps working.

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

Invariant: importing an export into an empty instance reproduces the user's knowledge base (findings, sectors, links, lessons, notes, recall state).

### Accounts (v1 foundation)

- Every user-owned row carries `owner_id` from the first migration. Shared access goes through the role model (ADR-0016), never through copying rows. Adding tenancy after data exists is a migration of every table; adding it now is one column.
- Registration in v1 stays invitation-only on the owner's Mac (ADR-0012). The account model already supports self-service sign-up behind a disabled flag.

### Metering (v1 foundation)

Every agent run in the cost ledger (ADR-0005) records:

- `transformation_kind`: `compile_lesson`, `fact_check`, `kb_synthesis`, `tutor_turn`, `ingest_assist`;
- `funding`: `owner_personal`, `byok` or `managed`;
- tokens, cost, and the user it was done for.

v1 uses only `owner_personal` and `byok`. The ledger is the source of truth for v2 billing.

### Monetization (v2)

| Path | Who pays the model | What reader-ai charges |
| --- | --- | --- |
| Bring your own provider | The user, at their provider | Platform fee per transformation (or a monthly plan with a transformation allowance) |
| Managed credits | reader-ai, on its organisation API key under the Commercial Terms | Credits = model cost × margin |

Plans and prices are data (`plans.yaml`: allowances, fee per transformation kind, margin), not code.

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **Hybrid: BYOK + platform fee, or managed credits** | Serves users with a provider and users without one | Two billing paths |
| BYOK only | No model cost risk for reader-ai | A user without a provider is read-only (ADR-0016) and cannot become a paying customer |
| Managed credits only | Simplest for users | reader-ai carries all model cost risk; users who already pay for a provider pay twice |
| Export as a paid feature | Revenue from lock-in | Contradicts the owner's principle; GDPR Article 20 gives EU users a right to data portability anyway |

## Rejected options — why (numbers)

- **BYOK only.** Covers 1 of 2 user types; every user without a provider stays a viewer who cannot pay.
- **Managed only.** Puts 100% of model cost on reader-ai. At the pilot target of ≤ $4.35 per compiled lesson (ADR-0005), a 23-lesson course is about $100 of model cost before any margin, paid up front by reader-ai for every new course.
- **Paid export.** Owner rejected it; the product sells the transformation, not access to the user's own results.

## Open questions (v2)

1. Prices: fee per transformation kind, margin on managed credits, free allowance.
2. Seller entity and payment provider (merchant of record or direct payments), which depends on where the product sells.
