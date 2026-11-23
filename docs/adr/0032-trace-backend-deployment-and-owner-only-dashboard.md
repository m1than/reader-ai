---
id: ADR-0032
title: Trace backend deployment — MLflow on its own SQLite volume, the dashboard on a second origin behind the api's owner gate, one trace per job
status: proposed
date: 2026-11-24
verified_by: "SRV-OBS tests: INV-OBS-5 (401/403 through Caddy before MLflow; no edge route to ingest), INV-OBS-4 (one linked trace per job), INV-OBS-2 (no canary in any export, CLI included), FR-OBS-8 [image] start-up; the stage-2 live check finds a tutor trace through MLflow's API"
revisit_when: "MLflow's idle or peak memory exceeds its 1g limit in the stage-2 live check; or a second user needs the dashboard (ADR-0016/0017); or a reader-ai least-privilege Postgres role exists (then the Postgres store is reconsidered)"
supersedes: []
---

## Context

The owner asked on 2026-11-24 for "the tracing system via dashboard for this system".

ADR-0018 (**proposed**, not accepted) puts traces in v1:
- Rotel as the collector;
- a trace backend chosen by a stage-0 spike;
- the Agent SDK's own spans nested under ours through `TRACEPARENT`.

It describes MLflow as "one server on our Postgres". The spike `spikes/trace-backend` (2026-11-20) passed for MLflow and Phoenix. It recommended MLflow in its minimal configuration (`--workers 1`, job execution off), on the licence (Apache-2.0 against Phoenix's ELv2). It left five things open, which this ADR decides:
- persistence;
- access to the UI (MLflow has no auth in that configuration);
- the trace shape for jobs;
- whether MLflow can show the owner's views;
- what the CLI exports.

Measurements behind this ADR, all zero credit:
- `spikes/cli-otel-content` (2026-11-24): the CLI's export under SRV-AGENT's exact environment.
- `spikes/obs-stack` (2026-11-24):
  - Rotel → hardened MLflow on SQLite and on Postgres;
  - Caddy `forward_auth`;
  - a lesson compile replayed in real time;
  - storage per span;
  - deletion.
- MLflow 3.18.0's source, read in the pinned image.

## Options

### Store

| Option | Gives | Costs (numbers) |
| --- | --- | --- |
| **A. SQLite on MLflow's own volume** | No secret in MLflow. MLflow on no network with Postgres. The stock image plus a 2-line derived image (a non-root data directory). The measured configuration | A second store to back up (a labelled volume). SQLite is single-writer, which is fine at `--workers 1` and ≈ 3 spans/s |
| B. A database in reader-ai's Postgres (ADR-0018's text) | One backup set | The stock image has no Postgres driver: `import psycopg2` fails in `ghcr.io/mlflow/mlflow:v3.18.0`, and with `postgresql://` MLflow exits at start with "No module named 'psycopg'" (measured). A derived image with a hash-locked driver is needed. MLflow would need the database password, which makes it a secret consumer (ADR-0026 delivery, wait wrapper). It would also join `core`, where today the only role is the `postgres` superuser (SRV-AUTH note 2): a third-party UI server would hold the app database's superuser password. Memory: no gain (below) |

### Dashboard access

| Option | Gives | Costs (numbers) |
| --- | --- | --- |
| **A. Second origin `https://localhost:8443`, Caddy `forward_auth` to `GET /api/obs/gate`** | The owner's existing session is reused: cookies are not isolated by port (RFC 6265bis), so the host-only `__Host-reader_session` cookie reaches 8443. MLflow's JavaScript runs outside the app's origin, so a flaw in it cannot call `/api/*` with the owner's session: SRV-AUTH's Origin check refuses every non-GET from `https://localhost:8443`. Measured: 401 without a cookie, 403 for a learner, 200 for the owner, 404 for blocked paths, 403 from MLflow for a foreign Host | One more published loopback port. The site's CSP must be measured against the UI in a browser |
| B. Same origin, path `https://localhost/traces/` (`--static-prefix`) | One port | MLflow's UI and its 3,000+ dependencies run on the app's origin. Any script injection there passes the app's Origin check and acts as the owner on every `/api/*` route |
| C. The api reverse-proxies MLflow | No Caddy change | Same origin as B. Every UI byte streams through the api's Python process |
| D. Subdomain `traces.localhost` | Origin isolation | The `__Host-` cookie is host-only and is not sent to a subdomain, so a second sign-in flow would be needed |
| E. MLflow's basic-auth app | Built in | A second credential to manage as a secret, and still not tied to the owner's passkey session |

### Trace shape for jobs

| Option | Gives | Costs |
| --- | --- | --- |
| **A. One trace per job, linked to the enqueuing span** | Each stage, answer or import is one trace whose root covers the whole run. The trace id is derived from (job id, key), so the owner's views compute links without storing them | Retries and the next stage are separate traces, joined by links |
| B. The job as a child of the request's trace | One trace per request chain | The root (the request) ends milliseconds after it starts, while its children run for minutes. Sweep re-enqueues have no request to belong to. MLflow computes a trace's duration from its root |

### The owner's views

| Option | Gives | Costs |
| --- | --- | --- |
| **A. `GET /api/obs/summary` from reader-ai's tables, plus an owner page linking to MLflow traces** | Cost per stage per lesson, tutor p50/p95 and cost, failures by error code, ingest pages per minute, all from the data of record (ADR-0018 rule 1: never from telemetry, which is lossy) | One route and one page |
| B. MLflow's built-in trace metrics | No code | In 3.18.0, span cost is grouped only by model name or provider, and trace count only by trace name or status (`sql_trace_metrics_utils.py`). Neither cost per stage nor failures by code can be expressed |

## Decision

1. **Store:** MLflow keeps SQLite on its own volume `obs_mlflow` (option A), excluded from `reader backup` (traces are diagnostic and rebuildable going forward; the data of record is in Postgres). Rotel and MLflow run in Compose profile `observability`, on three internal networks with explicit subnets:
   - `otel`: api, worker and rotel;
   - `obs`: rotel, mlflow and the worker (retention only);
   - `obs-ui`: caddy and mlflow.

   So caddy never reaches rotel or the worker, and the worker never reaches caddy. Neither service has a secret, a published port or egress. Both run non-root with a read-only root, `cap_drop: [ALL]` and equal memory and swap limits.

   **Amends ADR-0025 rule 5 (routes):** the worker also reaches `mlflow`, for the retention job's delete calls only. ADR-0025 already lists Rotel among the worker's routes.
2. **Access:** the UI is served only at `https://localhost:8443`, through Caddy, after `forward_auth` to the api's `GET /api/obs/gate`:
   - 204 for the owner;
   - 401 without a session;
   - 403 for any other user.

   The gate also checks where a request comes from, because MLflow 3.18.0 answers any `http(s)://localhost:*` Origin with credentialed CORS (measured: `Access-Control-Allow-Origin: http://localhost:3000` on a direct request). It answers 403 when an `Origin` header is present and is not `https://localhost:8443`, and when the original method is not GET or HEAD and `Origin` is not exactly that value. Caddy strips every `Access-Control-*` header from MLflow's responses, removes `Cookie` and `Authorization` on the way up and `Set-Cookie` on the way down, and answers 404 itself for MLflow's ingest, gateway, assistant and MCP paths. MLflow's `--allowed-hosts` accepts only `localhost:8443` from the edge (option A). Port 8443 is published only while the profile is active (`reader up`'s override).

   The site's CSP was measured in a headless browser. MLflow's UI renders with no violation under `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; worker-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'`, and makes no request to any other host. The ceiling stays: no script relaxation, no other host, `connect-src 'self'`. An MLflow upgrade that needs more waits for the owner.
3. **Trace shape:** one trace per job, with a link to the span that enqueued it (option A). The CLI's spans nest under our `agent.call` span through `TRACEPARENT`.
4. **The owner's views:** a summary API over reader-ai's own tables, plus an owner page that links into MLflow (option A).
5. **Privacy:**
   - **Our spans:** one exporter rebuilds every span against a declared allow-list.
   - **The CLI's spans** pass outside that exporter. They are bounded by the CLI's own flags:
     - the content flags are set to 0 (raw API bodies left unset);
     - detailed beta tracing is never enabled;
     - a unit test fails CI when the locked `claude-agent-sdk` differs from the version the canary fixture was measured on, so a pin change reruns the canary; it also reruns if any call ever declares tools.

     Measured on CLI 2.1.330: no prompt, system prompt, response or credential canary appears in the export, and the positive control finds the prompt canary.
6. **Retention:** traces older than 30 days (configurable from 1 to 365) are deleted by a daily worker job through MLflow's `delete-traces` API.

## Rejected options — why (numbers)

- **Store B (Postgres).**
  - It needs a derived image with a database driver (none in the stock image, measured).
  - It hands the app database's superuser password to a third-party UI server on `core`.
  - It buys nothing at this load:
    - MLflow peaked at 440 MiB on Postgres and 443 MiB on SQLite over the same 8-minute lesson replay plus a 10,080-span bulk, and Postgres added 45–70 MiB of its own;
    - SQLite stored 927 bytes per span, so a whole-book compile (≈ 11,000 spans) is ≈ 10 MB.
- **Access B and C (same origin).**
  - The UI's JavaScript would be same-origin with the app, and the app's only cross-site guard for state changes is the Origin check, which a same-origin script passes.
- **Access D (subdomain).**
  - The session cookie cannot reach it without a second sign-in.
- **Access E (basic auth).**
  - A second credential, not tied to the passkey.
- **Trace shape B (child of the request).**
  - The root ends before its children start.
  - Sweep re-enqueues have no parent.
- **Views B (MLflow's metrics).**
  - The dimensions are fixed to model and provider (cost) and to name and status (counts) in 3.18.0.

## Consequences

- **Easier:**
  - One click from a slow stage or a failed answer on the owner page to its trace.
  - Tracing can be turned off with one config key, which removes rotel and mlflow on the next `reader up`.
  - Switching to Phoenix changes only Rotel's exporter flags and the gate's upstream. Phoenix's own auth (on by default since v21) would need a key, delivered as a secret.
- **Harder:**
  - The owner trusts a second loopback port (same certificate, same host).
  - Resource cost when on (the default): about 1.25 GiB of memory limits (mlflow 1g, rotel 256m) and two image pulls.
- **Forbidden:**
  - MLflow reachable other than through the gate.
  - MLflow on a network with postgres, embed, agent-proxy or any kernel.
  - Any span attribute outside the allow-list from the api or worker.
  - Enabling the CLI's content flags or detailed beta tracing.
  - OTel metrics or logs export, until a spec adds them.
- **ADR-0018:** this ADR changes one line of its text, "one server on our Postgres", to "its own SQLite volume". ADR-0018 is itself still proposed.

## Open questions

1. Accept a second loopback port (8443) for the dashboard? The alternative is option B or C, with the same-origin risk above.
2. Retention: 30 days by default. Traces are diagnostic; evaluation data stays in the ledger and run records. Do you want longer?
3. ADR-0018 is still `proposed`. Accepting this ADR without it leaves the bus and trace groundwork formally undecided.
