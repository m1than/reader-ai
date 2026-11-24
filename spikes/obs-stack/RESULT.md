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

## Round 3 (SRV-OBS review round 2, N1), 2026-11-24

- **Command:** the same, with the candidate CSP.
- **Results:** `results/20261124T013500-round3/` (`browser.json` → `click`, `gate.log`,
  `shots/click-from-app-port.png`).
- **Gate rule added:** a `Sec-Fetch-Site` other than `same-origin`/`none` is refused, unless it is `same-site` with
  `Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest: document` and a forwarded GET.
- **Blocked list:** gains `/ajax-api/*/mlflow/gateway-proxy*`, and the assistant pattern now uses the `*` form.
- **The test:** a stand-in for the app's `/observability` page is served (ungated) on :8447, another port of the same
  site, with a link to the dashboard's deep link on :8446. The real browser clicks it, then issues subresource and
  fetch requests from that page.

| Request from the :8447 page | Sec-Fetch (site, mode, dest) | Gate | Browser outcome |
| --- | --- | --- | --- |
| Click on the trace link | same-site, navigate, document | 204 | the trace's span tree rendered (`job tutor.answer`, `agent.call`, `claude_code.interaction`, `claude_code.llm_request`) |
| `<img src=…/ajax-api/…>` | same-site, no-cors, image | 403 | error |
| `<script src=…/ajax-api/…>` | same-site, no-cors, script | 403 | error |
| `fetch(…, {mode: 'no-cors', credentials: 'include'})` | same-site, no-cors, empty | 403 | opaque |
| `fetch(…, {credentials: 'include'})`, Origin `http://localhost:8447` | same-site, cors, empty | 403 (Origin rule first) | rejected |

- **Requests after the navigation**, from the dashboard's own page: `same-origin`, and they pass.
- **Query strings:** Caddy's `forward_auth` appends the original query string to the gate's URI
  (`/api/auth/owner-gate?max_results=1&probe=img`), so the gate must ignore its own query.
- **Blocked paths:** all five probes, the spec's `*` forms included, answered Caddy's own 404.

## Round 4 (V.obs-analytics: MLflow's own analytics, every tab, the timeline, Jaeger), 2026-11-24

Zero credit, the spike's own project (`reader-spike-obs`), no published port, stopped after every run. Synthetic
spans only (ids, enums, numbers; the CLI-shaped spans use VEC-OBS-2's keys). The browser passes run headless
Chromium under SRV-OBS FR-OBS-9's CSP (`:8446`). MLflow 3.18.0 facts below were read in the pinned image's source
(`docker run --network none … mlflow`), then checked against the running server.

- **Commands:** `bash run-analytics.sh` (analytics.py, analytics-browser.js, analytics-filters.js,
  analytics-timeline.js); `bash run-tabs.sh` (analytics_tabs.py with `VARIANT=before|after`, analytics-tabs.js);
  `BULK=50 bash run-jaeger.sh` (compose.jaeger.yaml, jaeger.yaml, jaeger_check.py, jaeger-browser.js).
- **Results:** `results/20261124T102655-round4/` (REST views, searches, trace metrics, the timeline:
  `trace-tree.png`, `trace-timeline.png`, `timeline.json`); `results/20261124T101104-round4/filters.json` (URL
  forms); `results/20261124T105821-round4-tabs/` (every tab before and after: `tabs/before-*.png`,
  `tabs/after-*.png`, `tabs-data-after.json` with the per-lesson cost queries, `tabs-after.json` with the traces
  table's menus); `results/20261124T103924-round4-jaeger/` (`jaeger-timeline.png`,
  `jaeger.json`, `stats.txt`).

### What MLflow 3.18.0 does with OTLP spans (source, then measured)

| Mechanism | Where (3.18.0) | Measured |
| --- | --- | --- |
| Attribute values from a plain OTel client are JSON-encoded once, then a value that is itself JSON (`'{"a":1}'`) is unwrapped into a structure | `entities/span.py` `from_otel_proto`; `tracing/otel/translation/__init__.py` `strip_otlp_double_encoding` | `mlflow.spanInputs`/`mlflow.spanOutputs` sent as JSON strings show as structured Inputs/Outputs; `mlflow.llm.cost` as a JSON string is read |
| Span type: an explicit `mlflow.spanType` wins; otherwise translators set it at ingest (GenAI semconv `gen_ai.operation.name`: `invoke_agent` → AGENT, `chat` → CHAT_MODEL…; Claude Code `span.type`: interaction → AGENT, llm_request → LLM, tool → TOOL) | `translation/genai_semconv.py`, `translation/claude_code.py` | today `agent.call` becomes **LLM** (gen_ai keys present, no operation name); with `invoke_agent` it is AGENT |
| The stored span-type column (span search and per-type metrics) takes only an explicit `mlflow.spanType`; translated types live in the content | `sqlalchemy_store.py` (`"type": span.span_type`) | `span.type = 'AGENT'` matched nothing until `agent.call` carried `mlflow.spanType: AGENT` |
| Token usage at ingest: GenAI keys `gen_ai.usage.input_tokens`, `output_tokens`, `cache_read.input_tokens`, `cache_write.input_tokens` (legacy `cache_creation.input_tokens`); Claude Code keys `input_tokens` (+ cache, normalized to include it) | `translation/genai_semconv.py`, `claude_code.py` | our `gen_ai.usage.cache_read_input_tokens` / `cache_creation_input_tokens` are **not** read; the CLI's are |
| Trace token usage and cost: tree-aware sums (a span with usage hides its descendants' usage; the same for cost, separately) | `tracing/utils/__init__.py` `_aggregate_from_nodes` | today: trace usage = `agent.call`'s cache-less input (wrong); with semconv keys including the cache: equals the CLI's |
| Cost: computed at ingest for spans with usage and a model, **not** for AGENT spans; prices from MLflow's bundled catalog (no network): `claude-sonnet-5-5`, `claude-opus-5-5` present | `calculate_cost_by_model_and_token_usage`; `utils/model_catalog/anthropic.json` | the CLI's `llm_request` priced offline; today's `agent.call` (typed LLM) was priced too |
| Span-level cost metrics (Dashboard cost) sum every priced span, without the tree dedup | `query_trace_metrics` (`total_cost` by `span_model_name`) | with a priced `agent.call` and a priced `llm_request` in one trace, the dashboard showed the sum (0.93774 in the round-4 mix): a double count |
| Trace tags from root-span attributes `mlflow.traceTag.<key>`; the trace name from `mlflow.traceTag.mlflow.traceName`; the session from root `session.id`; linked prompts from the tag `mlflow.linkedPrompts` | `sqlalchemy_store.py`, `server/otel_api.py` | all four set and searchable (`tag.\`reader.stage\` = 'review'`, `trace.name = …`, `metadata.\`mlflow.trace.session\` = …`); "1 linked prompt" shown |
| Trace metrics API `POST /ajax-api/3.0/mlflow/traces/metrics`: TRACES (count, latency, tokens; by trace name or status), SPANS (count, latency by name/status; cost by model/provider; count by type), ASSESSMENTS | `tracing/constant.py` `TraceMetricKey`… | all queried; `trace_name` dimensions empty until the trace-name tag was set |

### Before and after, tab by tab (`results/20261124T105821-round4-tabs/tabs/`)

The `after` stack runs with `compose.catalog.yaml` (the ledger as MLflow's cost source, next section).

| Tab (route) | Before (today's spans) | After (proposed) |
| --- | --- | --- |
| Traces (`#/experiments/0/traces`) | rows with "-" for Input, Output, Session; tokens from the cache-less `agent.call` | Input/Output previews `{"question_id": …, "lesson_id": …}` / `{"outcome": "answered", "citations": 4}`; Session; Duration; State; Tokens; 9 tags per trace |
| Trace detail | `agent.call` typed LLM; no Inputs/Outputs | CHAIN root, RETRIEVER `tutor.retrieve`, AGENT `agent.call`, AGENT/LLM CLI spans, PARSER `tutor.citations`; header "9.6K tokens ≈$0.11" (the ledger amount), `agent.call` ≈$0.11, the CLI's `llm_request` ≈$0.00; "11 tags"; "1 linked prompt"; 2 assessments on the root |
| Execution timeline (button "Show execution timeline" in the Spans pane) | — | real Gantt bars on a shared axis with durations, nested, parallel stages visible; **narrow** (about 300 px of a 1,920 px window, labels truncated; no zoom or minimap): `results/20261124T102655-round4/trace-timeline.png` |
| Sessions (Traces/Sessions switch; `?groupBy=session`) | "0 sessions" | "2 of 2 sessions": the compile run and the tutor lesson, each expandable to its traces |
| Dashboard › Usage (`#/experiments/0/dashboard/usage`) | counts, latency p50/p90/p99, errors; tokens wrong (cache-less); cost double-counted | trace count, latency, error rate, tokens with input/output/cache read/cache write (198.43K: 180.46K input, 17.97K output, 147.00K cache read, 32.00K cache write), cost = the ledger amounts on `agent.call` only |
| Traces, Cost column (Display › Columns › Cost; `after-traces-cost-column.png`) | — | each trace's ledger cost (≈$0.62, $0.30, $0.21, $0.23, $0.05, $0.11, $0.03); not sortable |
| Traces, one lesson (`?q=<lesson id>`; `after-traces-lesson.png`) | — | "5 of 10 traces": exactly that lesson's 3 pipeline and 2 tutor traces |
| Dashboard › Quality | empty ("computed by scorers") | Quality summary: `fact_check.passed` (true/false), `verify.lab_passed`, `freshness.verdict`, `tutor.citations_valid`, `tutor.citation_share` (avg 0.75), with per-value bars |
| Dashboard › Tool calls | empty | `verify.lab` calls, latency, failures; success rate counts only spans with status OK (set OK on success) |
| Prompts | empty | `reader.tutor`, `reader.fact_check`, `reader.review`, version 1, tag `reader.git_sha` |
| Overview | the onboarding page ("Build a continuous quality loop") with the trace count | the same; it is a guide, not a chart page |
| Issues | "Discover issues automatically" (LLM-based detection) | unchanged: it needs an LLM provider (credit), stays unused |
| Judges, Review, Datasets, Evaluation runs, Agent versions | empty | empty (see the spec's per-tab table for why) |

CSP: zero violations on every tab, before and after.

### Cost per lesson, with the ledger as MLflow's cost source

Owner, 2026-11-24: per-lesson pricing in the traces; pricing.yaml (the ledger) is the source of truth. The `after`
data (`analytics_tabs.py`): two lessons of one course; lesson `…bb` has a compile run with a failed paid
`fact_check` attempt (0.23, outcome failed, `Transient`), its retry (0.21), a dead `review` (0.30) and two tutor
answers (0.03, 0.11); lesson `…dd` one `draft` step (0.62) and one tutor answer (0.05). Each root carries
`reader.lesson.id`, `reader.course.id`, `reader.step`, `reader.cost_usd` and (pipeline) `reader.run.spent_usd`; each
`agent.call` carries `mlflow.llm.cost` with `total_cost` = the ledger amount.

| Question | Measured (`tabs-data-after.json`, `tabs-after.json`) |
| --- | --- |
| Is an explicit `mlflow.llm.cost` on an AGENT span kept? | yes: `agent.call` cost 0.21 = the ledger amount |
| Does MLflow still price the CLI's `llm_request`? | with its bundled catalog, yes (`before`: 0.0631 on the CLI span beside 0.0401 on `agent.call`, summed by the dashboard). With `MLFLOW_MODEL_CATALOG_URI=file:///catalog` and a catalog pricing our two models at 0 (`catalog/anthropic.json`; MLflow reads `file://` catalogs, `utils/providers.py` `_fetch_local_provider`): 0.0 |
| Trace cost (header, Cost column) | = the call's ledger amount: 0.21, 0.23, 0.03 |
| Span cost metric, all traces | 1.55 = 0.03 + 0.11 + 0.05 + 0.23 + 0.21 + 0.30 + 0.62: no double count |
| Span cost per lesson (filter `trace.tag.\`reader.lesson.id\` = '<id>'`) | 0.88 and 0.67, the two lessons' ledger sums (the failed paid attempt included) |
| Span cost grouped by a tag | refused, 400: "Supported dimensions: ['span_model_name', 'span_model_provider']" (`span_name` too) |
| Trace-level cost metric | none: TRACES metrics are trace_count, session_count, latency and tokens (400 for `total_cost`) |
| Traces table: a cost column | yes, Display › Columns › Cost; tag columns too (`reader.lesson.id`, `reader.cost_usd`, `reader.run.spent_usd` …) |
| Traces table: sort by cost | no: Sort offers Time, Duration, State; the Cost header's menu has only "Hide column" |
| Search a lesson's traces | `tag.\`reader.lesson.id\` = '<id>'`: 5 and 2 traces; with `tag.\`reader.job.kind\` = 'pipeline.stage'`: 3 |

So MLflow shows each trace's and each call's ledger cost and filters a lesson's traces, but it cannot rank lessons
by cost or group cost by lesson; that view belongs to `/observability` (SRV-OBS FR-OBS-17). The input/output split
in this run used placeholder prices (3/15/0.3/3.75 per million); `analytics_tabs.py` now uses pricing.yaml's
(2/10/0.1/2.5). The totals are the ledger amounts either way.

### Saved views (links)

- Measured: the traces tab keeps its filters in local view state; the URL carries only `q` (full text), `sort`,
  `dir`, `startTimeLabel`/`startTime`/`endTime` and `groupBy` (read in the UI bundle, `mlflow.tracesV4ViewState.*`).
  `filter=<field>::<op>::<value>` links rendered all rows (8 of 8) in every form tried, with `filter` and `filters`.
- Working links (rows rendered, `results/20261124T101104-round4/filters.json`): `?q=exhausted` (1 of 8: the dead
  stage), `?q=dead` (1), `?q=job%20tutor.answer` (3), `?q=fact_check` (3), `?groupBy=session` ("1 session" before
  the second session existed). `sort=execution_duration&dir=desc` did not reorder rows. `q` searches span content,
  so it matches our tag and attribute values (ids, enums).

### The timeline and the Jaeger option (`results/20261124T103924-round4-jaeger/`)

- **MLflow:** the timeline exists (above) but is cramped for a 45-span compile trace.
- **Jaeger v2.23.0** all-in-one, `jaegertracing/jaeger:2.23.0@sha256:3823829974ccfc2485d2e48b165579091f32295022ae80ca49704e2f4834116e`
  (arm64, 169 MB), as Rotel's second named exporter (`--exporters=mlflow:otlp,jaeger:otlp`,
  `--exporters-traces=mlflow,jaeger`, settings in `ROTEL_EXPORTER_<NAME>_<FIELD>`; `ROTEL_OTLP_EXPORTER_<NAME>_*` is not
  read: "Exporter[mlflow]: must specify an endpoint"). In-memory storage with `max_traces`.
  - Delivery: the same traces reached both (Jaeger: all 45 spans of the compile-like trace; services `reader-worker`,
    `claude-code`).
  - Memory: 12 MiB idle; 35.5 MiB after 300 compile-like traces (13,500 spans; the first Jaeger pass); 24.5 MiB after
    50 (2,250 spans) and the browser pass (the kept run). MLflow beside it: 358–398 MiB.
  - UI: a full-width waterfall with a minimap, time axis, nesting, parallel stages, durations, "GenAI View" and
    "GenAI calls: 16" (`jaeger-timeline.png`).
  - Gate: Jaeger's UI loads ES modules (CORS mode, `Origin` sent), so its own origin must pass the gate's Origin rule.
  - CSP: two inline scripts (`sha256-eoBUh2b2awnlKCd0fkQUIXAAxPdxWV4aIas48E/3hLU=`,
    `sha256-07X4W69Q7ST165IvjVZwZU+G34LWjkh2Xj2s9fMwqYQ=`) and `base-uri 'self'` (its `<base href>`; with `'none'` its
    modules resolved under `/trace/` and failed). One `script-src eval` violation remains and the page still renders.
  - Retention: the in-memory store evicts beyond `max_traces` and empties on restart; Badger (on a volume) has a TTL.
  - Egress: none needed; the spike networks are internal.

## Round 5 (V.obs-analytics design review round 1: B1, B2, B8), 2026-11-24

Zero credit, project `reader-spike-obs`, no published port, every stack taken down (0 containers after each run).
Commands: `bash run-round5.sh` (stacks A–D), then `bash run-round5e.sh <that result dir, absolute>` (stacks E, F).
Driver `round5.py` (`PHASE=cost|session|gate|bulk|bulkfull|probe`); catalog generator `gen_catalog.py` (run inside the
pinned MLflow image; it wrote `catalog/anthropic.json`: the image's 31 bundled Anthropic ids, pricing.yaml's two among
them, all at 0); `mlflow-catalog.Dockerfile`, `compose.round5.yaml`, `compose.round5e.yaml`, `jaeger-pages.js`. The
spike gate learned per-site origins (`SITE_ORIGINS`, header `X-Reader-Site` set by the spike Caddyfile). Results:
`results/20261124T113402-round5/`.

### B1: where MLflow can still price the CLI's spans

Source (pinned image, `tracing/utils/__init__.py`, `utils/providers.py`; no litellm in the image): a span with a
provider is priced from `MLFLOW_MODEL_CATALOG_URI` first (default: a GitHub release URL, fetched over HTTP), then the
bundled file of that provider; a span without a provider scans every bundled provider file. An empty
`MLFLOW_MODEL_CATALOG_URI` disables the remote fetch.

| Stack (catalog) | pricing.yaml model | bundled model not in pricing (haiku-4-5) | no provider key | unknown model | span cost sum (ledger 0.48) |
| --- | --- | --- | --- | --- | --- |
| A: `file://` mount of the generated catalog | 0.0 | 0.0 | **0.0631** (bundled list price) | none | not isolated (stack A's other traces) |
| B: URI set, mount empty | 0.0631 | 0.03305 | 0.0631 | none | 0.6062 (double count) |
| C: mount empty, start guarded with `test -s … \|\| exit 78` | MLflow exited 78 (`guard.txt`) | | | | |
| **E: bundled catalog replaced in the image, no mount** | **0.0** | **0.0** | **0.0** | none | **0.48** |

So the design moves to E: the derived image deletes the bundled catalog files and copies the generated one in; no
mount can go missing, and the provider-less scan finds only zero prices. The spec also sets the URI empty (read in
source; stack E ran with the default URI on internal networks, where the fetch fails).

### B2: the trace's session

Source: `sqlalchemy_store.py` `log_spans` takes the first span of the first batch with `session.id` or
`gen_ai.conversation.id`, and a later batch never overwrites it. CLI spans with VEC-OBS-2's full key sets
(`session.json`):

| Case | Trace session |
| --- | --- |
| s1: CLI spans exported first, root with `session.id` = the run | the CLI's session id |
| s2: a `job.session` child with the run's `session.id` exported 1.5 s before the CLI | the run |
| s3: the same marker with no gap | the run |

### B8: Jaeger

- **Per-site gate** (`gate.json`, POST with the owner's cookie, 18 cases): the MLflow site passed only with its own
  origin, the Jaeger site only with its own; the other site's origin and no origin were 403; a client-sent
  `X-Reader-Site` was overridden by Caddy's `header_up` (no case changed with it).
- **Memory per span** (`stats.txt`, `stats-f.txt`): minimal spans (compile-like) 0.85–1.1 KB (13.7 → 22.9 → 37.5
  MiB for 9,000 and 18,000 more spans); VEC-OBS-2 full key sets 2.95–3.26 KB (84.7 → 109.6 → 154.7 MiB for 8,000
  and 16,000 more). Stack F's idle reading (84.7 MiB) was much higher than stack A's (12.8 MiB); not explained.
- **A stopped or OOM-killed Jaeger:** stopped, MLflow had 5 probe traces complete 0.6 s after the flush; OOM-killed
  (24m limit, `jaeger-oom-f.txt`: exit 137, OOMKilled true), 16.1–16.3 s; Rotel logged retries to Jaeger. A 48m
  limit with 27,000 minimal spans did not OOM (`jaeger-oom.txt`; MLflow 2.1–2.6 s, Rotel logged timeouts).
- **The eval violation** (`jaeger-pages.json`, ten pages): every page reports one `script-src eval` from
  `static/schemas-hm98w7NF.js` line 2 column 6182, the bundle's `try{return Function(``),!0}catch{return!1}` probe
  behind a `jitless` switch (fetched from a network-less Jaeger by run-round5e.sh; the 148 KB third-party bundle is
  not kept), which matches Zod 4's
  `allowsEval` check; every page rendered.
- **CSP hashes:** recomputed from the served index: the same two hashes as configured.
- **Round 4's memory result:** the first Jaeger pass (300 traces, 35.5 MiB) lost its result directory when
  intermediate runs were cleaned; round 5's `stats.txt` and `stats-f.txt` replace it and are kept.

## Round 6 (SRV-OBS phase 2: Runs, datasets, evaluation runs, agent versions, review, budgets), 2026-11-24

The owner asked to "use all advantages of MLflow to manage everything here as a dashboard". Here `run-phase2.sh`
starts one stack with the zero-price catalog. `before` is the implemented V.obs-analytics data
(`analytics_tabs.py VARIANT=after`). `after` adds `phase2.py`, run in `driver2.Dockerfile` (the full `mlflow==3.18.0`
client) with only the committed bench files mounted read-only. `phase2-tabs.js` screenshots each tab under the
candidate CSP. The kept run is `results/20261124T142803-round6-phase2`. The first run, `20261124T141648`, keeps its
JSON and stderr only; it is described under Evaluation runs below.

- **Book-text check** (`quotes.json`, from `check_bench_quotes.py` over the pilot PDF's text layer, 256,083
  words): for every question, the longest run of consecutive words it shares with the book. The script prints ids
  and lengths only. Results by set:
  - questions-v2: 40 questions, longest shared run 4 words;
  - bench-fixed: t01, longest run 1 word;
  - questions.yaml (v1): 30 questions, longest run 5 words, reached by q04, q07, q08 and q13.

  No question shares 6 or more words with the book. v1 was not written under v2's paraphrase rules, so its four
  5-word records are excluded by the proposed bar: no record that shares 5 or more consecutive words is registered.
- **Runs** (`phase2.json` `link_traces_to_run`, `traces_by_run_metadata`; `p2/after-run-compile*.png`,
  `after-run-ingest.png`):
  - A compile Run carries 9 params and 15 metrics (ledger cost by stage, tokens, duration, claims, lab cells,
    pages, loops), plus a stepwise `cost_usd.cumulative`.
  - `POST /api/2.0/mlflow/traces/link-to-run` returned 200 and wrote `mlflow.sourceRun` on the 4 stage traces.
    The Run's Traces tab lists them with their assessments.
  - An import Run carries 6 metrics.
  - A link from a Run in another experiment was also accepted (200).
  - **Finding:** in a GenAI experiment, a Run with linked traces is listed in the **Evaluation runs** tab with type
    `Eval`. Compile and import Runs therefore mix with bench runs there (`after-evaluation-runs.png`).
- **Datasets** (`after-datasets.png`, `after-dataset-detail.png`): `tutor-bench-v2` (40 records: v2 without v03,
  plus t01; gold pages as expectations) and `search-bench-v1` (30) were created through `mlflow.genai.datasets`.
  The detail page lists the records with their inputs and expectations.
- **Evaluation runs** (`after-evaluation-runs.png`):
  - Three search-bench runs went through `mlflow.genai.evaluate` with code scorers only (hit@3, hit@10, reciprocal
    rank) over the committed per-question ranks; no `predict_fn`, no model call. For bge-m3: hit@10 0.667, hit@3
    0.385, reciprocal rank 0.398 over the 39 shared ids.
  - The tutor bench ran as a Run with metrics (grounded 0.625, gold cited 0.575, p50/p95 latency, mean and total
    USD).
  - The tab charts the metrics from the earliest run to the latest, so a re-run is a new point.
  - **Findings:** `evaluate` writes its own trace per row: 117 of the experiment's 133 traces carry the question
    text as their input. That write comes from the MLflow client and does not pass through our allow-list. With
    the default parallelism it also exhausted MLflow's SQLite pool: in the first run, 32 `QueuePool limit of size
    5 overflow 10` errors, a load failure. With `MLFLOW_GENAI_EVAL_MAX_WORKERS=1` there were 0.
- **Agent versions** (`phase2.json` `versions`; `after-agent-versions.png`, `after-agent-version-*.png`):
  - Two `create_external_model` versions with params (prompt-set sha, model, pipeline version) are listed with
    their parameters.
  - OTLP never sets trace metadata: a root `mlflow.modelId` attribute gave 0 traces by `metadata.mlflow.modelId`.
    `mlflow.traceTag.mlflow.modelId` gave 1 trace by `tag.mlflow.modelId`.
  - The client has no link-traces-to-model API, and REST has no set-trace-metadata call. So the version's Traces
    tab stays empty for OTLP traces (`after-agent-version-traces.png`), and comparing versions works through the
    tag filter.
- **Labeling and Review** (`phase2.json` `labels`; `after-review.png`, `after-run-compile-traces.png`):
  - A `HUMAN` assessment `owner.lesson_ok` = false, with ids-only metadata, landed on the trace. It shows in the
    Run's Traces tab as an assessment column (False 100 %).
  - `create_labeling_session` is Databricks-only (`UnsupportedLabelingStoreURIException`).
  - The OSS label schema (pass/fail "good"/"bad", no comment) and a custom review queue `lessons` with the 4 stage
    traces were created, and the queue is listed with 4 to-do items.
- **Budgets and alerts** (`phase2.json` `budgets`):
  - `gateway/budgets/create` (USD 0.01 a day, GLOBAL, ALERT) returned 200. Its window's `current_spend` stayed 0.0
    after the run's priced OTLP traces: budgets count AI-gateway traffic only.
  - An exceeded window fires a `budget_policy.exceeded` webhook, which is an outbound call from MLflow
    (`mlflow/gateway/budget.py`). The only webhook entities are registry, prompt and budget-policy events.
  - MLflow 3.18 therefore has no budget or alert for our spend, and `/observability` stays the alert surface.
- **CSP:** one violation, a `media-src` from the Overview page's remote demo video, which is blocked and harmless.
  The same page showed it in round 4.
- **Memory** (`stats.txt`): MLflow 424.6 MiB of 1 GiB after both data sets and every tab.
- **Not measured here:** whether the Traces tab of a Run in a *separate* experiment lists traces stored in
  experiment 0. Round 7 measured it.

## Round 7 (SRV-OBS phase 2: where compile Runs live), 2026-11-24

`run-round7.sh` uses one fresh stack. `round7.py` replays 4 stage traces into experiment 0 (OTLP), creates the
experiment `runs` with one compile Run (4 metrics, 1 param), and links the traces to that Run. `round7-tabs.js`
screenshots the result. Kept in `results/20261124T143808-round7-runs`.

- **API** (`round7.json`):
  - `link-to-run` returned 200.
  - Searching experiment 0 for `metadata.mlflow.sourceRun = <run>` found 4 traces; the same search over the
    `runs` experiment found 0.
  - The client's `search_traces(run_id=…)` refused: "Run … belongs to experiment 1, which is not in the list of
    locations provided: ['0']". With both experiments as locations it found 4.
  - The Run is listed in `runs` (1) and not in experiment 0 (0).
- **UI** (`r7.json`, `r7/*.png`):
  - The Run's overview and metric charts show (`run-overview.png`, `run-metrics.png`).
  - Its **Traces tab shows "No traces found"** (`run-traces.png`): the UI searches only the Run's own experiment.
  - The `runs` experiment's Evaluation runs tab lists and charts the Run as `Eval` (`runs-exp-evaluation-runs.png`),
    and its Traces tab is empty.
  - Experiment 0's Evaluation runs tab is empty, and its Traces tab lists the 4 traces.
- **Decision for the design:**
  - Compile and import Runs stay in experiment 0, beside their traces. In round 6 the Run's Traces tab there listed
    the traces with their assessments.
  - Bench datasets and evaluation runs, which have no traces, go to their own experiment `bench`, so they no longer
    mix with compile Runs.
- **CSP:** the same single `media-src` violation from the Overview demo video.

## Limits

- **Memory sampling:** `docker stats` only, at 4–5 s intervals, with page cache included.
- **Browser:** headless Chromium only, plain HTTP, on four pages. Interactions such as editing assessments or
  deleting from the UI were not exercised under the candidate CSP.
- **TLS:** Caddy served plain HTTP here; `tls internal` on 8443 is the same mechanism as the main site (ADR-0024).
- **Volume permissions:** the volume was fresh each run. Ownership through the image directory is the OPS-DEPLOY
  blobs pattern.

## Spend

$0. No model calls.
