---
id: ADR-0036
title: Owner settings editable in the web UI as a database overlay over reader.yaml
status: proposed
date: 2026-11-25
verified_by: "SRV-CONFIG FR-CONFIG-26 [unit] merge and validation table and [db] set, stale, reset and refusal cases; the sign-up switch turned off by an override answers 404 without a restart; WEB-MANAGE FR-MANAGE-11 [browser]; `reader status` prints every override"
revisit_when: "An override is ever found disagreeing with reader.yaml without the `reader status` line (one occurrence); the allow-list is asked to grow beyond 5 key families; the stack listens on anything other than 127.0.0.1"
supersedes: []
---

> Owner decision, 2026-11-25 (relayed by the coordinator): option B, settings editable from the UI. The owner sets
> this ADR's status himself after the review; this file's status is not changed by the agent. Until it reads
> `accepted`, WEB-MANAGE FR-MANAGE-6 shows these settings read-only and FR-MANAGE-11 / SRV-CONFIG FR-CONFIG-26 wait.
>
> Drafted with WEB-MANAGE (V/manage, 2026-11-25).
> Amends, if accepted: ADR-0011 (`proposed`, "one file declares instance settings … budgets … sharing defaults") and
> SRV-AUTH FR-AUTH-23 ("the switch is read at startup only; changing it needs an api restart, as every reader.yaml
> value does"). No accepted ADR is contradicted.

## Context

Owner, 2026-11-25 (in Russian, paraphrased): the tool has no edit buttons, no customization and no deletion. The
management layer (WEB-MANAGE) adds them for courses, lessons, materials, users and personal settings. Some of the
settings the owner expects on a settings page live in reader.yaml today:

| Setting | reader.yaml key | Owner | Read when |
| --- | --- | --- | --- |
| Sign-up mode | `sharing.signup` | SRV-AUTH FR-AUTH-23 | api start only |
| Trace retention | `observability.retention_days` (1..365, default 30) | SRV-CONFIG FR-CONFIG-17, SRV-OBS FR-OBS-11 | worker start and the daily job |
| Monthly provider budget | `budgets.monthly_usd`, `budgets.warn_at` | SRV-AGENT | each job (`load_config` per call) |
| Per-stage model and budget | `tasks.<stage>.model`, `tasks.<stage>.budget_usd_per_lesson` | SRV-AGENT | each job |

Two more caps the owner calls "budget caps" are spec constants, not configuration: the compile run cap
(SRV-PIPELINE `bounds.run_cap_usd: 8.00`, with a per-request `cap_usd` override) and the tutor caps (SRV-TUTOR
`question_cap_usd 0.30`, `daily_cap_usd 3.00`). "Test" and "live run" budgets are the agent's project budgets in
docs/progress.yaml, not application settings.

Facts that constrain the choice:

- reader.yaml is mounted read-only into every container (OPS-DEPLOY FR-DEPLOY-5); no process writes it.
- It is the owner's declarative source of truth, kept beside the repository and reviewed as text (ADR-0011).
- Several values are already re-read per request or per job (`load_config` in ingest, plan, search, tutor and agent
  code), others only at start (user sync, `sharing.signup`, observability).

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| A. Read-only page (WEB-MANAGE FR-MANAGE-6 as drafted) | The owner sees every effective value, its key and the command that applies a change; one source of truth | Each change is a file edit plus `reader up` (about 1 min of restart for start-only keys, hypothesis) |
| B. Database overlay for an allow-list of keys | Edit from the page; effective value = overlay ?? reader.yaml ?? default; the page shows which source wins | Two sources of truth; a new `settings_overrides` table, an audit row per change, a reload path for start-only keys (sign-up switch, retention), tests for precedence |
| C. The api writes reader.yaml | One source of truth, edited from the page | The file becomes writable from a network-facing process; comments and formatting lost on rewrite; contradicts the read-only mount |

## Decision (proposed)

Option B for this allow-list only: `sharing.signup`, `observability.retention_days`, `budgets.monthly_usd`,
`budgets.warn_at`, `tasks.<stage>.budget_usd_per_lesson`. Model choice per stage stays file-only (a model id names a
provider contract and pricing row; a typo there costs credit). Users, providers, secrets, materials and kernels stay
file-only. An overlay value is validated by the same Pydantic model as the file; the page always shows both the file
value and the override, and "Reset to reader.yaml" deletes the override. Every change requires a recent sign-in
(SRV-AUTH `recent_session`) and writes one audit row. `reader up` prints a line for each key whose override differs
from the file, so the file never silently disagrees with the running instance.

Until the owner accepts this ADR, Option A ships.

## Rejected options — why

- C: writing a secret-adjacent file from the api widens what a compromised api process can change (providers and
  secret references live in the same file). The read-only mount is a deliberate boundary (ADR-0026).

## Consequences

- Easier: the owner turns sign-up off, lowers the monthly budget or shortens trace retention from the page.
- Harder: two sources of truth; every reader of an allow-listed key goes through one `effective_settings()` function,
  and the start-only readers need a reload hook (SRV-AUTH re-reads the sign-up switch per request instead of at
  start; SRV-OBS reads retention per job run).
- Forbidden: an override for any key outside the allow-list; an override that the file's schema would refuse.

## Open questions

1. Accept B, or keep A (file plus `reader up`) for v1?
2. Should model choice per stage join the allow-list, limited to the model ids in the pricing table?
3. Should the compile run cap and the tutor caps become reader.yaml keys first (they are spec constants today)?
