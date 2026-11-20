---
id: ADR-0018
title: Agent bus, run traces and quality signals in v1; evaluation, local models and tuning in v2
status: proposed
date: 2026-11-20
verified_by: "SRV-AGENT tests: every agent run produces one run record and an OTel span tree with gen_ai.* attributes; quality signals attach to runs; content capture is off for personal-use material; pipeline stages advance only through events"
revisit_when: "The OpenTelemetry GenAI conventions publish a stable release, or v2 work starts"
---

## Context

Owner decisions, 2026-11-20:

- Everything runs on an agent bus, so that analysing material and building a course is itself an observable pipeline.
- Collect traces of this process and metrics, so it is clear where the pipeline is really good and where it is bad.
- Use local models and tune them.
- Lay the groundwork now; put the rest into the v2 backlog.
- Local models in v2 run on the owner's ASUS Ascent GX10 (training and heavy inference) and on the Mac (light models: embeddings, classification).

Why the groundwork cannot wait: traces and quality labels for runs that already happened cannot be reconstructed. Every lesson compiled and every tutor session in v1 without them is lost training and evaluation data.

External facts:

- OpenTelemetry's GenAI semantic conventions define spans, metrics and events for model calls, tool execution, retrieval, memory and whole agent runs. In v1.42.0 (12 June 2026) they moved to a dedicated repository, `semantic-conventions-genai`. As of mid-July 2026, every `gen_ai.*` attribute, span, metric and event was still in Development status.
- Anthropic's Consumer Terms prohibit using the services to develop competing products, including to develop or train AI or machine-learning models. The owner's Claude subscription runs under the Consumer Terms.

## Decision for v1 (groundwork)

| Piece | What is built | Where |
| --- | --- | --- |
| **The bus** | The event log (ADR-0006) is the bus. Pipeline stages are consumers: `stage.completed` enqueues the next stage, so no stage calls another directly. Every agent invocation is a job with an input event and an output event | SRV-EVENTS, SRV-PIPELINE |
| **Run records** | One `agent_runs` row per run: parent run, task class, prompt file hash, model, provider, input references (hashes and span ids, not raw text), output reference, tokens, cost, latency, status | SRV-AGENT |
| **Traces** | reader-ai opens a parent span per stage, job and tutor turn, with `gen_ai.*` and `reader.*` attributes kept in one mapping module so a convention change is one edit. The Agent SDK's own OpenTelemetry export (traces beta, metrics, log events) nests the agent's model, tool and subagent spans under that parent through `TRACEPARENT`. Rotel, an OpenTelemetry collector written in Rust (Apache-2.0), runs in Compose. It buffers all spans and routes them to the trace backend. Content capture is opt-in and always off for `personal-use` material. Details and alternatives: [docs/research/agent-tracing-2026-11.md](../research/agent-tracing-2026-11.md) | SRV-AGENT, SRV-OBS |
| **Quality signals** | Evaluation events attached to runs: fact-check findings per lesson, lab failures, blocking review findings, G2 return reasons (owner labels from a fixed list), tutor answer feedback (useful / wrong / unclear), recall accuracy on cards the tutor explained, learner edits of generated cells | SRV-PIPELINE, SRV-TUTOR, SRV-RECALL |
| **Stage metrics** | Per stage and task class: pass rate on first attempt, loops back to draft, cost and latency percentiles, owner return rate at G2 | SRV-OBS |
| **Model per task as data** | Task → provider + model in `reader.yaml` (ADR-0005), so a local model is a config change once its adapter exists | SRV-AGENT |
| **Consent flags** | Per user: may their runs be used for evaluation; may they be used for tuning. Default: evaluation yes for the owner, everything no for others | SRV-AUTH |

## Decision for v2 (backlog)

| Epic | Track | Scope |
| --- | --- | --- |
| Agent bus as a platform | engineering | Agents as independent subscribers with typed contracts per topic; replay of a topic into a new agent version; dead-letter handling; per-agent concurrency and budgets |
| Custom agent pipeline with per-agent control | ai | Every agent in the pipeline is a declared unit with its own model and provider, prompt version, tools, budget, enable/disable switch and version history, controlled one by one from the UI. The Claude Agent SDK is the first integration behind it, not the only one (owner, 2026-11-20) |
| Trace dashboards | engineering | Stage-quality dashboards on the trace backend chosen in v1, built from the v1 metrics |
| Evaluation harness | ai | Offline eval sets built from labelled v1 runs; regression gate on any prompt or model change; side-by-side comparison per task class |
| Local inference | ai | `openai-compatible` provider adapter; GX10 serves an OpenAI-compatible endpoint (vLLM, SGLang or llama.cpp, chosen by a spike); MLX on the Mac for embeddings and classification |
| Routing | ai | Task → model chosen by measured quality and cost from the eval harness, not by default |
| Tuning | ai | LoRA/QLoRA on the GX10 from curated datasets; every tuned model must beat its baseline on the eval set before promotion |
| Agent runtime beyond the Agent SDK | ai | A tool-using agent loop for local models (the Agent SDK serves Claude models only) |

## Training data rule

Tuning datasets are built from:

- human-authored text: learner notes, owner labels, corrections;
- source spans of material the user owns;
- outputs of local or open-weight models.

They exclude outputs produced by Claude (confirmed by the owner, 2026-11-20). The Consumer Terms forbid using the service to train models, and the Commercial Terms used for managed credits carry their own use restrictions. Claude outputs may still be used for evaluation: for example, as one side of a comparison judged by the owner.

## Tracing tools (research, 2026-11-20)

The owner asked whether ready tools exist or whether reader-ai should extend OpenTelemetry itself. The answer is both, split along a clear line ([research note](../research/agent-tracing-2026-11.md)).

- **Ready, and used as is:**
  - the Agent SDK's native OTel export;
  - the OTel SDK;
  - Rotel as the collector;
  - a trace backend.
- **Ours, and small:**
  - parent spans and `reader.*` attributes;
  - the per-run CLI environment;
  - a span processor that writes `agent_runs`;
  - quality signals as both rows and trace events.

Backend options for the owner's Mac:
- **MLflow** (Apache-2.0): one server on our Postgres, with GenAI conventions, evaluation and a model registry for v2 tuning.
- **Arize Phoenix** (Elastic License 2.0): one container. It stays owner-only, because the license forbids offering it to third parties as a hosted service.
- **Langfuse** (MIT core) is rejected for v1: 6 services with production minimums of 9 CPU and 21.5 GiB next to reader-ai.
- **Writing our own backend** is rejected: it duplicates whole products.

The trace backend moves from v2 into v1 as an optional Compose profile, chosen by a one-day spike (MLflow vs Phoenix) in stage 0 (owner, 2026-11-20).

## The performance path in Rust (owner, 2026-11-20)

The telemetry buffering and routing path is Rust: Rotel replaces the Go OpenTelemetry Collector.

| Number | Value | Source |
| --- | --- | --- |
| Python OTel SDK throughput, in process, 12 attributes per span, 2 vCPU | 36,919 spans/s | Measured 2026-11-20 (200,000 spans, BatchSpanProcessor) |
| reader-ai v1 telemetry load (hypothesis) | ≈ 3 spans/s even when a whole 23-lesson book compiles in one hour (≈ 11,000 spans) | 23 lessons × 7 stages × ≈ 70 spans |
| Rotel vs OTel Collector, Kafka → ClickHouse benchmark | 462,000 vs 137,000 spans/s per core | ClickHouse/Streamfold benchmark |

At v1 load neither choice is the bottleneck: the headroom is about 10,000× in Python alone. Rust is chosen for what it gives later:
- the v2 SaaS (ADR-0017) multiplies the load by the number of users;
- Rotel takes custom processors written in Rust (and Python) for redaction, sampling and enrichment.

Own Rust code starts as the first such processor, with its own spec. Tooling then adds the Rust layer patterns (`telemetry/**`, tests in `telemetry/tests/`).

Two rules hold whatever carries the telemetry:
1. **The ledger and `agent_runs` never come from telemetry.** They are written by the worker in the job's own database transaction, from the Agent SDK's result message. Telemetry is lossy by design: the CLI drops spans silently when an export fails.
2. **The collector is replaceable.** Rotel and the Go OTel Collector both speak OTLP, so falling back is a Compose change. Rotel's Python embedding is still a pre-release, so reader-ai runs Rotel as a standalone container, not embedded.

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **Groundwork in v1, platform in v2** | Data accumulates from the first lesson; v1 scope grows by roughly one table, one event type and a span mapping | Some v1 effort on things that show value only in v2 |
| Everything in v1 | Full loop sooner | Adds 7 epics to a 27-spec v1; delays the daily study loop by months |
| Nothing until v2 | Smallest v1 | v2 starts with 0 labelled runs; every v1 lesson and session is lost as evaluation data |

## Rejected options — why (numbers)

- **Nothing until v2.** The pilot alone compiles 23 lessons through 7 stages, about 160 stage runs, plus months of daily tutor sessions. Without groundwork, all of them reach v2 unlabelled.
- **Everything in v1.** 7 more epics on top of 27 specs, ahead of the stage that gets the owner studying daily (stage 6).

## Open questions (v2)

1. Trace backend: MLflow or Phoenix, decided by the stage 0 spike `trace-backend` on the owner's Mac.
2. Serving stack on the GX10 (vLLM, SGLang, llama.cpp) and the first task to move to a local model; the likeliest candidates are classification of findings into sectors and recall-card generation.
