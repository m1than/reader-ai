---
id: ADR-0035
title: Worker writes of code assessments and prompt templates to MLflow (amends ADR-0032 Decision 1)
status: accepted
date: 2026-11-24
verified_by: "spikes/obs-stack round 4 (results/20261124T105821-round4-tabs: 8 of 8 assessments created, 3 prompts registered, the Quality dashboard and Prompts tab filled); if accepted, SRV-OBS FR-OBS-14/-15's [unit], [db] and [image] tests and INV-OBS-2's canary over both request bodies"
revisit_when: "MLflow can ingest assessments or prompt links through OTLP; or the worker gains any other MLflow write"
supersedes: []
---

## Context

> Amendment proposed by [ADR-0039](0039-knowledge-layers-and-amendment-rules.md) (2026-11-26): Decision 1's assessment source widens from `CODE` to also `LLM_JUDGE`, written by the worker's `obs.eval` job only, scores and enums only, never text. In force once ADR-0039 is accepted; then, where the two disagree, ADR-0039 wins.

ADR-0032 Decision 1 amends ADR-0025 rule 5 so that the worker reaches `mlflow` "for the retention job's delete calls
only". SRV-OBS's proposed V.obs-analytics amendment adds two more worker → MLflow REST writes:

- **FR-OBS-14, code assessments:** after a job computes a deterministic outcome (fact-check passed, lab cells passed,
  citations valid), a job `obs.feedback` POSTs an assessment to the job's trace. It fills MLflow's Quality dashboard.
- **FR-OBS-15, prompt registry:** at start the worker registers the repository's static prompt templates (never book
  text), so traces link to the template version they used.

Both send data outside the span exporter's allow-list, through a second channel. The design review of 2026-11-24
(round 1, B5) found that this contradicts Decision 1. Under AGENTS.md this needs an ADR, and the owner decides.

## Decision (proposed)

The worker may make exactly these MLflow writes on the `obs` network, in addition to retention's deletes:

1. `POST …/mlflow/traces/{id}/assessments` (and the lookup GET before it), with bodies built only from a declared
   assessment catalogue (name, value type or enum, metadata keys with ranges, source `CODE` and a module id). No
   free text: no rationale, no error message.
2. `POST …/mlflow/registered-models` / prompt-version creation and tag calls for the repository's static prompt
   files, checked by a unit test against the secret scanner and for placeholders that a renderer would fill with
   book text.

Each body passes INV-OBS-2's canary test (no book text, question, answer, system prompt, run token or secret).
Each failure is logged and never changes a job's outcome or blocks the worker's start.

## Consequences

- MLflow's Quality dashboard and Prompts tab fill with deterministic data.
- A second write channel to MLflow exists beside OTLP; it is bounded by its catalogue and the canary.
- Until the owner accepts this ADR, SRV-OBS FR-OBS-14 and FR-OBS-15 stay blocked; the rest of the amendment
  proceeds without them.
