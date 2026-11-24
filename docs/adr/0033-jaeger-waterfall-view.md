---
id: ADR-0033
title: A Jaeger waterfall view beside MLflow, on its own origin behind the owner gate (amends ADR-0032)
status: accepted
date: 2026-11-24
verified_by: "spikes/obs-stack rounds 4 and 5 (results/20261124T103924-round4-jaeger; results/20261124T113402-round5: gate.json, jaeger-pages.json, stats.txt, stats-f.txt, probe-jaeger-*.json, jaeger-oom*.txt); if accepted, SRV-OBS's [config], [image] and [live] tests listed under Consequences"
revisit_when: "MLflow's execution timeline gets a full-width or zoomable view; or Jaeger's UI needs 'unsafe-eval' or 'unsafe-inline' to render; or a second user needs the dashboards; or the in-memory cap proves too small"
supersedes: []
---

## Context

The owner asked on 2026-11-24 "is there a Jaeger-like view of spans?". MLflow 3.18.0's "Show execution timeline"
draws real nested bars with durations, but in a pane of about 300 px: a 45-span compile trace is cramped and its
names truncate (spikes/obs-stack round 4, `trace-timeline.png`). Jaeger's trace view is a full-width waterfall with a
minimap. This ADR was split from the first ADR-0033 draft (Jaeger plus a content mode) at the V.obs-analytics design
review, round 1 (B9); the content mode is ADR-0034. Round 5 measured the review's B8 points.

## Decision (proposed; off unless the owner accepts it)

- **Image:** Jaeger v2.23.0 all-in-one (Apache-2.0),
  `jaegertracing/jaeger:2.23.0@sha256:3823829974ccfc2485d2e48b165579091f32295022ae80ca49704e2f4834116e` (arm64,
  169 MB), service `jaeger` in profile `observability`, non-root (10006), read-only root, `cap_drop: [ALL]`,
  no-new-privileges, `mem_limit` = `memswap_limit` = 256m, its own telemetry off (`service.telemetry.metrics.level:
  none`).
- **Export:** Rotel exports to both backends: `--exporters=mlflow:otlp,jaeger:otlp`, `--exporters-traces=mlflow,jaeger`,
  settings in `ROTEL_EXPORTER_<NAME>_<FIELD>` (measured; `ROTEL_OTLP_EXPORTER_<NAME>_*` is not read). The same
  allow-listed spans reach both; nothing new is exported.
- **Networks** (corrects the first draft, which said "`obs` only" and also routed Caddy to it): two new internal
  networks with explicit subnets, checked free by `docker network inspect` at build like FR-OBS-8's:
  `jaeger-in` with exactly rotel and jaeger (OTLP 4318), and `jaeger-ui` with exactly caddy and jaeger (UI 16686).
  Jaeger is on neither `obs` nor `obs-ui`, so it shares no network with mlflow or the worker. VEC-DEPLOY-2 gains
  exactly the pairs rotel–jaeger and caddy–jaeger, and the `never` pairs jaeger–{api, worker, mlflow, postgres,
  embed, agent-proxy, mcp, every kernel}. Jaeger initiates no connection. Egress: none.
- **Storage and retention:** in memory, `max_traces` = 450 at `mem_limit` 256m, with `GOMEMLIMIT=200MiB` (the Go
  runtime's soft limit, below the container limit, so the collector collects harder before the kernel kills it).
  Derivation (review round 2, R2-3): memory per span measured with VEC-OBS-2's full CLI key sets at 2.95–3.26 KB
  (round 5 stack F: +24.9 MiB for 8,000 spans, +45.1 MiB for the next 16,000; with minimal attributes 0.85–1.1 KB,
  stack A), taken as 3.3 KB; idle taken as the worst reading, 85 MiB (stack F's 84.7 MiB; stack A read 12.8 MiB;
  the difference is declared, not explained); usable = 256 MiB × 0.75 − 85 MiB ≈ 107 MiB; a trace is sized at 72
  spans, SRV-OBS's bound for one lesson stage (`bounds`; the compile-like spike trace had 45); 107 MiB ÷ (72 ×
  3.3 KB) ≈ 460, rounded down to 450. Retention is by count, not by age: the oldest traces are evicted beyond 450.
  Jaeger restarts empty (an OOM kill or a `reader up` loses every trace it held); a restart loop under sustained
  load was not measured. MLflow stays the store with its 30-day retention.
- **Access:** a third Caddy site `localhost:8444` (`tls internal`), published by `reader up` on 127.0.0.1 only
  while the option is on, behind `forward_auth` to `/api/obs/gate`. The gate learns which site asks from a header
  Caddy sets on the forward_auth request, `header_up X-Reader-Site mlflow|jaeger` (Caddy overrides any value the
  client sent), and accepts as `Origin` only that site's origin: `https://localhost:8443` for `mlflow`,
  `https://localhost:8444` for `jaeger`; a missing or unknown site is 403. Measured (round 5, `gate.json`, 18 cases
  with the owner's cookie on POST): each site passed only with its own origin; the other site's origin and no
  origin were 403; a client-sent `X-Reader-Site` changed nothing. These cross cases join INV-OBS-5. Because a
  request without the header is refused, the main site's `https://localhost/api/obs/gate` route also gets
  `X-Reader-Site: app` (Origin checked against `https://localhost`), so its answer there is unchanged.
- **CSP of 8444:** FR-OBS-9's policy with two changes: `script-src 'self'` plus the UI's two inline-script hashes
  (`sha256-eoBUh2b2awnlKCd0fkQUIXAAxPdxWV4aIas48E/3hLU=`, `sha256-07X4W69Q7ST165IvjVZwZU+G34LWjkh2Xj2s9fMwqYQ=`;
  round 5 recomputed both from the served index), and `base-uri 'self'` (the UI's `<base href>`; with `'none'` its
  modules resolved under `/trace/` and failed). `'unsafe-eval'` is not granted. The one violation on every page
  (ten pages visited in round 5: search, trace, embedded trace, dependencies, deep dependencies, monitor, quality
  metrics, compare and two bad routes) is a single `script-src eval` from `static/schemas-hm98w7NF.js` line 2:
  the bundle's `try{return Function(``),!0}catch{return!1}` capability probe behind a `jitless` switch (it matches
  Zod 4's `allowsEval` check), which falls back to non-JIT code when blocked; every page rendered.
- **Links:** `/observability` links each trace to both views.

## Consequences

- One container (≤ 256 MiB), one loopback port, two small networks, one more gate origin.
- **Rotel and a failing Jaeger** (round 5): with Jaeger stopped, MLflow returned 5 probe traces complete 0.6 s after
  the flush; after Jaeger was OOM-killed under load (24m limit, exit 137, `OOMKilled` true) they arrived in
  16.1–16.3 s, and Rotel logged retries to Jaeger. Delivery to MLflow continued within NFR-OBS-1's 30 s; a longer
  Jaeger outage under sustained load was not measured, nor why the OOM case was slower (Rotel's per-exporter
  queueing was not read), so the [image] test repeats this with the shipped limits.
- **Tests if accepted:** [config] the networks and VEC-DEPLOY-2 pairs, the 8444 site and its CSP; [unit] the gate's
  per-site table (INV-OBS-5's cross cases and the main site's `app`); [image] Jaeger starts under the hardening,
  receives a span through Rotel, holds `max_traces` × 72 spans with VEC-OBS-2's full key sets (32,400 spans) under
  the shipped 256m limit and `GOMEMLIMIT` with no OOM kill (`docker inspect` OOMKilled false; `docker stats` below
  the limit; never `docker exec`) and records the fill's wall time, Jaeger's CPU from `docker stats` and the
  seconds until Jaeger's API returns the last trace (a GC-bound collector that stays alive but stalls shows there),
  the CSP hashes recomputed from the image's served index equal the configured ones (an image bump that
  changes the inline scripts fails), and MLflow delivery within 30 s with Jaeger stopped; [live] 8444 answers 401
  without a cookie and renders a trace for the owner.
