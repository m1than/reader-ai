---
id: ADR-0016
title: Sharing, roles and paired study and teaching programs
status: proposed
date: 2026-11-20
verified_by: "SRV-SHARE tests: role matrix enforced on every endpoint and MCP tool; agent requests run on the acting user's credentials only; personal-use source files are never served to another user"
revisit_when: "A program needs more than 20 participants, or simultaneous text editing of the same cell"
---

## Context

Owner decision, 2026-11-20: share materials, run paired study and teaching programs, and render all of it correctly.

Constraints:

- ADR-0005: another person's requests must never run on the owner's subscription.
- Copyright: the Bashir PDF is the owner's personal copy; sharing must not distribute it.
- ADR-0010: paired programs serve the "make it attractive" law (a group where the habit is normal) and accountability (never miss twice).

## Decision

**Roles per course**

| Role | Read lessons | Run labs | Tutor | See others' progress | Comment on cells | Edit lessons, publish |
| --- | --- | --- | --- | --- | --- | --- |
| viewer | ✓ | — | — | — | — | — |
| learner | ✓ | ✓ | own credentials | own only | ✓ | — |
| mentor | ✓ | ✓ | own credentials | cohort | ✓ (assign, review) | — |
| editor | ✓ | ✓ | own credentials | cohort | ✓ | ✓ (gates stay with owner) |
| owner | all | all | own credentials | all | all | all, plus G1/G2 |

**Sharing.** Invitation links carry a role and an expiry; recipients sign in with a passkey. A shared course shares lessons, findings and labs. Source files with `license: personal-use` are never served to others; a learner who wants the reading route attaches their own copy, matched by sha256.

**Own materials, own provider.** Every invited person connects their own materials and their own provider (ADR-0005). Every agent action (tutor, lab hints, compiling their own materials) runs on the credentials of the person it acts for. A person with no provider connected is a **viewer** whatever role the invitation carried: they can read and follow, but no agent runs for them. Connecting a provider upgrades them to the invited role.

**Shared pages are read-only by default (owner, 2026-11-20).** A page shared from the platform by link reaches a person who was not invited, has no plan of their own and has connected no provider. That person is a viewer: they read the page, and nothing runs for them (no tutor, no labs, no compile). Getting more requires an invitation plus either a purchase or a connected provider.

**Mentors follow stages.** A mentor sees each learner's position in the plan, gate status of the learner's own courses, recall accuracy (if the learner opts in) and missed days.

**Paired program** = a cohort on one course with a schedule:

- shared progress board (who finished which lesson, recall accuracy if the member opts in);
- pair sessions: participants join one kernel and one session chat; each person's tutor turns run on that person's credentials, and the chat shows both;
- check-ins: a missed day can notify the partner, opt-in per member (accountability, ADR-0010);
- teaching mode: a mentor assigns lessons and labs, reviews submissions and comments on cells.

**Presence and updates** go through the existing event stream (SSE); the shared kernel uses the same authenticated WebSocket relay with per-participant execution attribution. Learner notes are private unless the learner shares a cell.

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **Role matrix per course + cohorts + per-user credentials** | Covers sharing, pairs and mentoring; respects provider terms and copyright | Role checks on every endpoint and MCP tool |
| Public read-only links only | Simplest | No pairs, no mentoring, no shared labs |
| CRDT co-editing (Yjs) of notebooks | Google-Docs-style editing | Merge semantics with pipeline-owned cells; not needed for study |

## Rejected options — why (numbers)

- **Public links only.** Covers 1 of 3 requested capabilities (sharing), not paired study or teaching.
- **CRDT co-editing.** Solves simultaneous authoring, which v1 does not have (lessons are authored by the pipeline and published at gate G2); its cost is a merge layer over every cell type.

## Resolved questions (owner, 2026-11-20)

1. A partner without a provider takes part read-only. Without their own subscription or key, there is nothing for an agent to run on.

## Open questions

1. Who is in the first paired program, and on which course? This decides the acceptance test of the sharing stage.
