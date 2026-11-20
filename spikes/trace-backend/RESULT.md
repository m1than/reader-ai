# Spike: trace-backend — RESULT

**Question (docs/roadmap.md 0.3; ADR-0018):** MLflow or Phoenix as the trace backend on the Mac, fed through Rotel with our parent spans and the Agent SDK's native traces joined by `TRACEPARENT`.
**Measures:** memory at idle and under load; time to find why a seeded failing stage failed; whether quality scores attach to the trace.
**Pass criterion:** agent spans nest under our stage span; a score is visible on the trace.

**Verdict: PASS for both backends.**
- **Recommendation: MLflow** in its minimal configuration (`--workers 1`, `MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false`). Memory is comparable, so the recommendation rests on the licence: Apache-2.0 against Phoenix's ELv2, owner-only. Server-side evaluation needs the job runner, which this configuration disables (see "Recommendation").
- **Switching:** for ingest, it changed only Rotel's three exporter variables. Writing scores and querying traces are backend-specific (`query.py`).

Date: 2026-11-20.
- **Command:** `bash spikes/trace-backend/run.sh`.
- **Measured run (round 2):** `results/20261120T205810/{phoenix,mlflow}.json`.
- **Round 1, kept for comparison:** `results/20261120T201622/`, MLflow in its default configuration.

## Setup

| Part | Version | Role |
| --- | --- | --- |
| Probe (ours) | `python:3.14-slim-trixie`, `claude-agent-sdk` 0.2.186, `opentelemetry-sdk` and `-exporter-otlp-proto-http` 1.45.2 | Opens `reader.job` → `reader.stage <name>` spans and runs one Agent SDK query inside each stage. OTLP/HTTP to Rotel |
| Agent SDK → CLI | bundled Claude Code CLI | `claude-agent-sdk` 0.2.186, `_internal/transport/subprocess_cli.py` lines 782–804: if an OTel span is active, the SDK injects `TRACEPARENT`/`TRACESTATE` into the CLI's environment. The CLI exports its beta traces with `CLAUDE_CODE_ENABLE_TELEMETRY=1`, `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`, `OTEL_TRACES_EXPORTER=otlp`; metrics and logs are off |
| Collector | Rotel `streamfold/rotel:v0.2.7` | OTLP in (gRPC 4317, HTTP 4318); one OTLP exporter set by `ROTEL_OTLP_EXPORTER_ENDPOINT`, `_PROTOCOL`, `_CUSTOM_HEADERS` |
| Backend A | Phoenix `arizephoenix/phoenix:version-22.0.0` | Rotel → `phoenix:4317` (gRPC). `PHOENIX_ENABLE_AUTH=false` for this local spike (see findings) |
| Backend B | MLflow `ghcr.io/mlflow/mlflow:v3.18.0`: `mlflow server --workers 1`, `MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false` | Rotel → `http://mlflow:5000` (HTTP, `/v1/traces`) with header `x-mlflow-experiment-id=0` |

Both backends keep SQLite inside their container, with no volume; each `down -v` starts empty. Persistence for v1 (a Phoenix volume, or MLflow on Postgres per ADR-0018) was not measured.

**Host:** the Mac has 36 GiB, but containers run in the Docker Desktop VM with 4 CPUs and 11.62 GiB, which is the budget that matters here. The images were pulled before the run, so "ready" below is container start to API answer, not a cold image pull.

**Load:**
1. **Probe:** 3 stages on `claude-haiku-5-5`: `draft`, `fact_check` (seeded failure: model `claude-nonexistent-0-0`) and `review`, each one turn without tools. 11–12 spans.
2. **Synthetic replay** (`replay.py`, no model calls): one lesson-sized trace of **498 spans**. That is 1 job span plus 7 stages × 10 interactions × (1 interaction + 3 `llm_request` + `tool` with 2 children), with `gen_ai.*` token attributes. It matches ADR-0018's estimate of about 7 stages × ~70 spans per lesson, and stands in for the roadmap's "under one lesson compile".
   - **Limits:** the replay is a burst of zero-duration spans from one process, delivered in about one OTLP batch, with no concurrent queries, and memory sampled at intervals of 1 s or more.
   - **What it shows:** each backend ingests a lesson-sized trace. It does not show memory over a minutes-long compile.

## Results (round 2)

| Measure | Phoenix 22.0.0 | MLflow 3.18.0, minimal |
| --- | --- | --- |
| Agent spans nest under our stage span | **yes**: 3 of 3 `claude_code.interaction` under a `reader.stage`; 4 `llm_request` under interactions | **yes**: 3 of 3; 5 `llm_request` |
| Score visible on the trace | **yes**: `POST /v1/span_annotations` → `GET …/span_annotations?span_ids=` returns `reader.quality` 0.8 | **yes**: `mlflow.log_feedback(…, span_id=…)` → `get_trace().info.assessments` returns `reader.quality` 0.8 |
| Seeded failure | `reader.stage fact_check` (ERROR) → `claude_code.llm_request` (ERROR, `model: claude-nonexistent-0-0`) | the same |
| Triage by API: span fetch + tree walk | fetch 0.025 s (REST, paged), walk < 0.001 s | fetch 0.030 s (`get_trace` timed inside the container), walk < 0.001 s |
| Idle memory over 3 starts | **467–831 MiB** (831, 467, 467) | **372–581 MiB** (581, 372, 372) |
| Idle processes (PIDs) | 22 | 10 |
| Peak during the probe run | 467 MiB, 22 PIDs | 491 MiB, 16 PIDs |
| Peak during the 498-span replay | 470 MiB, 23 PIDs | 441 MiB, 16 PIDs |
| After the replay | 469 MiB, 23 PIDs | 390 MiB, 12 PIDs |
| Spans replayed / ingested | 498 / 498 | 498 / 498 |
| Rotel (both runs) | 8–41 MiB idle, ≤ 10 MiB under load, 7–8 PIDs | same |
| Ready after `compose up` | 15–19 s | 5–6 s |
| Image | 1,433 MB | 1,369 MB |
| Token in `docker inspect` of the probe container | 0 entries | 0 entries |

- **First start:** the first of the three starts used more memory for both backends: 831 against 467 MiB for Phoenix, 581 against 372 MiB for MLflow. Starts 2 and 3 agree, so they are the fairer comparison.
  - **Hypothesis, untested** (the reviewer's): the first start pulls image files into the VM's page cache, and `docker stats` counts that against the container.
- **MLflow default configuration:** round 1, without `--workers 1` and with job execution on, idled at 2,445 MiB (`results/20261120T201622/mlflow.json`). The two settings cut idle memory by about 4–6×.
- **Human triage time not measured.** No browser was used, so how many minutes a person needs in either UI is unknown. The timings above are the API procedure only: fetch the trace's spans, then walk from the deepest error span up to its stage. Both backends are equivalent on it; the cause sits two hops from the stage span and names the failing stage.

Span tree, identical in both backends (ids under `query.nesting.edges` in the result files):

```
reader.job lesson-compile-spike
├── reader.stage draft        → claude_code.interaction → claude_code.llm_request ×1–2
├── reader.stage fact_check   (ERROR) → claude_code.interaction → claude_code.llm_request (ERROR: model: claude-nonexistent-0-0)
└── reader.stage review       → claude_code.interaction → claude_code.llm_request ×1–2
```

## Findings

1. **`TRACEPARENT` works without glue code.** Opening our span and calling `query()` inside it is enough (SDK lines cited above).
2. **The CLI's error lands on its own span** (`llm_request`, status ERROR, `model: …`), separate from our stage span's exception. Triage reads the CLI span.
3. **Phoenix 22 enables auth by default in Docker** (since v21). A deployment needs an API key, which Rotel would send with `ROTEL_OTLP_EXPORTER_CUSTOM_HEADERS`; the key becomes a secret (ADR-0011).
4. **MLflow needs `x-mlflow-experiment-id` on ingest.** Its trace id is `tr-` plus the OTel trace id, so our ids map directly.
5. **MLflow's default server is heavy** (2.4 GiB idle). The minimal configuration is required, and it belongs in the Compose file.
6. **Scores:** Phoenix takes annotations over plain REST. MLflow was driven through its Python client in the server container; its REST API for assessments exists but was not tested.

## Recommendation (for S0.exit; the decision is the owner's)

**MLflow, minimal configuration.** The recommendation rests on the **licence**:
- **Licence:** MLflow is Apache-2.0. Phoenix's Elastic License 2.0 keeps the trace backend owner-only (ADR-0018), which constrains the v2 VPS and sharing plans (ADR-0016, ADR-0017).
- **Memory: comparable** in MLflow's minimal configuration, not lighter overall.
  - Idle, starts 2–3: 372 against 467 MiB.
  - Probe peak: 491 against 467 MiB.
  - Replay peak: 441 against 470 MiB.
  - Processes: 10–16 against 22–23.

  The VM has 11.62 GiB and also runs the kernel sandbox, Docling and the stack. MLflow's default configuration (2.4 GiB) would not be comparable.
- **v2:** MLflow's evaluation and model registry are the ones ADR-0018 plans for tuning. The minimal configuration disables the server job runner, though: with `MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false`, MLflow 3.18.0 refuses jobs (`server/jobs/__init__.py:312`). That turns off server-side scorers, evaluation, discovery and optimize jobs. Using them needs the runner (see below).

**Auth: no advantage either way.** Both ran without auth on 127.0.0.1.
- **MLflow:** it has no auth in this configuration, and `--allowed-hosts "*"` removes its host-header check.
- **Phoenix:** it ships with auth on, and the spike disabled it.

**What stays unmeasured for MLflow**, to check in SRV-OBS before relying on it:
- **Server-side scoring and evaluation:** they need the job runner. With the runner on and `--workers 1`, MLflow idled at 1.61 GiB in the reviewer's round-1 measurement; this spike's measured configuration has it off.
- **Client-side evaluation:** not tested.
- **Auth:** MLflow's auth (its basic-auth app, or a proxy) and a strict `--allowed-hosts`.
- **Postgres:** measured on SQLite inside the container, while ADR-0018 plans our Postgres.
- **Concurrency:** one worker was not tested under concurrent ingest, or over a minutes-long compile.
- **Scores over REST:** scores were written through the Python client only.

Phoenix remains a valid fallback, with an equal result on nesting, scores and triage.

## Spend

| Run | USD | Evidence |
| --- | --- | --- |
| Exploratory Phoenix run | 0.003082 | *Hand-recorded*: printed, result dir not kept. Higher than later runs because its `draft` call wrote the prompt cache ($0.0022 against $0.0001–0.0003 later) |
| Exploratory MLflow run | 0.000587 | *Hand-recorded*, same |
| Round 1, both backends | 0.001073 | `results/20261120T201622/spent.txt` |
| Round 2, both backends | 0.001052 | `results/20261120T205810/spent.txt` |
| Replays | 0 | no model calls |
| **Total** | **0.005794** | cap $2.00 |

**Cap enforcement:** `run.sh` sums every `results/*/spent.txt` plus the hand-recorded exploratory runs before each probe. It counts a probe's spend from per-stage lines, so a killed probe's finished stages still count. `probe.py` checks the remaining cap between stages only: a stage that has started always finishes.

**Token:** the owner allowed Keychain use for this spike in chat on 2026-11-20 ("allow keychain for compile-cost and trace-backend").
- `run.sh` pipes the item `reader-ai/claude-oauth-token` to the probe on stdin.
- The token is exported only inside the container and is never printed.
- It is absent from `docker inspect`: `token_keys_in_docker_inspect` is 0 in both result files.

**Cleanup:** a `trap` removes the probe and replay containers and runs `docker compose down -v` on exit. After the run, 0 containers and 0 volumes were left.
