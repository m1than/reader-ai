---
id: ADR-0001
title: specd-lite process, roles and enforcement
status: proposed
date: 2026-11-20
verified_by: "tools/trace/check.mjs (T1–T8) and guard-tests.mjs (P7) in CI; tools/hooks/pre-write.mjs (H1–H3) in .claude/settings.json; scenarios in tools/README.md"
revisit_when: "A hook or CI false positive more than once a week, or a process violation that reaches main"
---

## Context

> Amended by [ADR-0019](0019-autonomous-agent-mode.md) (2026-11-20): the agent approves specs and merges its own PRs; the owner keeps `implemented` and ADR decisions. Where the two disagree, ADR-0019 wins.

Owner decision, 2026-11-20: reader-ai is built with the same specd-lite process as web3-noncustody, and Claude builds it from this ADR package. Two things differ from web3-noncustody:

1. The agent writes the implementation. The owner's lever is the spec and the ADR, not the code.
2. The product is multi-component (Python service, web UI, sandbox, deployment), so the spec must also say which files a module owns.

## Process

```
ADR (proposed) → owner accepts
  → spec draft (paths, normative sources, FR/INV/NFR/VEC) → owner approves
    → failing tests with @trace (Red) → implementation (Green) → refactor
      → owner marks spec implemented
```

Unknown numbers are measured before the spec is approved, in `spikes/<name>/` with a `RESULT.md` that the ADR or spec cites. Spikes never ship and are never imported by `server/` or `web/`.

## IDs and locations

| Kind | Pattern | Example |
| --- | --- | --- |
| Module | `(SRV|WEB|OPS)-<NAME>` | `SRV-INGEST`, `WEB-NOTEBOOK`, `OPS-DEPLOY` |
| Item | `(REF|FR|INV|NFR|VEC)-<NAME>-<n>` | `FR-INGEST-3` |
| ADR | `ADR-NNNN` | `ADR-0009` |
| Tests | `server/tests/**/test_*.py`, `web/**/*.test.ts(x)`, `e2e/**/*.spec.ts`, `tools/dod/*.test.mjs` (node:test, OPS-DOD) | |
| Implementation | `server/src/**`, `web/src/**`, `sandbox/**`, `deploy/**` | |

Every spec declares `paths`: globs of the files the module owns.

Every spec also declares a `track`, so that planning, review and the backlog keep three kinds of work apart (owner, 2026-11-20):

| Track | Covers |
| --- | --- |
| `product` | User-facing features: what a learner, mentor or owner can do |
| `ai` | Agents, prompts, models, retrieval, evaluation, traces of agent runs, local models and tuning |
| `engineering` | Platform, deployment, security, performance and cost optimization |

## CI rules (tools/trace)

| ID | Rule |
| --- | --- |
| T1 | Spec is well-formed: id, status, title, `paths`, `track`, at least one normative source, item ID format, every NFR has a numeric threshold and verified_by |
| T2 | IDs are unique across the repository |
| T3 | Every test file contains `@trace` |
| T4 | `@trace` references only declared IDs |
| T5 | In an approved/implemented spec every FR, INV, VEC (and NFR with `verified_by: test`) is covered by a test |
| T6 | ADR is well-formed; accepted requires verified_by and revisit_when |
| T7 | Specs reference only existing ADRs |
| T8 | Spec-first: every implementation file is claimed by the `paths` of an approved/implemented spec |
| T10 | The Definition-of-Done index (spec/fixtures/OPS-DOD/operations.yaml) is well-formed and matches the specs, tests, live labels and the route and CLI registries; an `enforced` operation has no `missing` cell (OPS-DOD FR-DOD-3) |
| P7 | On a PR, a test traced to an approved spec changes only together with that spec; escape hatch: commit trailer `Spec-Neutral: <reason>` |
| P8 | On a PR, the Definition of Done is not loosened (an operation demoted or removed, a budget loosened, a new `not_user_facing` entry, a new or changed waiver, …); owner's path: commit trailer `DoD-Owner-Change: <reason>` (OPS-DOD FR-DOD-13) |

## Hook rules (tools/hooks/pre-write.mjs, Write/Edit/MultiEdit)

| ID | Rule |
| --- | --- |
| H1 | Spec-first: an implementation file is writable only if an approved/implemented spec claims it |
| H2 | A test traced to an approved spec changes only if the spec is modified in the working tree |
| H3 | The agent never raises status: spec to approved/implemented, ADR to accepted/rejected/superseded |
| H4 | The agent never adds or changes a waiver: a write of the Definition-of-Done index whose set of `waived` cells gains or changes a cell is refused (OPS-DOD FR-DOD-12) |

The hook fails closed when its dependencies are missing. Writes through Bash bypass the hook; T8 and P7 in CI close that gap, and CLAUDE.md forbids project writes through Bash.

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **Spec-first gate (H1 + T8)** | The owner controls what gets built through one artifact, the spec | ~40 lines of tooling; every new file needs a spec claim |
| Owner reviews every PR diff | Full control | At 60 min/day the owner's budget goes to review instead of study |
| No gate, review after merge | Fastest | Code without a spec defeats the spec as the source of truth |

## Decision

specd-lite with a spec-first gate: the owner approves ADRs and specs; the agent writes tests and code only inside approved `paths`.

## Rejected options — why (numbers)

- **Owner reviews every diff.** The owner's study budget is 60 min/day (2026-11-06). SmartBear's Cisco code-review study puts effective review speed below 500 lines per hour, so a 500–1,000 line module costs 1–2 hours: 100–200% of a day's budget per module.
- **No gate.** web3-noncustody measured that hooks plus CI catch violations at write time (17 of 17 scenarios in its tools/README.md). Without a gate, the first signal is a human noticing after merge.

## Consequences

- Specs are the only owner-facing artifact per module; they must be short and exact.
- An unclaimed file (for example, a new shared utility) forces a spec change: the duplication is visible, not silent.
