# Spike: obs-stack — RESULT

**Questions (SRV-OBS, ADR-0032):**
- What does MLflow use in memory over a minutes-long compile, with SQLite on its own volume and with Postgres?
- Do Rotel and MLflow run hardened (non-root, read-only root, cap_drop ALL)?
- Does the Caddy `forward_auth` owner gate work in front of the MLflow UI?
- How many bytes does a span take in the store?
- Does deletion by timestamp work?

**Verdict: PASS, zero credit.**
- **Memory:** MLflow peaked at 440–443 MiB with either store (443 × 1.25 = 554 MiB, under a 1g limit). Rotel stayed at
  25 MiB or less, against a 256m limit.
- **Hardened start:** both services start and run under the hardening.
- **Owner gate:** 401 without a cookie, 403 for a learner, 200 for the owner (UI and ajax API), 404 for the blocked
  ingest and gateway paths, and 403 from MLflow for a foreign Host.
- **Traces:** every replayed trace arrived complete.
- **Storage:** 927 bytes per span in SQLite.
- **Deletion:** `delete_traces` removed every trace in 0.05–0.08 s.

Date: 2026-11-24.
- **Command:** `bash spikes/obs-stack/run.sh` (`BACKENDS`, `DURATION_S`, `BULK_LESSONS`).
- **Results:**
  - `results/20261124T003011/`: SQLite, 480 s replay. Its bulk phase overflowed the SDK queue, see Findings.
  - `results/20261124T004053/`: Postgres, 480 s replay.
  - `results/20261124T004941/`: SQLite, paced bulk, for bytes per span.

## Setup

Own Compose project `reader-spike-obs`, with no published port. The running `reader` stack was not touched.

| Part | Value |
| --- | --- |
| Rotel | `streamfold/rotel:v0.2.7`, its entrypoint (receivers 4317, 4318) plus `--exporter otlp --otlp-exporter-endpoint http://mlflow:5000 --otlp-exporter-protocol http --otlp-exporter-custom-headers x-mlflow-experiment-id=0`; uid 10005, read-only, tmpfs /tmp, cap_drop ALL, no-new-privileges, 256m |
| MLflow | `reader-spike-mlflow:local` = `ghcr.io/mlflow/mlflow:v3.18.0` + `psycopg2-binary` (for the Postgres variant only) + `/var/lib/mlflow` owned by 10004; `--workers 1`, `MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false`, `--allowed-hosts localhost:8443,mlflow:5000,mlflow`, artifacts on the volume; uid 10004, read-only, tmpfs /tmp, cap_drop ALL, 1g |
| Stores | SQLite at `/var/lib/mlflow/mlflow.db` on a named volume; or `postgresql+psycopg2://` to `postgres:18-trixie` (a throwaway spike password) |
| Edge | `caddy:2.11.9-alpine`, site `:8443` (plain HTTP; TLS not under test): `forward_auth gate:9000 { uri /api/auth/owner-gate }`, 404 for `/v1/* /gateway/* /ajax-api/3.0/mlflow/assistant/* /mcp* /api/*/mlflow/gateway/*`, else `reverse_proxy mlflow:5000` |
| Gate | `gate.py`: 204 for cookie `__Host-reader_session=owner`, 403 for `=learner`, 401 otherwise |
| Driver | `python:3.14-slim-trixie`, OTel SDK 1.45.2 (BatchSpanProcessor defaults), `mlflow-skinny` 3.18.0 client |
| Networks | two internal networks with explicit subnets (see Findings 1) |
| Memory | `docker stats --no-stream` of every container, one sample every 4–5 s in practice. It includes page cache, as in spikes/trace-backend |

**Load:**
- **Sustained:** one lesson compile shaped as SRV-OBS proposes, in real time over 480 s. That is 7 job traces
  (`pipeline.stage` root → `reader.stage` → 10 × (interaction → 3 llm_request + tool → 2)), 72 spans each, every
  root ending after its children. Alongside it:
  - a 3-span `tutor.answer` trace every 15 s (31 traces);
  - a UI-like poller: search plus get-trace every 5 s.
- **Bulk:** 20 lessons, 140 traces, 10,080 spans, flushed after each lesson.
- **Retention:** `delete_traces(experiment_id="0", max_timestamp_millis=now)`.

## Results

| Measure | SQLite volume | Postgres |
| --- | --- | --- |
| Ready after `up` | 4.7 s / 5.6 s | 4.6 s |
| MLflow during the sustained replay | 376–420 MiB, ≤ 29 PIDs | 381–391 MiB, ≤ 29 PIDs |
| MLflow peak, bulk | 433 / 443 MiB | 440 MiB, plus postgres ≤ 70 MiB |
| Rotel | 8–25 MiB, ≤ 18 PIDs | 8–16 MiB |
| Caddy | ≤ 20 MiB | ≤ 14 MiB |
| Stage traces complete (72 spans each) | 7 / 7, each ≈ 68.6 s long (480 / 7) | 7 / 7 |
| Tutor traces complete | 31 / 31 | 31 / 31 |
| Bulk traces complete | 140 / 140 (paced run) | 140 / 140 |
| Poller (search + get), n = 94 | p50 0.082 s, p95 0.204 s | p50 0.059 s, p95 0.165 s |
| Delete all traces | 132 in 0.06 s; 147 in 0.08 s | 178 in 0.05 s |
| Store size | 1,490,944 → 10,833,920 bytes for 10,080 spans: **927 bytes per span** | not measured (in the database) |
| Size after delete | unchanged (SQLite keeps freed pages for reuse) | — |

**Gate path, identical for both stores:**

| Request through Caddy | Status |
| --- | --- |
| `/` without a cookie | 401 |
| `/` as learner | 403 |
| `/` as owner | 200, the MLflow UI index |
| `POST /ajax-api/2.0/mlflow/experiments/search` as owner | 200 |
| `POST /ajax-api/2.0/mlflow/experiments/search` without a cookie | 401 |
| `POST /v1/traces` as owner | 404 at the edge |
| `/gateway/x` as owner | 404 at the edge |
| owner with `Host: evil.example` | 403 from MLflow's `--allowed-hosts` |

The gate received `X-Forwarded-Method` and `X-Forwarded-Uri` for every request, POSTs included, as GETs.

## Findings

1. **No free address pool.** On the owner's Docker Desktop, creating a network from the predefined pools fails with
   "all predefined address pools have been fully subnetted". New Compose networks need explicit subnets.
2. **Rotel's image entrypoint** already sets both receiver endpoints. Repeating `--otlp-grpc-endpoint` fails ("cannot
   be used multiple times"). The image runs as root by default and runs fine as uid 10005, read-only. It is Ubuntu
   22.04 with bash and no HTTP client, so a TCP healthcheck `bash -c 'exec 3<>/dev/tcp/127.0.0.1/4318'` is the
   available probe.
3. **MLflow on Postgres needs a driver:**
   - the stock image has neither psycopg2 nor psycopg 3;
   - with `postgresql://`, MLflow exited at start with "No module named 'psycopg'";
   - `postgresql+psycopg2://` plus `psycopg2-binary` works.
4. **The store makes no memory difference.** MLflow's own memory is the same within the sampling noise; Postgres adds
   its own 45–70 MiB.
5. **SDK queue overflow (round 1).** Emitting 10,080 spans in 0.3 s overflowed the OTel SDK's own 2,048-span queue
   ("Queue full, dropping Span"), so 94 of 140 bulk traces arrived and 12 of 20 sampled ones were complete. With a
   flush after each lesson, every span arrived. At v1 load (≈ 3 spans/s, ADR-0018) the queue never fills.
6. **Late roots work.** Children exported minutes before their root (BatchSpanProcessor every 5 s) are assembled into
   one complete trace. Its duration is the root's.
7. **Storage is small.** A whole-book compile (≈ 11,000 spans, ADR-0018) is ≈ 10 MB in SQLite. Retention is not a
   storage problem.

## Round 2 (SRV-OBS design review B2–B4, B6), 2026-11-24

- **Command:** `bash spikes/obs-stack/run-browser.sh`, with `CSP_CANDIDATE` for the candidate policy on :8446.
- **Results:** `results/20261124T012202-round2/` (`driver.json`, `browser.json`, `shots/`, `gate.log`,
  `caddy-adapt.json`).
- **Setup changes:**
  - Networks split as SRV-OBS FR-OBS-8 now has them: `obs` = rotel, mlflow, driver; `obs-ui` = caddy, mlflow, gate,
    driver.
  - The gate implements the Origin rules.
  - Caddy strips `Cookie` and `Authorization` going up, and `Set-Cookie` and `Access-Control-*` coming down.
  - An echo upstream (`gate.py` on :9001) sits behind the same proxy settings.
  - Three Caddy sites differ only in CSP: :8443 strict base, :8445 none, :8446 candidate.

**Gate and edge** (owner cookie unless noted; the dashboard origin is `http://localhost:8443` in the spike):

| Request | Status |
| --- | --- |
| POST search, `Origin` = dashboard | 200 |
| POST search, no `Origin` | 403 |
| POST search, `Origin: http://localhost:3000` | 403 |
| POST search, `Origin: https://localhost` (the app) | 403 |
| GET search, `Origin: http://localhost:3000` | 403 |
| GET search, same origin | 200, no `Access-Control-Allow-Origin` |
| **Direct to MLflow** (no edge), GET, `Origin: http://localhost:3000` | **200 with `Access-Control-Allow-Origin: http://localhost:3000`**: MLflow trusts every localhost origin with credentials; this is the threat the gate's Origin rule closes |
| POST `text/plain` to experiments search (edge, and direct) | 400 |
| POST `text/plain` to delete-traces (direct) | 400 |
| Echo upstream, owner cookie plus `Authorization` | the upstream saw `accept-encoding, host, origin, user-agent, via, x-forwarded-for, x-forwarded-host, x-forwarded-proto`: no cookie, no authorization. No `Set-Cookie` and no ACAO reached the client, though the echo sent both |
| `/v1/traces`, `/gateway/x`, `/ajax-api/3.0/mlflow/assistant/x`, `/mcp`, `/api/2.0/mlflow/gateway/x` | 404 from Caddy itself (7-byte body `blocked`) |

**Finding: a blocked-path bug in round 1.**
- **Symptom:** in round 1 and the first round-2 run, `respond @blocked 404` sat next to a catch-all `handle`, and
  Caddy runs `handle` before `respond`. So `/v1/traces` reached MLflow (422), and the other "404"s were MLflow's own
  (207 bytes).
- **Fix:** a `handle @blocked { respond … 404 }` block.
- **Test:** the paired `caddy adapt` test must assert the handler order, not just the presence of a `respond`.

**Retention with a real cutoff:**
- **Setup:** a trace with a 40-day-old timestamp and a fresh one, both ingested through Rotel.
- **First call:** REST `delete-traces` with the 30-day cutoff and `max_traces: 1` deleted 1 trace in 0.01 s.
- **Second call:** without `max_traces` it deleted 0 traces in 0.002 s.
- **After:** the old trace is gone and the fresh one stays. MLflow's handler sets no server-side cap on
  `max_traces`; the SQL query applies `LIMIT max_traces` only when it is given.

**Browser pass** (headless Chromium 1.65.1 in the caddy container's network namespace; owner cookie as an extra
header):

| Site | CSP violations (UI, traces list, two trace details) | Requests to another host | Renders the trace detail |
| --- | --- | --- | --- |
| :8443 strict `default-src 'self'; connect-src 'self'; …` | inline styles only (`style-src-elem 'inline'`), on every page | none | yes, but the Links tab text did not render |
| :8445 no CSP | — | none | yes |
| :8446 candidate `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; worker-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'` | **none** | none | yes, identical to no CSP |

- **CSP:** the UI needs no script relaxation, no `'unsafe-eval'` and no other host. The candidate is within the
  ceiling SRV-OBS FR-OBS-9 sets.
- **Deep link:** `#/experiments/0/traces?selectedEvaluationId=tr-<trace id>` opens the trace's detail with its span
  tree (`job tutor.answer` → `agent.call` → `claude_code.interaction` → `claude_code.llm_request`).
  `selectedTraceId` opens only the list.
- **Span links:** shown as a badge on the span and a "Links 1" tab (`shots/candidate-deep_selectedEvaluationId.png`).
- **The UI's own failing calls**, the same with and without CSP:
  - `404 /ajax-api/2.0/mlflow/users/current` (MLflow has no auth app);
  - `404 /ajax-api/3.0/mlflow/assistant/config` and `/providers` (blocked at the edge). The UI still renders.

## Limits

- **Memory sampling:** `docker stats` only, at 4–5 s intervals, with page cache included.
- **Browser:** headless Chromium only, plain HTTP, on four pages. Interactions such as editing assessments or
  deleting from the UI were not exercised under the candidate CSP.
- **TLS:** Caddy served plain HTTP here; `tls internal` on 8443 is the same mechanism as the main site (ADR-0024).
- **Volume permissions:** the volume was fresh each run. Ownership through the image directory is the OPS-DEPLOY
  blobs pattern.

## Spend

$0. No model calls.
