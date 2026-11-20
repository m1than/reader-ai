# Research: tracing agent systems (2026-11-20)

Question from the owner: are there ready tools for tracing agent systems properly, or should reader-ai extend OpenTelemetry itself? This feeds ADR-0018.

## What reader-ai needs from tracing

| Need | Why | Must have |
| --- | --- | --- |
| One trace per pipeline stage or tutor turn, with the agent's model calls, tool calls and subagents nested inside | "Where is it good and where is it bad" is a per-stage question | Context propagation from our spans into the agent's spans |
| Tokens, cost, latency per span | Budgets and the ledger (ADR-0005, ADR-0017) | Numeric attributes |
| Quality signals attached to runs | Fact-check findings, lab failures, G2 returns, tutor feedback (ADR-0018) | Scores or events linked to a trace |
| Content capture off by default | `personal-use` material must not leave the instance (ADR-0015) | Opt-in content |
| Runs on the owner's Mac next to the reader-ai stack | v1 is Mac-only (ADR-0012) | Small footprint, arm64 |
| License compatible with a product served to paying users in v2 | ADR-0017 | Permissive, or used only internally |
| Datasets and model lifecycle for tuning local models | v2 (ADR-0018) | Export of labelled runs; ideally a model registry |

## Finding 1: the Agent SDK already emits OpenTelemetry

Source: code.claude.com, Agent SDK → Observability with OpenTelemetry (read 2026-11-20).

- The Agent SDK runs the Claude Code CLI as a child process; the CLI has built-in OpenTelemetry instrumentation and exports directly to an OTLP collector. The SDK itself emits nothing.
- Three signals, each enabled separately:
  - metrics: tokens, cost, sessions;
  - log events: prompt, API request, API error, tool result;
  - traces: beta, enabled with `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` plus `OTEL_TRACES_EXPORTER`.
- Span names:
  - `claude_code.interaction`: one agent-loop turn;
  - `claude_code.llm_request`: model, latency, tokens;
  - `claude_code.tool`, with children `claude_code.tool.blocked_on_user` and `claude_code.tool.execution`;
  - `claude_code.hook`.
  Subagent spans nest under the parent's tool span, so a delegation chain is one trace.
- **Context propagation:** when `query()` is called inside an active OTel span, the SDK injects `TRACEPARENT`/`TRACESTATE` into the CLI. The agent's `interaction` span becomes a child of our span, and log events carry the same `trace_id`/`span_id`.
- **Content is off by default.** Opt-in variables: `OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_TOOL_DETAILS`, `OTEL_LOG_TOOL_CONTENT`, `OTEL_LOG_RAW_API_BODIES`.
- Export failures are silent unless `CLAUDE_CODE_OTEL_DIAG_STDERR=1` is set.
- Trace span names and attributes are beta and may change between releases.

Consequence: reader-ai does not need a third-party instrumentor for the agent's internals. It needs its own parent spans (stage, tutor turn, job) and the right environment for the CLI.

## Finding 2: instrumentation libraries exist as a fallback

| Library | Conventions | Status |
| --- | --- | --- |
| OpenInference `openinference-instrumentation-claude-agent-sdk` (Python and JS) | OpenInference (`openinference.*`) | Used by Langfuse's and Phoenix's Claude Agent SDK guides |
| `opentelemetry-claude-agent-sdk` | OTel GenAI semantic conventions | Alpha |
| `traceai-claude-agent-sdk` | traceAI, Apache-2.0 | Subagent tracing |

They matter only if the CLI's native traces prove insufficient, for example if the beta span names keep changing.

## Finding 3: the conventions are still moving

The OTel GenAI conventions moved out of the core semantic-conventions repository into `semantic-conventions-genai` with v1.42.0 (2026-06-12). As of mid-2026 every `gen_ai.*` attribute was still in Development status, and the new repository had no tagged release. reader-ai keeps its attribute mapping in one module (ADR-0018) so a convention change is one edit.

## Finding 4: backends

| Backend | License | Self-host footprint | Agent traces | Evals and scores | Datasets and models | Fit |
| --- | --- | --- | --- | --- | --- | --- |
| **MLflow Tracing** | Apache-2.0 (Linux Foundation project) | One server plus a backend store (Postgres, MySQL or SQLite) and an artifact store | OTLP ingest; native GenAI semantic conventions for export and ingestion | LLM-judge evaluation, human feedback | Prompt versioning, **model registry**, experiments | Strongest for v2 tuning; reuses our Postgres |
| **Arize Phoenix** | Elastic License 2.0 (source-available): free for internal use, forbids offering it to third parties as a hosted or managed service | One container, SQLite or Postgres; v21.0.0 on 2026-10-31 | OpenInference-first, OTLP on port 6006 (HTTP) and 4317 (gRPC) | LLM-as-judge, score write-back | Datasets, experiments | Lightest; internal-only because of ELv2 |
| **Langfuse** | MIT core, enterprise modules separately licensed; part of ClickHouse since January 2026 | 6 services: web, worker, Postgres, Redis, ClickHouse, S3/MinIO. Production minimums: web and worker 2 CPU / 4 GiB each, Postgres 2 / 4, Redis 1 / 1.5, ClickHouse 2 / 8 | OTel-native SDKs and OTLP endpoint | Scores, LLM-as-judge | Prompt management, datasets | Richest product; heaviest |
| **Opik (Comet)** | Apache-2.0, whole platform | Docker Compose; footprint to be measured | Native OTel support | Built-in LLM-judge metrics library | Prompt optimization | Strong license; agent tracing rated moderate by MLflow's comparison |
| Laminar, OpenLIT | Apache-2.0 | — | Yes | Partial | — | Smaller ecosystems |
| Jaeger / Grafana Tempo | Apache-2.0 / AGPL-3.0 | Light | Generic spans | None | None | No LLM-specific views or scores |

## Finding 5: what "our own OTel" means in practice

A full trace backend (storage, query, UI, scoring, datasets) is the whole product of every row above. Writing one would be a large v2 epic that duplicates mature projects. What is genuinely ours is small:

1. **Parent spans** for pipeline stages, jobs and tutor turns, with reader-ai attributes (`reader.course`, `reader.lesson`, `reader.stage`, `reader.transformation_kind`, `reader.funding`) next to `gen_ai.*`.
2. **CLI environment per run**: exporter endpoint, `OTEL_RESOURCE_ATTRIBUTES` with `enduser.id` and course/lesson, and content-capture flags off for `personal-use` material.
3. **A span processor** that writes the run summary into `agent_runs`, our data of record for the ledger, export and datasets, so reader-ai never depends on a tracing vendor for billing or tuning data.
4. **Quality signals** written both as our rows and as OTel events or scores on the trace, so any backend shows them.
5. **An OTel Collector** in Compose as the router: our spans and the CLI's spans go to the backend of choice. A backend change is collector config.

## Options

| Option | v1 effort | v2 effort | Risk |
| --- | --- | --- | --- |
| **A. Standard instrumentation + own run records + MLflow as backend** | Parent spans, processor, collector; MLflow server on our Postgres | Evals and model registry already there for tuning | MLflow's trace UI is less agent-specialised than Phoenix or Langfuse |
| **B. Same + Phoenix as backend** | Same, plus one Phoenix container | Datasets and experiments are there; model registry is not | ELv2: Phoenix stays an owner-only tool and is never exposed to users |
| **C. Same + Langfuse as backend** | Same, plus 6 services | Rich evals and prompt management | Production minimums add 9 CPU and 21.5 GiB of RAM next to reader-ai on a Mac |
| **D. Our own backend on OTel** | Spans, processor, collector, plus our own storage, UI and scoring | Everything else, ourselves | Months of work that duplicates A–C |

## Recommendation

**A, with B as the challenger in a spike.**

- Instrument with standard OTel: our parent spans plus the Agent SDK's native traces, joined by `TRACEPARENT`.
- Keep `agent_runs` and quality signals in Postgres as the data of record.
- Route everything through an OTel Collector, so the backend stays replaceable.
- Pick the backend with a one-day spike: MLflow vs Phoenix on the owner's Mac, measured on one compiled lesson. Compare memory, time to find "why did this stage fail", and how scores attach.

Why not C: its minimum footprint (9 CPU, 21.5 GiB) is larger than the whole reader-ai stack planned for the Mac.

Why not D: a trace backend is a product of its own. What reader-ai must own is the data of record and the attributes, and A keeps both.

## Sources

- [Agent SDK: Observability with OpenTelemetry](https://code.claude.com/docs/ja/agent-sdk/observability)
- [Langfuse: Claude Agent SDK integration](https://langfuse.com/integrations/frameworks/claude-agent-sdk)
- [OpenInference Claude Agent SDK instrumentation](https://pypi.org/project/openinference-instrumentation-claude-agent-sdk)
- [opentelemetry-claude-agent-sdk](https://pypi.org/project/opentelemetry-claude-agent-sdk/)
- [Langfuse production checklist](https://langfuse.com/resources/engineering/self-hosted-langfuse-production-checklist)
- [Arize Phoenix license](https://arize.com/docs/phoenix/self-hosting/license)
- [MLflow Tracing](https://mlflow.org/docs/latest/genai/tracing/index.html)
- [Opik README](https://raw.githubusercontent.com/comet-ml/opik/main/README.md)
- [OTel GenAI conventions status](https://dev.to/azena-ai/opentelemetrys-genai-semantic-conventions-are-not-stable-yet-heres-what-actually-shipped-in-2026-3mke)
