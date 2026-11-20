---
id: ADR-0002
title: Product scope, non-goals and pilot
status: proposed
date: 2026-11-20
verified_by: "Pilot acceptance: Bashir course plan approved at gate G1 and lesson L01 published at gate G2"
revisit_when: "The second course in the app needs a capability listed here as a non-goal"
---

## Context

Owner decisions, 2026-11-20:

- Build on existing pieces (a Jupyter kernel), but as a separate application with its own UI; practice and questions happen inside the app.
- Any material can be loaded; the user sets the limits.
- One notebook per lesson plus a course index, using the lesson template from the blockchain study.
- The Claude Docs knowledge base of the blockchain study moves into the app.
- The agent runs every authoring task in stages with fact-checking and validation.
- A web UI whose components can be changed on the fly through a Storybook component library.
- Pilot: Bashir, «Блокчейн. Исчерпывающее руководство» (DMK 2025, Russian edition of *Mastering Blockchain*, 4th ed.), with every lesson marked up.
- An MCP connector for other agents, organised by knowledge sectors; sharing; paired study and teaching programs.
- Learning-loop principles come from James Clear, *Atomic Habits*: distance (friction to start, the long run, spacing), engagement and the four laws. The ideas are the design basis for how learners work with data and text; the book itself is not loaded into the app. Soft engagement mechanics only.
- v1 runs on the owner's Mac; a VPS follows once load is measured (ADR-0012).

## Scope (v1)

| Capability | ADR |
| --- | --- |
| Upload material; outline-first ingestion | 0008 |
| Course plan → lessons as `.ipynb` with a reader-ai cell schema | 0004, 0009 |
| Staged compile with fact-check, lab verification, adversarial review, two owner gates | 0009 |
| Notebook UI with a sandboxed kernel for labs | 0003, 0013, 0014 |
| Tutor chat bound to the current cell; recall cards (FSRS); habit tracker; notifications (Telegram first) | 0010 |
| Agent providers per user, budgets and cost ledger | 0005 |
| Sectorized knowledge base and MCP server | 0015 |
| Sharing, roles, paired programs | 0016 |
| Declarative config, cross-platform secrets | 0011 |
| Self-hosted Docker Compose deployment behind Caddy | 0012 |

## Non-goals (v1)

- Hosting material for the public or redistributing it. Copyrighted sources stay on the owner's volume and are never committed; the repository references them by sha256.
- Points, XP, leaderboards, competitive streaks (owner chose soft engagement, 2026-11-20).
- Non-Anthropic agent runtimes. The provider port exists (ADR-0005); other implementations wait for a real need.
- Mobile apps. The web UI is responsive; native shells are out of scope.
- Real-time collaborative editing of lesson text by several authors. Pair sessions share execution and chat, not simultaneous text editing (ADR-0016).

## Pilot: Bashir course

`pilot/bashir/course.yaml` marks up all 23 chapters as lessons L01–L23: plan order, stage, page ranges, sections and subsections, generated from the PDF bookmark outline (494 entries). The source PDF (935 pages, sha256 `61e8d4c390ba63c3f13efe0860d5042f8d1b19ba8f26b4f2e14e73478bc9e2b2`) is not committed.

Two findings from building the manifest:

1. Chapter 23 «Альтернативные блокчейны» (book pages 877–915) was missing from the study plan, which covered 22 chapters. It is added as stage S16 with 5 sessions: 4 theory hours × 1.2 buffer. Chapter 22 has the same 39 pages and was planned at 3 hours; chapter 23 gets one hour more because alternative chains changed the most since the 2023 original, so its "what changed" cells need the most web verification. The owner delegated this estimate and gives feedback after the stage.
2. A text parse of the table of contents dropped 3 of 23 chapters (8, 20, 23: titles that wrap to a second line). The bookmark outline had all 23. This is the measurement behind outline-first ingestion (ADR-0008).

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **Separate app on the Jupyter kernel protocol** | Own UI and flow; real kernels; notebooks stay portable | Notebook UI must be built (ADR-0013) |
| JupyterLab extension (Jupyter AI, notebook-intelligence) | Notebook UI for free | UI is JupyterLab's; staged pipeline, tutor loop, habit tracker and sharing do not fit its extension points |
| `%cc` magic in plain notebooks (jupyter-cc) | Minutes to start | No pipeline, gates, tracker, sharing or MCP; everything lives in cell outputs |

## Decision

A separate web application that speaks the Jupyter kernel protocol and stores lessons as plain `.ipynb`. The Bashir book is the pilot; Atomic Habits shapes the learning loop but is not a pilot course.

## Rejected options — why (numbers)

- **JupyterLab extension.** Of the 10 v1 capabilities in the scope table, only 2 (notebook UI, kernel) come from JupyterLab; the other 8 would be custom panels inside a host UI the owner cannot restyle through Storybook, which contradicts the owner's UI requirement.
- **`%cc` magic.** Covers 1 of 10 capabilities (a tutor in the notebook) and has no place for owner gates.

## Resolved questions (owner, 2026-11-20)

1. Chapter 23 budget: delegated to the agent; 5 sessions, see above.
2. Course content is versioned as immutable revisions in Postgres and blobs (ADR-0007); git export is a later feature, not the store.
