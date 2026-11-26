---
id: ADR-0009
title: Staged compile pipeline with fact-check, lab verification, adversarial review and two owner gates
status: proposed
date: 2026-11-20
verified_by: "SRV-PIPELINE tests: a lesson with an uncited claim, a failing lab, or an open blocking review finding cannot be published; stages are idempotent by input hash"
revisit_when: "An error is found in a published lesson that a stage should have caught (each such escape reopens the stage that missed it)"
---

## Context

> Amendment proposed by [ADR-0039](0039-knowledge-layers-and-amendment-rules.md) (2026-11-26): stage 1 outputs a base pack and overlays; fact-check uses the fact-check retrieval route; the checks after drafting run cheapest first (rules, lab, claim support, adversarial review last), the first failure loops back, at most 3 loops with every return counted. In force once ADR-0039 is accepted; then, where the two disagree, ADR-0039 wins.

Owner decision, 2026-11-20: the agent does all authoring tasks in staged steps with fact-checking and validation, so that material passes a thorough check.

Evidence from the manual pilot (Chapter 1 lesson, 2026-11-07): the Nakamoto attacker-success table first showed 63.4% for q = 0.3, z = 1; an independent recomputation gave 62.8%. One wrong number in one table, caught only because the value was recomputed by separate code. The pipeline makes that recomputation mandatory instead of lucky.

## Decision

The pipeline is declared as data (`server/src/reader/pipeline/stages.yaml`); the worker interprets it. Each stage has inputs, outputs, a model task class (ADR-0005), a budget, and pass criteria.

| # | Stage | Output | Pass criteria |
| --- | --- | --- | --- |
| 1 | Ingest | Outline, chunks with spans, embeddings | Outline complete per ADR-0008 |
| 2 | Plan | Lessons, order, session estimates, stage grouping | **Gate G1: owner approves the plan** |
| 3 | Draft | Lesson notebook (ADR-0004 kinds) | Notebook and cell schema valid |
| 4 | Fact-check | Every `explain`, `changed`, `erratum` claim has ≥ 1 citation: a source span, or a URL with retrieval date; `changed` cells cite sources newer than the material | 0 uncited claims; contradicted claims fixed or turned into `erratum` |
| 5 | Lab verification | Each lab runs top to bottom in the sandbox twice; expected values are computed by an independent path (official test vectors, a reference library, or a second implementation) | All asserts pass on both runs; outputs identical across runs |
| 6 | Adversarial review | A separate agent with a fresh context, not the drafting conversation, reviews for errors, missing trade-offs and wrong numbers | 0 open blocking findings; non-blocking ones become `open` cells |
| 7 | Publish | Immutable lesson revision | **Gate G2: owner publishes** |

Rules:

- A stage reruns only if its input hash changed; reruns of a published lesson create a new revision and never touch learner-owned cells.
- Findings from stages 4–6 loop back to stage 3 at most 3 times; the fourth failure stops the lesson and asks the owner.
- Every stage records its prompt version, model, cost and duration in the lesson's provenance.

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **Staged pipeline with independent checks and two gates** | Errors caught by mechanisms that do not share the drafter's context | 3–4 agent passes per lesson instead of 1 |
| Single-pass generation with self-review | Cheapest | The reviewer shares the drafter's context and blind spots |
| Human review of every lesson in full | Highest trust | Owner budget: a 40-page chapter lesson is hours of review at 60 min/day |

## Rejected options — why (numbers)

- **Single pass.** The pilot's only measured numeric error (63.4% vs 62.8%) survived a careful single-pass write and was caught only by independent recomputation, which single-pass generation does not include.
- **Full human review.** Bashir chapters average 39 pages (890 pages over 23 chapters, PDF pages 27–916); full review of each lesson would cost several 60-minute sessions per chapter on top of studying it. Gates G1 and G2 keep the owner on decisions, not proofreading.

## Consequences

- Compile cost is roughly 3–4× a single pass; the budget in ADR-0005 is sized for that and measured on L01.
- The same pipeline compiles any material; nothing in it is specific to the Bashir pilot.
