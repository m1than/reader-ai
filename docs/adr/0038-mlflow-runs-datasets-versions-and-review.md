---
id: ADR-0038
title: MLflow Runs, evaluation datasets, agent versions and a review queue as the owner's dashboard (amends ADR-0035)
status: proposed
date: 2026-11-24
verified_by: "spikes/obs-stack round 6 (results/20261124T142803-round6-phase2: phase2.json, p2-before.json, p2-after.json, p2/*.png, quotes.json; the pool-exhaustion run 20261124T141648; round 7, results/20261124T143808-round7-runs: round7.json, r7.json, r7/*.png); if accepted, SRV-OBS phase 2's [unit], [db] and [image] tests and INV-OBS-2's canary over every new request body"
revisit_when: "MLflow ingests run links, logged-model links or trace metadata through OTLP; or a bench needs a model call to score (an LLM judge); or a second user needs the dashboards; or the AI gateway is unblocked"
supersedes: []
---

## Context

The owner asked on 2026-11-24 to "use all advantages of MLflow to manage everything here as a dashboard": experiment
Runs for compile runs and imports, the repository's bench sets as evaluation datasets with their results as
evaluation runs, agent versions, labeling, and budgets and alerts. SRV-OBS phase 1 (FR-OBS-12..19) fills MLflow's
Traces, Sessions, Overview, Quality and Prompts tabs. ADR-0035 (accepted) lets the worker make exactly two kinds of
MLflow writes beside OTLP and retention: code assessments from a declared catalogue, and the repository's static
prompt templates. Phase 2 needs more writes; under AGENTS.md that is an ADR for the owner.

What MLflow 3.18.0 offers, measured in spike round 6 (zero credit, own stack):

- OTLP sets only a trace's session, its user and its `mlflow.traceTag.*` tags.
  - A trace reaches a Run only through `POST /api/2.0/mlflow/traces/link-to-run` (at most 100 traces a call; it
    writes `mlflow.sourceRun`).
  - A LoggedModel ("agent version") would list a trace only through the trace metadata `mlflow.modelId`. OTLP does
    not carry that key, and neither the client nor REST can set it on an existing trace. A trace tag
    `mlflow.modelId` is searchable.
- Runs, params, metrics, evaluation datasets (`/api/3.0/mlflow/datasets/*`), logged models
  (`/api/2.0/mlflow/logged-models*`), label schemas and review queues (`/api/3.0/mlflow/label-schemas/*`,
  `/api/3.0/mlflow/review-queues/*`) are OSS REST APIs in 3.18.0. Labeling *sessions* are Databricks-only.
- In a GenAI experiment, every Run with linked traces or metrics appears in the Evaluation runs tab as type `Eval`,
  which charts its metrics from the earliest run to the latest.
- `mlflow.genai.evaluate` writes one trace per row from the client, with the row's inputs. That write bypasses our
  allow-list, and at default parallelism it exhausted the SQLite pool. Plain Runs with metrics fill the same tab
  without it.
- Budgets exist only as AI-gateway policies (`/api/3.0/mlflow/gateway/budgets/*`):
  - only gateway traffic moves a window: our priced OTLP traces left `current_spend` at 0;
  - an exceeded window fires a `budget_policy.exceeded` webhook, which is an outbound call from MLflow.

  We do not use the gateway (it is blocked), and MLflow has no egress. MLflow therefore has no budget or alert for
  our spend.

## Decision (proposed)

The worker may make exactly the MLflow writes below on the `obs` network, in addition to ADR-0035's two writes and
retention's deletes. Every write comes from a declared catalogue (names, value types, enums, ranges). It carries ids,
enums, numbers and pinned repository data only, never free text. Every write goes through REST from the worker's own
HTTP client, never through `mlflow.genai.evaluate`.

1. **Runs (job `obs.runs`).** After a compile run or an import ends, the worker writes one Run:
   - declared params: ids, model names, the version key (Decision 2) and the reader version;
   - declared metrics: ledger cost by stage, token sums, durations and counts;
   - then `link-to-run` for that run's traces.

   The job is idempotent by a `reader.run_id` or `reader.ingest_id` tag, looked up first. The Runs live in
   experiment 0, beside their traces. Spike round 7 put a Run in its own experiment: the link was accepted, but the
   Run's Traces tab found no traces, because the UI searches only the Run's own experiment.
2. **Agent versions (at worker start, beside `obs.prompts`).** One LoggedModel per distinct version key. The key is
   the sha256 of the prompt-set sha256, the stage → model map and the pipeline config's sha256, and those three are
   its params. Each job's root span carries `mlflow.traceTag.reader.version_key` (the key's first 12 hex characters)
   through the allow-list. The Agent versions tab lists the versions with their params. Traces are compared by the tag
   filter, because the tab's own trace list cannot be filled from OTLP.
3. **Evaluation datasets and runs (job `obs.bench`).** The job is enqueued by an owner-only API call that carries a
   bench result file.
   - Datasets: a dataset is registered only from a bench set whose file sha256 is pinned in SRV-OBS's catalogue,
     and only with records that share no run of 5 or more consecutive words with the pilot's text layer (spike
     round 6 `quotes.json`).
   - Evaluation runs: one Run per bench run, holding the result's declared numbers (hit@k, reciprocal rank,
     grounded, gold cited, latency, cost) and per-question scores as stepwise metrics. The worker computes the
     scores from ids and page numbers: no model call, no judge.
   - Datasets and evaluation runs live in their own experiment `bench`. A bench Run has no traces, so it loses
     nothing there (round 7: a separate experiment's Evaluation runs tab lists and charts its Runs). Experiment 0's
     Evaluation runs tab then holds only compile and import Runs.
4. **Review queue (at worker start).** One label schema per declared human label (pass/fail, no comment) and one
   custom review queue in experiment 0. Compile and tutor traces are added to the queue by id. The owner labels in
   MLflow's Review tab, or from the app: `POST /api/obs/labels` enqueues job `obs.feedback` with source `HUMAN` and
   ids-only metadata.

Each request body passes INV-OBS-2's canary test. A failed write is logged; it never changes a job's outcome and
never blocks the worker's start. Budgets and alerts stay outside MLflow: `/observability` shows spend against the
existing caps (SRV-AGENT's daily and run caps), and a notification waits for SRV-NOTIFY.

## Consequences

- MLflow becomes the dashboard for runs (compare compile runs on cost and quality), bench regressions over time
  (a re-run is a new evaluation run), agent versions and owner labels, at zero credit.
- The worker's second write channel grows from two to six request kinds, still bounded by catalogues and the
  canary; the bench path adds one owner-only API route.
- Committed bench question text (the agent's own paraphrases, no book quotes by the measured check) leaves the
  repository for the owner's own MLflow store; that is the one non-numeric content in phase 2.
- If the owner rejects this ADR, phase 2 shrinks to what OTLP and ADR-0035 already allow: the version tag on
  traces and app labels as `HUMAN` assessments through `obs.feedback`.
