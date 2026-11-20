---
id: ADR-0021
title: Ingestion amendment — native Docling with the formula model, code from the PDF text layer, a 20-page fidelity regression check
status: proposed
date: 2026-11-20
verified_by: "SRV-INGEST fidelity regression on the 20 pilot pages of spikes/ingest-docling (rule B, at most 2 of 20 misread); a native full-book run recording time and peak memory"
revisit_when: "Docling's formula or code model gains MPS support; the regression check misreads more than 2 of 20 pages; a material without a text layer becomes the main input"
supersedes: []
---

## Context

ADR-0008 chose Docling for content. Its `revisit_when` says: "misreads more than 2 of 20 sampled pages with formulas or tables". `spikes/ingest-docling` (2026-11-20, RESULT.md on branch `0/S0.spike.ingest-docling`, PR #13) triggered it.

Measured on the 935-page pilot PDF:

| Fact | Number | Source |
| --- | --- | --- |
| Throughput, Docker, 4 VM CPUs, formula model off | 935 pages in 1,895 s = 29.6 pages/min; peak 5,005 MiB (sampled) | `results/full.json` |
| Formula items detected in the whole book | 35 | grep of the full output, RESULT.md |
| Fidelity, 20 pages, Docker, no formula model | 8 of 20 misread (rule A) | `results/sample-noformula.json`, RESULT.md |
| Fidelity, native, formula + code models | 7 of 20; code text re-OCR'd from the image and corrupted on every code page | `results/sample-native.json` |
| Fidelity, native, formula model on, code model off (D) | 3 of 20 under rule A, 7 of 20 under rule B; tables 11/11; display formulas 15/15 | `results/sample-native-nocode.json`, per-page table in RESULT.md |
| Formula model cost | Docker at 4 VM CPUs: 689 s for one page with 2 formulas, including model load (one hand-recorded observation). Native, host CPU: 118.0 s for 6 formulas, about 19.7 s each | RESULT.md "Formula cost" |
| MPS | Docling refuses MPS for the formula and code model ("Supported devices: cpu, cuda, xpu") | RESULT.md "Environment" |
| OCR, 10 pages rendered as images | character accuracy mean 0.870 (0.678–0.970); word recall 0.607–0.900; 1.44 pages/min; text inside figures is lost | `results/ocr.json`, `results/ocr-words.json` |

Rule A: a code block, command output or key flattened onto one line with exact characters is not a misread. Rule B: it always is. The remaining D misreads (pages 378, 461, 468) are all code blocks: one dropped as an image, two detached from their numbered-list steps.

## Options

| Option | Gives | Costs (numbers) |
| --- | --- | --- |
| **A. Native ingest, formula model on, code model off, code lines from the text layer** | 15/15 formulas; tables 11/11; exact code characters with line breaks restored (hypothesis) | Ingest runs outside Docker, on the host; the formula model costs about 19.7 s per formula (≈ 11 min for the book's 35) |
| B. Docker with a larger VM, same pipeline | Keeps ingest in a container (ADR-0014 isolation for parsing) | Not measured; one page with 2 formulas took 689 s at 4 VM CPUs |
| C. Docker as measured, formula model off | Simple; 29.6 pages/min | 8/20 misread; every display formula with real notation lost (0 of 6) |
| D. Docling's code model on | Line breaks kept | 7/20 misread; plausible-looking corruption of commands, hashes and numbers |

## Decision

This amends ADR-0008 "Decision" item 2 (Content), adding a rule:
- **Formulas:** Docling runs with the formula model on. Ingestion runs natively on the owner's Mac (host CPU) unless a measured run shows a Docker VM size that brings the formula cost within 2 times the native cost (option B, one measurement first).
- **Code:** Docling's code model stays off. Code items keep Docling's bounding boxes, and their lines are read from the PDF text layer inside those boxes, restoring line breaks and list position.
- **Regression check:** the 20 pilot pages and their per-page verdicts in `spikes/ingest-docling/RESULT.md` become SRV-INGEST's fidelity regression test, with rule B as the target (at most 2 of 20 misread). It runs on every change to the ingest pipeline.
- **OCR:** for pages without a text layer, OCR (EasyOCR, `ru` + `en`) is the fallback. Its word recall of 0.87–0.90 on clean pages is recorded as a known limit for full-text search, and figure-region text is not recovered.

## Rejected options — why (numbers)

- **C (formula model off).** 8 of 20 misread; 0 of 6 display formulas with real math notation survive.
- **D (code model on).** 7 of 20 misread; corrupted code is worse than flattened exact code, because errors look plausible (a value missing three zeros, altered hashes).
- **B (larger Docker VM) is not rejected.** It is unmeasured, and one run with more VM CPUs decides between A and B. The 17-fold native speed-up on page 128 mixes CPU count (18 against 4), the OCR setting and the environment; their shares were not separated.

## Consequences

- **Hypotheses, each with the check that measures it:**
  - Code lines from the text layer fix rule-B flattening on pages 133, 141, 143 and 381: measured by the regression check.
  - Re-attaching code blocks to their list steps fixes pages 461 and 468: the regression check.
  - Detecting emoji-bearing code blocks fixes page 378: the regression check.
  - Native full-book time and peak memory: one native full run in SRV-INGEST.
- **Ingestion now runs on the host.** It parses untrusted documents outside the container boundary of ADR-0014, so OPS-SANDBOX and SRV-INGEST must state what limits the native worker gets: at least a separate user, no secrets in its environment, and read-only input.
- **Throughput is not a constraint:** 29.6 pages/min in Docker; the book plus formulas is estimated at about 43 min (Docker throughput plus the native formula estimate; not a measured native run).

## Open questions

- Should a native ingest worker run under a dedicated macOS user, or inside a macOS sandbox profile?
- Is one measured run with the Docker VM at more CPUs (option B) wanted before SRV-INGEST is specified?
