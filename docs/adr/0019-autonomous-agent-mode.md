---
id: ADR-0019
title: Autonomous agent mode — the agent approves specs and never waits; the owner reviews after
status: proposed
date: 2026-11-20
verified_by: "tools/hooks/pre-write.mjs scenarios: the agent can set a spec to approved, cannot set implemented, cannot raise any ADR status; docs/progress.yaml is updated on every merged task; the owner's stage live check passes before any spec becomes implemented"
revisit_when: "A stage's live check fails because of a spec the agent approved, twice in a row; or the agent merges a change the owner reverts"
supersedes: []
---

## Context

> Amendment proposed by [ADR-0020](0020-knowledge-layers-and-amendment-rules.md) (2026-11-25): a PR that adds or amends an ADR is merged by the owner, like a PR that changes an ADR status. In force once ADR-0020 is accepted; then, where the two disagree, ADR-0020 wins.

Owner decisions, 2026-11-20:

- The environment must be ready so the owner hands the work to the agent and it does everything needed, without stopping, and brings the system up.
- Specs: the agent approves them itself; the owner reviews after, on the stage live checks.
- Permissions: Claude Code auto mode.
- Merging: the agent merges its own pull requests, except the owner's gates.
- Start: the owner launches Claude Code from the terminal with a kickoff prompt and a shared AGENTS.md.

This changes ADR-0001 in three places and keeps everything else (spec-first, traceability, P7, CI).

## Decision

| Concern | ADR-0001 | Now |
| --- | --- | --- |
| Spec `draft → approved` | Owner | **Agent**, after the spec passes trace-check and the agent's own review against its ADRs; the spec records `approved_by: agent` and the date |
| Spec `approved → implemented` | Owner | Owner, after the stage live check (`e2e/live/stage-N.sh`) passes on his Mac; this is the review after |
| ADR `accepted` / `rejected` / `superseded` | Owner | Owner (unchanged) |
| Merging pull requests | Owner | **Agent**, when CI is green and the PR does not change an ADR status; PRs that change an ADR status are merged by the owner |
| Waiting for the owner | Stop at every fork | **Never wait.** A question goes into `docs/progress.yaml` as a blocker and a Telegram message; the agent takes the next ready task |

Unchanged rules: H1 spec-first (code only inside the `paths` of an approved spec), H2 / P7 (tests change with their spec), T1–T8 in CI, no secrets anywhere, no copyrighted material in the repository.

### What still stops a task (not the whole run)

The agent marks the task `blocked` with the reason, notifies the owner, and moves on. Any of these blocks a task:
- an action outside this repository that cannot be undone, such as publishing a package, a release in another repository, or deleting data outside `data/`;
- spending above the live-run budget in `docs/progress.yaml` (Agent SDK credit for live checks and spikes);
- a missing secret or access, for example a provider token or another repository;
- a decision that contradicts an accepted ADR, which needs a new proposed ADR first;
- the same check failing 3 times after 3 different fixes.

### Run state

`docs/progress.yaml` is the single source of what is done, in progress, blocked and next, in roadmap order. The agent updates it in the same PR as the work. A new session resumes from it.

### Notifications

`tools/agent/notify.mjs` sends a Telegram message through the owner's bot. It reads the token from the host file itself; the agent never reads the token. The agent notifies:
- when a stage reaches its exit;
- when a task is blocked;
- when a PR needs the owner (ADR status);
- when the session stops (Stop hook).

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **Agent approves specs, owner reviews after** | No idle time waiting for approvals | A wrong spec is found at the stage live check, not before the code |
| Approval queue, owner approves in batches | The owner's lever stays before the code | The agent idles or works ahead on unapproved specs; owner chose against it |
| Engineering specs by the agent, product and AI by the owner | Mixed | Two regimes to remember |

## Rejected options — why (numbers)

- **Approval queue.** With 28 specs and one batch a day, the dependency chain (stage 1 → 2 → 3 …) serialises on the owner's daily slot. At least 9 stage boundaries each wait up to a day; the owner rejected the waiting.
- **Mixed regime.** 12 product and 6 AI specs would still wait for the owner, which is 18 of 28.

## Consequences

- The spec is still the first artifact of every module, and CI still enforces traceability; what moves is who flips `approved`.
- The owner's review moves to stage exits: live checks plus `status: implemented`. A failed live check sends the stage back with the owner's notes as blockers.
- Hook H3 allows the agent `draft → approved` for specs only.
