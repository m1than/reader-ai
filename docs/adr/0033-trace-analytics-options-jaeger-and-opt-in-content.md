---
id: ADR-0033
title: Trace analytics options — a Jaeger waterfall beside MLflow, and an opt-in content mode (amends ADR-0032)
status: proposed
date: 2026-11-24
verified_by: "spikes/obs-stack round 4 (results/20261124T103924-round4-jaeger, results/20261124T105821-round4-tabs); if accepted, SRV-OBS's [config], [image] and [live] tests of the chosen options"
revisit_when: "MLflow's execution timeline gets a full-width or zoomable view; or Jaeger's UI needs 'unsafe-eval' or 'unsafe-inline' to render; or a second user needs the dashboards"
supersedes: []
---

## Context

The owner asked on 2026-11-24 for "more detailed info in mlflow dashboards … use all tabs for tracing, all with best
practices" and "is there a Jaeger-like view of spans?". SRV-OBS's proposed V.obs-analytics amendment fills MLflow
3.18.0's tabs with ids, enums and numbers only (spans types, token usage, previews, tags, sessions, code-sourced
assessments, prompt templates). Two things stay outside ADR-0032 and need the owner's decision:

1. **A waterfall view.** MLflow's "Show execution timeline" draws real nested bars with durations, but in a pane of
   about 300 px: a 45-span compile trace is cramped and its names truncate (spikes/obs-stack round 4,
   `trace-timeline.png`).
2. **Text in traces.** Several MLflow tabs are most useful with prompts and answers (the trace detail's chat view,
   Review/labeling, Datasets, LLM judges). ADR-0032 and SRV-OBS INV-OBS-1/-2 keep every text out of traces.

## Decision (proposed; the owner decides each option separately)

### Option A — Jaeger all-in-one as a second view (off unless chosen)

- Jaeger v2.23.0 all-in-one (Apache-2.0), `jaegertracing/jaeger:2.23.0@sha256:3823829974ccfc2485d2e48b165579091f32295022ae80ca49704e2f4834116e`,
  in the `observability` profile, on the `obs` network only (Rotel in, Caddy out through `obs-ui`).
- Rotel exports to both: `--exporters=mlflow:otlp,jaeger:otlp`, `--exporters-traces=mlflow,jaeger`, settings in
  `ROTEL_EXPORTER_<NAME>_<FIELD>` (measured; the `ROTEL_OTLP_EXPORTER_<NAME>_*` form is not read). The same allow-listed
  spans reach both; nothing new is exported.
- Storage: in memory with `max_traces` (measured: 12 MiB idle, 35.5 MiB with 13,500 spans; limit 256m). Traces
  are lost on restart; that is acceptable for a debugging view, since MLflow stays the store. Badger on a volume
  with a TTL equal to `retention_days` is the alternative if the owner wants them to survive restarts.
- Access: a third Caddy site `localhost:8444`, published by `reader up` on 127.0.0.1 only while the option is on,
  behind the same `forward_auth` to `/api/obs/gate`. The gate's Origin rules accept `https://localhost:8444` as a
  second dashboard origin (Jaeger's UI loads ES modules, so its requests carry `Origin`). The UI's two inline
  scripts are allowed by hash, `base-uri 'self'` (its `<base href>`), everything else as FR-OBS-9's CSP. One
  `script-src eval` violation was reported and the page still rendered; `'unsafe-eval'` is not granted.
- Egress: none. Jaeger's own telemetry is off (`service.telemetry.metrics.level: none`).
- Links: `/observability` links each trace to both views.

### Option B — opt-in content mode (off by default)

- A setting `observability.content: off | redacted` (default `off`). `off` is today's rule: no text in any span.
- `redacted` would let three kinds of text into traces, for the owner's own debugging, with this redaction policy:
  - the learner's question and the tutor's answer, truncated to 2,000 characters each, as root
    `mlflow.spanInputs`/`mlflow.spanOutputs`;
  - the CLI's prompts (`OTEL_LOG_USER_PROMPTS=1`) — never book source blocks: the system prompt (the rendered book
    source) stays excluded, which the CLI cannot do selectively, so this part needs a CLI-side filter or stays off;
  - never secrets, handles, tokens, notebook outputs, or another user's text: with open sign-up (ADR-0031) content
    mode is refused when any non-owner user exists.
- It would turn on MLflow's chat view, Review queues and text Datasets. It is not designed further here: it
  changes INV-OBS-1/-2 and the copyright rule ("copyrighted source material is never committed" extends to traces
  only by policy), so the owner decides first.

## Consequences

- Without either option, MLflow's tabs still carry types, tokens, cost, previews of ids, tags, sessions,
  assessments and prompt templates (SRV-OBS V.obs-analytics), and the timeline stays MLflow's narrow one.
- Option A adds one container (≤ 256 MiB), one loopback port and one gate origin; its retention is by count.
- Option B weakens INV-OBS-1 by design and needs its own review round before any build.
