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

## Limits

- **Memory sampling:** `docker stats` only, at 4–5 s intervals, with page cache included.
- **Browser:** none was used. The UI's CSP needs and its deep-link parameter are unverified (SRV-OBS FR-OBS-9 and
  FR-OBS-10 check them at Green).
- **TLS:** Caddy served plain HTTP here; `tls internal` on 8443 is the same mechanism as the main site (ADR-0024).
- **Volume permissions:** the volume was fresh each run. Ownership through the image directory is the OPS-DEPLOY
  blobs pattern.

## Spend

$0. No model calls.
