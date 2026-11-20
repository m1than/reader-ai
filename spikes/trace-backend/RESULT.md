# Spike: trace-backend — RESULT

**Question (docs/roadmap.md 0.3; ADR-0018):** MLflow or Phoenix as the trace backend on the Mac, fed through Rotel with our parent spans and the Agent SDK's native traces joined by `TRACEPARENT`.
Measures: memory at idle and under one lesson compile; time to find why a seeded failing stage failed; whether quality scores attach to the trace.
**Pass criterion:** agent spans nest under our stage span; a score is visible on the trace.
**Verdict: PASS for both backends.** Recommendation: **Phoenix** for v1 on the Mac (see "Recommendation"). Switching backends changed only Rotel's three exporter variables, so the fallback costs one Compose change, as ADR-0018 assumed.

Date: 2026-11-20. Command: `bash spikes/trace-backend/run.sh` (both backends in sequence). Raw data: `results/20261120T201622/{phoenix,mlflow}.json`.

## Setup

| Part | Version | Role |
| --- | --- | --- |
| Probe (ours) | `python:3.14-slim-trixie`, `claude-agent-sdk` 0.2.186, `opentelemetry-sdk` and `-exporter-otlp-proto-http` 1.45.2 | Opens `reader.job` → `reader.stage <name>` spans and runs one Agent SDK query inside each stage. Exports OTLP/HTTP to Rotel |
| Agent SDK → CLI | bundled Claude Code CLI | The SDK injects `TRACEPARENT`/`TRACESTATE` from the active OTel span into the CLI's environment (`claude_agent_sdk/_internal/transport/subprocess_cli.py`, the block commented "Propagate active OTEL trace context to the CLI"). The CLI exports its beta traces with `CLAUDE_CODE_ENABLE_TELEMETRY=1`, `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`, `OTEL_TRACES_EXPORTER=otlp`; metrics and logs off |
| Collector | Rotel `streamfold/rotel:v0.2.7` | OTLP in (gRPC 4317, HTTP 4318); one OTLP exporter configured by `ROTEL_OTLP_EXPORTER_ENDPOINT`, `_PROTOCOL`, `_CUSTOM_HEADERS` |
| Backend A | Phoenix `arizephoenix/phoenix:version-22.0.0` | Rotel → `phoenix:4317` (gRPC). Auth disabled for this local spike (see findings) |
| Backend B | MLflow `ghcr.io/mlflow/mlflow:v3.18.0` (`mlflow server`, SQLite in the container) | Rotel → `http://mlflow:5000` (HTTP, `/v1/traces`) with header `x-mlflow-experiment-id=0` |

**Load:** one probe run of 3 stages on `claude-haiku-5-5`: `draft`, `fact_check` (seeded failure: model `claude-nonexistent-0-0`) and `review`, each a one-turn query without tools. This is far smaller than a real lesson compile; memory "under load" below is for this load only.

## Results

| Measure | Phoenix | MLflow |
| --- | --- | --- |
| Agent spans nest under our stage span | **yes**: 3 of 3 `claude_code.interaction` spans have a `reader.stage` parent; 4 `claude_code.llm_request` under interactions | **yes**: 3 of 3; 5 `llm_request` under interactions |
| Score visible on the trace | **yes**: `POST /v1/span_annotations` → `GET …/span_annotations?span_ids=` returns `reader.quality` 0.8 on the `review` stage span | **yes**: `mlflow.log_feedback(…, span_id=…)` → `get_trace().info.assessments` returns `reader.quality` 0.8 on the `draft` stage span |
| Seeded failure found | `reader.stage fact_check` (ERROR, SDK `ResultError`) → child `claude_code.llm_request` (ERROR, `model: claude-nonexistent-0-0`) | the same two error spans, same message |
| Time to find the cause via API | 0.07 s (one REST call for the project's spans, filter by trace, walk from the deepest error span to its stage) | 2.17 s (same procedure through the MLflow Python client in the server container; includes Python start) |
| Ready after `compose up` (cold) | 24 s | 8 s |
| Memory, idle (backend / Rotel) | 832 MiB / 15 MiB | 2,445 MiB / 8 MiB |
| Memory, peak during the probe run (sampled each 1 s) | 833 MiB / 17 MiB | 2,454 MiB / 28 MiB |
| Image size | 1,433 MB (Rotel 345 MB) | 1,369 MB |
| Spans in the trace | 11 (the probe's `review` call made one `llm_request`) | 12 |

Span tree, identical in both (ids in the results files under `query.nesting.edges`):

```
reader.job lesson-compile-spike
├── reader.stage draft        → claude_code.interaction → claude_code.llm_request ×1–2
├── reader.stage fact_check   (ERROR) → claude_code.interaction → claude_code.llm_request (ERROR: model: claude-nonexistent-0-0)
└── reader.stage review       → claude_code.interaction → claude_code.llm_request ×1–2
```

**"Minutes to find the failure" is not measured for a human in a UI.** No browser was used. The figures above are an API procedure, the same one an agent or a CLI script would use. In both backends the cause is two hops from the stage span, and it names the failing stage directly. An earlier exploratory Phoenix run measured 487 MiB idle (not kept); the kept run measured 832 MiB, so Phoenix idle memory varies by about 350 MiB between starts.

## Findings

1. **`TRACEPARENT` works without glue code.** Opening our span and calling `query()` inside it is enough. The SDK reads the active OTel context and passes it to the CLI. No `ClaudeAgentOptions.env` handling was needed.
2. **The CLI's error lands on its own span.** The `llm_request` span carries status ERROR and the reason (`model: …`), separate from our stage span's exception. Triage reads the CLI span; our span only records that the stage failed.
3. **Phoenix 22 enables auth by default in Docker** (since v21, the image binds `::`). The spike set `PHOENIX_ENABLE_AUTH=false`. A deployment needs an API key, which Rotel can send with `ROTEL_OTLP_EXPORTER_CUSTOM_HEADERS`, and the key becomes a secret (ADR-0011).
4. **MLflow needs the experiment id as a header.** Without `x-mlflow-experiment-id`, OTLP ingest has no target. MLflow's trace id is `tr-` plus the OTel trace id, so our ids map directly.
5. **MLflow's footprint is about 3× Phoenix's at idle** (2.4 GiB against 0.5–0.8 GiB) in this configuration, with SQLite in the container. ADR-0018 plans MLflow on our Postgres, which was not measured.
6. **Scores:** Phoenix takes annotations over plain REST. MLflow was exercised through its Python client; its REST API for assessments exists but was not tested here.
7. **Content capture stayed off** (`OTEL_LOG_*` unset). The spans hold names, models, tokens and status, but no prompt text.

## Recommendation (for S0.exit; the decision is the owner's)

**Phoenix** for v1 on the Mac:
- 3× lower memory at idle, on a 36 GiB machine that also runs the kernel sandbox, Docling and the stack;
- annotations and spans over plain REST from any process, without an extra client library;
- an equal result on nesting, scores and triage.

Costs to accept:
- **License:** Elastic License 2.0. ADR-0018 already records this: Phoenix stays owner-only and is not offered to third parties as a hosted service.
- **Auth:** an API key has to be managed (finding 3).

MLflow (Apache-2.0) stays the fallback, and the v2 choice if its evaluation and registry features are needed. The switch is Rotel's three exporter variables.

## Spend

| Run | USD |
| --- | --- |
| Exploratory Phoenix run (kept stack, not in `results/`) | 0.003082 |
| Exploratory MLflow run (not in `results/`) | 0.000587 |
| Measured run, both backends (`results/20261120T201622/spent.txt`) | 0.001073 |
| **Total** | **0.004742** (cap $2.00, enforced per probe run and per spike in `run.sh`) |

Token: Keychain item `reader-ai/claude-oauth-token` → `docker run -e`, read only inside `run.sh`. The owner allowed this for trace-backend on 2026-11-20. While a probe runs, the token is visible to `docker inspect`, as in agent-sdk-container. All containers and volumes were removed after the run (`docker compose down -v`).
