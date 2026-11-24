---
id: ADR-0036
title: Owner settings editable in the web UI as a database overlay over reader.yaml
status: proposed
date: 2026-11-24
verified_by: "SRV-CONFIG FR-CONFIG-26 [unit] merge, narrowing and validation table and [db] set, stale, reset and refusal cases; the sign-up switch turned off by an override answers 404 without a restart, and an override cannot open sign-up that the file turns off; WEB-MANAGE FR-MANAGE-11 [browser]; `reader status` prints every override"
revisit_when: "An override is ever found disagreeing with reader.yaml without the `reader status` line (one occurrence); the allow-list is asked to grow beyond 5 key families; the stack listens on anything other than 127.0.0.1"
supersedes: []
---

> Owner decision (relayed by the coordinator, 2026-11-24): option B, settings editable from the UI. The owner sets
> this ADR's status himself after the review; the agent does not change it. Until it reads `accepted`, WEB-MANAGE
> FR-MANAGE-6 shows these settings read-only and FR-MANAGE-11 / SRV-CONFIG FR-CONFIG-26 are not built.
>
> Drafted with WEB-MANAGE (V/manage, 2026-11-24). Revision 1 after deep-review round 1 (840af4b): the amended
> decisions listed in full; the file's `signup: off` always wins; backup and restore; which processes read the
> overlay; audit rows hold the values.
>
> Amends, if accepted:
> - ADR-0031 (accepted) §1 "Switch": `sharing.signup` is read from reader.yaml at start; with this ADR it is also
>   narrowed by an override, read per request.
> - ADR-0032 (accepted) §6 "Retention": "configurable from 1 to 365" in reader.yaml; with this ADR also by an
>   override, read at each run of the retention job.
> - ADR-0011 (`proposed`): "one file declares instance settings … budgets … sharing defaults".
> - SRV-AUTH FR-AUTH-23: "the switch is read at startup only; changing it needs an api restart, as every reader.yaml
>   value does".

## Context

Owner, 2026-11-24 (in Russian, paraphrased): the tool has no edit buttons, no customization and no deletion. The
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
| A. Read-only page (WEB-MANAGE FR-MANAGE-6) | The owner sees every effective value, its key and the command that applies a change; one source of truth | Each change is a file edit plus `reader up` (about 1 min of restart for start-only keys, hypothesis) |
| B. Database overlay for an allow-list of keys | Edit from the page; the page shows which source wins | Two sources of truth; a new `settings_overrides` table, an audit row per change, per-request or per-run reads for start-only keys, tests for precedence |
| C. The api writes reader.yaml | One source of truth, edited from the page | The file becomes writable from a network-facing process; comments and formatting lost on rewrite; contradicts the read-only mount |

## Decision (proposed)

Option B for this allow-list only: `sharing.signup`, `observability.retention_days`, `budgets.monthly_usd`,
`budgets.warn_at`, `tasks.<stage>.budget_usd_per_lesson`. Model choice per stage stays file-only (a model id names a
provider contract and pricing row; a typo there costs credit). Users, providers, secrets, materials and kernels stay
file-only.

- **Precedence.** effective = override ?? file ?? default, validated by the same Pydantic model as the file.
- **Narrowing only for the sign-up switch.** The file's value is resolved first by SRV-AUTH FR-AUTH-23's rule,
  defaults included (an absent key means open only on `localhost`, off on any other host). When that resolved value
  is off, sign-up is off whatever the override says; an override can turn sign-up off when the file leaves it open,
  never open it when the file closes it. The file is the owner's hard boundary for who can create an account.
- **Budgets in either direction.** The budget keys take an override that raises them as well as one that lowers
  them. That is accepted: a raise needs a recent sign-in and leaves an audit row with the old and new values, and
  `reader status` prints it beside the file value.
- **Ignored overrides.** An override that no longer validates against the current file (its task removed, a bound
  changed) or that the narrowing rule overrides is not applied; it is logged once per process start, shown as
  ignored on the page and printed by `reader status` as `ignored (<reason>)` until the owner resets it.
- **Changes.** Every change requires a recent sign-in (SRV-AUTH `recent_session`) and writes one audit row with the
  key, the old and the new value (the values are small numbers or enums, not secrets; a hash of them would be
  trivially reversible and only hide them from the owner). "Reset to reader.yaml" deletes the override.
- **Visibility.** The page shows the file value beside every override. `reader up` and `reader status` print one
  line per override, so the file never silently disagrees with the running instance.
- **Who reads the overlay.** Only the api and the worker, which hold a database connection: the api reads the
  sign-up switch per request and serves the settings routes; the worker reads retention at each run of the retention
  job and the budgets at each agent call's budget check. A process without a database connection (the agent proxy
  of ADR-0025, the host CLI `reader-ingest`) never reads an allow-listed key; if one ever needs such a key, that key
  leaves the allow-list first.
- **Backup and restore.** Overrides live in Postgres, so `reader backup` keeps them and `reader restore` brings them
  back with the rest of the database (OPS-CLI FR-CLI-17/-18). After a restore, `reader status` lists them; a restored
  override whose key the current reader.yaml no longer has (a task removed from `tasks`) is ignored and listed as
  `override <key>: ignored (not_in_file)`, like any ignored override above.

Until the owner accepts this ADR, option A ships.

## Rejected options — why

- C: writing a secret-adjacent file from the api widens what a compromised api process can change (providers and
  secret references live in the same file). The read-only mount is a deliberate boundary (ADR-0026).

## Consequences

- Easier: the owner turns sign-up off, lowers the monthly budget or shortens trace retention from the page.
- Harder: two sources of truth; every reader of an allow-listed key goes through one `effective_settings()` function.
- Forbidden: an override for any key outside the allow-list; an override that the file's schema would refuse; an
  override that opens sign-up the file closes.

## Open questions

1. Should model choice per stage join the allow-list, limited to the model ids in the pricing table?
2. Should the compile run cap and the tutor caps become reader.yaml keys first (they are spec constants today)?
