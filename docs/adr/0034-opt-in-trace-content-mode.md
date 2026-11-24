---
id: ADR-0034
title: An opt-in trace content mode for the owner's own material (amends ADR-0032 privacy, bounded by ADR-0018)
status: proposed
date: 2026-11-24
verified_by: "Nothing is built under this ADR. If accepted, a separate SRV-OBS revision with its own review round, whose INV-OBS-1/-2 tests cover the mode's off state, its truncation and the personal-use refusal"
revisit_when: "MLflow's chat view, Review queues or text Datasets become necessary to debug the tutor or the pipeline; or the CLI gains a selective prompt-logging flag that can exclude the system prompt"
supersedes: []
---

## Context

The owner asked on 2026-11-24 to use every MLflow tab. Some tabs are only useful with text: the trace detail's chat
view, Review (labeling queues), text Datasets and LLM judges. ADR-0032 (privacy, point 5) and SRV-OBS INV-OBS-1/-2
keep every text out of traces. ADR-0018 (Traces row) already says: "Content capture is opt-in and always off for
`personal-use` material." Split out of the first ADR-0033 draft at the V.obs-analytics design review, round 1 (B9).

## Decision (proposed)

- A setting `observability.content: off | truncated` (default `off`). `off` is today's rule: no text in any span.
- **Always off for `personal-use` material (ADR-0018).** A lesson, question or job whose material is marked
  `personal-use` never exports text, whatever the setting. The Bashir pilot is `personal-use`, so this mode has no
  effect on it.
- **Refused with other users.** With open sign-up (ADR-0031), the mode is refused at setup while any non-owner user
  exists, so no other person's text enters traces.
- `truncated` lets two kinds of text into traces of material that is not `personal-use`, cut, not redacted (nothing
  is removed from inside the text; it is cut at a length):
  - the learner's question and the tutor's answer, truncated to 2,000 characters each, as the root's
    `mlflow.spanInputs` / `mlflow.spanOutputs`;
  - nothing from the CLI: `OTEL_LOG_USER_PROMPTS=1` would also log the system prompt (the rendered book source), and
    the CLI cannot exclude it selectively, so the CLI's content flags stay 0.
- Never in any mode: secrets, handles, tokens, notebook code or output, book source blocks, another user's text.

## Consequences

- Off by default and off for the pilot, so nothing changes until the owner accepts this ADR and a later SRV-OBS
  revision builds it with its own review round.
- With the mode on for non-personal-use material, the chat view and text-based Review queues work for the tutor.
- INV-OBS-1/-2 then become conditional on the setting and the material's licence flag; that revision must keep
  the canary test for the `off` state and add one for the personal-use refusal.
