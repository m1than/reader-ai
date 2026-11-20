---
id: ADR-0006
title: Backend — async Python, event log with transactional outbox, Postgres job queue, SSE streams
status: proposed
date: 2026-11-20
verified_by: "SRV-EVENTS tests: event and state change commit atomically; SSE resume by Last-Event-ID loses no events; a killed worker's job is retried exactly once more"
revisit_when: "Sustained job throughput above 50 jobs/s, or SSE fan-out above 500 concurrent streams"
---

## Context

Owner decision, 2026-11-20: Python with proper skills, async, an event-driven architecture and streams; runs in containers; serves any UI as a self-hosted service.

Load profile (hypothesis, single owner plus a few partners): one course compile is 23 lessons × 6 stages = 138 jobs; a study day is tens of tutor turns and kernel executions. Peak job rate stays below 1 job/s.

## Decision

| Concern | Choice |
| --- | --- |
| Runtime | Python 3.14, `uv` for dependencies and lockfile (amended by ADR-0020; was 3.13) |
| HTTP | FastAPI on Uvicorn (ASGI), Pydantic v2 models as the API contract; OpenAPI → generated TypeScript client for `web/` |
| Streams to the browser | Server-Sent Events for events and token streams (resumable with `Last-Event-ID`); WebSocket only for the kernel relay, which is bidirectional by protocol |
| Database access | SQLAlchemy 2 async with asyncpg; Alembic migrations |
| Events | `events` table written in the same transaction as the state change (transactional outbox); typed event registry is the single contract |
| Jobs | Postgres queue (`FOR UPDATE SKIP LOCKED`, `LISTEN/NOTIFY` wake-up) through `procrastinate`; idempotency key `(lesson, stage, input_sha256)` |
| Structure | One package `reader`, three entrypoints: `api`, `worker`, `mcp` |
| Quality gates | ruff, mypy `--strict`, pytest with anyio, Hypothesis for invariants, testcontainers Postgres for integration |
| Observability | OpenTelemetry traces and metrics (OTLP), structlog JSON logs with secret redaction, trace id in every event |

## Options considered for the queue and event bus

| Option | Gives | Costs |
| --- | --- | --- |
| **Postgres (outbox + SKIP LOCKED + LISTEN/NOTIFY)** | State, events and jobs commit in one transaction; one stateful service to back up | Throughput ceiling far above the load profile, below dedicated brokers |
| Redis Streams / arq | Fast, familiar | Second stateful store: enqueue and state change are no longer atomic, so a crash between them loses or duplicates a job |
| NATS JetStream / Kafka | Real streaming platform | Two extra services and their volumes for < 1 job/s |

## Options considered for the HTTP framework

| Option | Gives | Costs |
| --- | --- | --- |
| **FastAPI** | Largest ecosystem, Pydantic v2 native, OpenAPI out of the box | Some magic in dependency injection |
| Litestar | Faster in benchmarks, msgspec | Smaller ecosystem; speed is irrelevant at < 1 req/s of heavy work |
| Starlette only | Minimal | Hand-written validation and OpenAPI |

## Rejected options — why (numbers)

- **Redis or NATS.** Adds 1–2 stateful services (and their volumes and backups) to remove a bottleneck that is at least two orders of magnitude away (< 1 job/s now, revisit at 50 jobs/s). The atomic outbox is lost: 2 stores to reconcile instead of 1.
- **WebSocket for all streams.** SSE resumes from `Last-Event-ID` with plain HTTP through Caddy; a WebSocket stream needs its own replay protocol for the same guarantee.

## Consequences

- Every feature is "write state + append event" in one transaction; projections (progress views, sector indexes) are rebuilt from events if needed.
- Long agent stages run in the worker, never in a request; the api only enqueues and streams progress.
