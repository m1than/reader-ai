---
id: ADR-0004
title: Lesson format — nbformat v4 with a reader-ai cell metadata schema
status: proposed
date: 2026-11-20
verified_by: "SRV-LESSON tests: every generated lesson passes nbformat.validate and the reader cell JSON Schema; round-trip read/write is byte-identical"
revisit_when: "A lesson needs a construct that cannot be expressed as a cell plus metadata"
---

## Context

Owner decision, 2026-11-20: one notebook per lesson plus a course index, using the lesson template from the blockchain study. The Chapter 1 lesson in the Claude Docs knowledge base had these parts: diagnostic questions, reading route through the chapter, explanations with derivations, what changed since the book, errata, trade-off tables, labs with self-verifying asserts, notes (konspekt), open questions.

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **Plain `.ipynb`, semantics in `metadata.reader`** | Opens in JupyterLab, VS Code, GitHub; standard tooling (nbformat, nbconvert, papermill) | Metadata must be validated separately |
| MyST Markdown notebooks | Diff-friendly text | Outputs are not stored; conversion step on every run |
| Custom JSON lesson format | Exactly the shape needed | Loses every Jupyter tool; a converter is needed to practise anywhere else |

## Decision

A lesson is a valid nbformat v4 notebook. Every cell carries `metadata.reader` validated by `spec/fixtures/lesson/cell.schema.json` (to be drafted with SRV-LESSON):

| `kind` | Purpose | Key fields |
| --- | --- | --- |
| `diagnostic` | Entry questions that decide the reading route | `questions[]`, `routes{answer: section_ids[]}` |
| `route` | Which source sections to read, in which order | `source_refs[]` (material id, page or span) |
| `explain` | Explanation, derivation, diagram | `claims[]` with `citations[]` |
| `changed` | What changed since the source was written | `as_of`, `citations[]` (URLs, retrieved date) |
| `erratum` | Error in the source | `source_ref`, `correction`, `citations[]` |
| `tradeoff` | Options table with numbers | `options[]`, `rejected_because` |
| `lab` | Practice code | `expected` (hidden, hashed), `asserts[]`, `kernel`, `network: none|allowlist` |
| `recall` | Spaced-repetition card | `front`, `back`, `fsrs` state is kept per user, not in the file |
| `notes` | Learner's konspekt | owned by the learner, never overwritten by the pipeline |
| `open` | Open question | `status`, `answer_ref` |

Every cell also carries `id` (nbformat 4.5 cell ids), `sectors[]` (ADR-0015) and `provenance` (pipeline stage and input hash that produced it).

The course index is `index.ipynb`: plan, lesson status, links. Learner state (progress, recall schedule, notes) lives in Postgres keyed by cell id, so republishing a lesson never destroys a learner's work.

## Rejected options — why (numbers)

- **MyST Markdown.** Stores 0 outputs; every reopen re-executes labs, and pair sessions lose the shared output history.
- **Custom JSON.** Works in 1 tool (reader-ai) instead of at least 4 (JupyterLab, VS Code, GitHub preview, nbconvert).

## Consequences

- `notes` cells and learner state survive republishing: the pipeline writes only cells whose `provenance` it owns.
- The Chapter 1 lesson from Claude Docs is migrated by converting its sections to these kinds.
