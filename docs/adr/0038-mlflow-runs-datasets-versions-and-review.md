---
id: ADR-0038
title: MLflow Runs, evaluation datasets, agent versions, owner labels and MLflow operations (amends ADR-0035 and ADR-0032)
status: accepted
date: 2026-11-24
verified_by: "spikes/obs-stack rounds 6-11: results/20261124T142803-round6-phase2 (tabs before/after, quotes.json), results/20261124T141648-round6-phase2 (pool exhaustion under evaluate), results/20261124T143808-round7-runs (Runs in another experiment), results/20261124T150936-round8-rest (every write as raw REST: round8.json `raw`, r8/*.png, quotes.json), results/20261124T151501-round9-ops and results/20261124T151935-round9-ops (SQLite journal modes under 8 writers, disabled features, health probes, online backup and restore, the stop-during-migration failure, load, upgrade), results/20261124T153052-round10-setup (setup drift, finalize, retention deletes, Host and CORS checks), results/20261124T154753-round11-egress (no egress from mlflow's networks, with a positive control); if accepted, SRV-OBS phase 2's [unit], [db], [image] and [live] tests and INV-OBS-2's canary over every new request body"
revisit_when: "MLflow ingests run links, logged-model links or trace metadata through OTLP; or a bench needs a model call to score (an LLM judge); or a second user needs the dashboards; or the AI gateway is unblocked; or the measured SQLite limits of FR-OBS-28 are reached"
supersedes: []
---

## Context

> Amendment proposed by [ADR-0039](0039-knowledge-layers-and-amendment-rules.md) (2026-11-26): this ADR's `revisit_when` is met (a bench needs an LLM judge): a local worker job runs `judge.eval.*` and writes scores only; judge-named owner labels and judge-instruction alignment join the catalogue. In force once ADR-0039 is accepted; then, where the two disagree, ADR-0039 wins.

The owner asked on 2026-11-24 to "use all advantages of MLflow to manage everything here as a dashboard": Runs for
compile runs and imports, the bench sets as evaluation datasets with their results as evaluation runs, agent
versions, labeling, budgets and alerts. He then added: "all MLflow configuration must be done properly so it is a
production-ready solution".

SRV-OBS phase 1 (FR-OBS-12..19) fills the Traces, Sessions, Overview, Quality and Prompts tabs. ADR-0035 (accepted)
lets the worker make exactly two kinds of MLflow writes beside OTLP and retention's deletes: code assessments from a
declared catalogue, source CODE; and the repository's static prompt templates. Phase 2 needs more writes and owner
labels with source HUMAN. Under AGENTS.md that is an ADR for the owner.

ADR-0034 (rejected 2026-11-24) proposed an opt-in mode that would put the owner's own lesson and tutor text into
traces, so that MLflow's chat view, Review queues and text Datasets would have content. This ADR differs:

- it puts no lesson, book, question or answer text into MLflow;
- its only non-numeric content is the committed bench questions, which are the agent's own wording, verbatim-checked
  against the book;
- it keeps labels in Postgres as the record (FR-OBS-24) and mirrors only a pass/fail value and enums.

So MLflow's Review queues and label schemas, which would show empty traces without text, are not used. That is the
view ADR-0034 was rejected for.

What MLflow 3.18.0 offers, measured in spike rounds 6 to 11 (zero credit, own stacks):

- **Links.** OTLP sets a trace's session, user and `mlflow.traceTag.*` tags only.
  - A trace reaches a Run only through `link-to-run`, at most 100 trace ids a call (round 8: 120 gave 400). The
    link writes `mlflow.sourceRun`.
  - A Run's Traces tab searches only the Run's own experiment (round 7). So Runs with traces live in experiment 0.
  - A LoggedModel lists a trace only through trace metadata, which OTLP does not set and no API sets later. Versions
    are compared by a trace tag instead.
- **Run params.** Params are write-once (round 8: a changed value is 400). Anything that changes between two ends of
  a run is a metric (re-logged at a new step) or a tag.
- **Assessments.** A HUMAN assessment is created by POST. A relabel is a second POST with `overrides` set to the
  first one's id; MLflow marks the first `valid: false` (round 8). PATCH also updates in place.
- **Evaluate.** `mlflow.genai.evaluate` writes one client-side trace per row with the row's inputs, outside our
  allow-list. At default parallelism it exhausted the SQLite pool (round 6). Plain Runs with metrics fill the same
  Evaluation runs tab.
- **Labeling.** Labeling sessions are Databricks-only. Review queues and label schemas are OSS but show nothing
  useful on text-free traces.
- **Budgets.** Budgets are AI-gateway policies only. Our priced OTLP traces left `current_spend` at 0, and an
  exceeded window fires an outbound webhook, while MLflow has no egress.
- **Operations** (round 9):
  - With `MLFLOW_ENABLE_AI_GATEWAY=false`, the gateway answers 501. Assistant, jobs and MCP answer 404.
  - SQLite in WAL mode took 8 concurrent writers plus an OTLP stream with 0 errors (p95 0.20 s). The default
    journal mode gave p95 0.43 s, and its 68 errors were assessments posted before their traces were ingested.
  - An online backup through the sqlite3 backup API took 0.02 s and served every trace again from a second
    MLflow.
  - A server stopped during its own first-start migration left the file unable to start, and 3.18.0 runs pending
    migrations itself at start.
  - Round 10: setup made 0 writes on a re-run. Round 11: mlflow's networks reach neither an IP literal nor DNS.

## Decision (proposed)

**1. Writes.** The worker may make exactly the MLflow writes listed in VEC-OBS-5 `writes`, on the `obs` network, in
addition to ADR-0035's two writes and retention's deletes.
- Every request body comes from a declared catalogue (VEC-OBS-5: patterns, enums and ranges, unknown keys
  rejected).
- Every write goes through the worker's own HTTP client, never through `mlflow.genai.evaluate` or the MLflow client
  package.

The job kinds are `obs.setup`, `obs.runs`, `obs.bench` and `obs.label`, plus `obs.retention`, which is extended.
- `obs.setup`: experiments and agent versions from committed data (FR-OBS-21, FR-OBS-26).
- `obs.runs`: a Run per compile run or import, and the trace links (FR-OBS-20).
- `obs.bench`: datasets and evaluation Runs from an owner upload (FR-OBS-22, FR-OBS-23).
- `obs.label`: the HUMAN mirror of owner labels (FR-OBS-24).
- `obs.retention`: also deletes old Runs and LoggedModels (FR-OBS-31).

**2. Amendment of ADR-0035.** ADR-0035's assessment catalogue had one source type, CODE. This ADR adds HUMAN
assessments with the names `owner.lesson_ok` and `owner.answer_ok`.
- The value is pass/fail; the metadata is only ids, a reason enum and a revision number.
- They are posted by `obs.label`, not by `obs.feedback`.
- A relabel posts a new revision with `overrides`.
- ADR-0035's canary and failure rules apply unchanged.

**3. Labels have Postgres as the record.** The owner's label is stored in `owner_labels` first. MLflow holds a mirror
that may lag or be missing (tracing off, no trace yet), and it never feeds back.

**4. Bench results have Postgres as the record.** `obs_bench_results` holds each upload's declared numbers. The api's
alerts read Postgres only; the api never reads MLflow.

**5. Operations as code.**
- MLflow's experiments, tags and names come from `deploy/otel/mlflow-setup.yaml`. One idempotent setup function
  applies them, from the `obs.setup` job and inline wherever an experiment is needed.
- Every server flag and environment variable is pinned in compose with its source.
- The store is SQLite in WAL mode on the `obs_mlflow` volume (FR-OBS-28). Freed pages are reused, so there is no
  VACUUM.
- Schema migrations run in a one-shot `mlflow-migrate` service before the server starts (FR-OBS-29). It works
  under an exclusive `flock`, keeps exactly one pre-migration backup, and has a manual restore command.
- Retention covers traces, Runs and LoggedModels (FR-OBS-31).
- The stack's health:
  - `reader doctor` and `reader status` read `docker inspect` and the volume's size on the host;
  - `/observability` shows `obs_stack_status`, which a 30-minute untraced `obs.health` job fills (FR-OBS-32).

**6. Backup and reach, replacing two parts of ADR-0032 Decision 1.**
- ADR-0032 Decision 1 put `obs_mlflow` outside `reader backup`. This Decision replaces that exclusion: `obs_mlflow`
  joins `reader backup` (FR-OBS-30).
  - The copy always goes through SQLite's backup API, with the stack up or down.
  - Traces come along, because they share the file.
  - The volume's backup label in compose changes accordingly (OPS-DEPLOY FR-DEPLOY-15).
- ADR-0032's "the worker reaches mlflow for retention only" is widened to the job kinds of Decision 1. The api still
  never reaches MLflow.

Each request body passes INV-OBS-2's canary test. A failed write is logged; it never changes a job's outcome and never
blocks the worker's start (INV-OBS-3). Budgets and alerts stay outside MLflow, on `/observability` (FR-OBS-25), and a
push notification waits for SRV-NOTIFY.

## Consequences

- MLflow becomes the owner's dashboard for compile runs and imports, bench regressions over time, agent versions and
  label mirrors, at zero credit.
- The worker's MLflow write channel grows from two request kinds to VEC-OBS-5's list, still bounded by catalogues and
  the canary. The api gains two owner-only routes, `POST /api/obs/bench` and `POST /api/obs/labels`, and two
  Postgres tables.
- The committed bench questions (the agent's own paraphrases; no 5-word run shared with the book, by the verbatim
  check) leave the repository for the owner's own MLflow store. That is phase 2's one non-numeric content.
- `obs_mlflow` is backed up now, so a restore brings Runs, datasets and versions back. Traces come along because they
  share the file; they stay diagnostic, and retention still trims them.
- If the owner rejects this ADR, phase 2 shrinks to what OTLP and ADR-0035 allow without it: the version tag on
  traces (FR-OBS-21 a), the alerts computed from the ledger (FR-OBS-25 without `bench_regressed`), and the operations
  items that add no write (FR-OBS-27, -28, -29, -32, -33, -34). Owner labels would then live in Postgres only, with no
  MLflow mirror: ADR-0035 allows only CODE assessments.
