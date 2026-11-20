---
id: ADR-0021
title: Ingestion amendment — formula model on, code from the PDF text layer, a deterministic page-expectation check; Docker or native decided by one measurement
status: proposed
date: 2026-11-20
verified_by: "SRV-INGEST local check on the owner's Mac against committed per-page expectations for the 20 pilot pages (counts and hashes only, no book text, expected hashes computed independently of the pipeline): code-block count, lines per block, sha256 of each block's text equal to the committed sha256 of the text layer inside its box, block order relative to list items (block index before or after a list-item index; pages 461, 468), decoded-formula count, table shape (rows × columns). It runs only where the sha256-matched PDF is present; a missing PDF is reported as a skip, never as a pass. Plus measurement M1 deciding Docker or native, and, if native, the confinement check in Consequences"
revisit_when: "After SRV-INGEST is green: the owner's fidelity live check misreads more than 2 of the 20 pages (rule B); or a native or Docker full run with the formula model falls below 10 pages/min; or Docling's formula model gains MPS support"
supersedes: []
---

## Context

ADR-0008 chose Docling for content, with `revisit_when`: "misreads more than 2 of 20 sampled pages with formulas or tables". `spikes/ingest-docling` triggered it on 2026-11-20 (`spikes/ingest-docling/RESULT.md` on main, PR #13; deep-reviewer approve, round 2).

The roadmap names the fallback "run the ingestion worker natively on macOS with the Apple GPU (MPS)" (docs/roadmap.md:135, the spike table, and :359, the risk table). Its premise changed: Docling refuses MPS for the formula and code model ("Supported devices: cpu, cuda, xpu").

The spike's configurations are lettered A–D in its RESULT.md. This ADR uses **S-A … S-D** for them, and **O1–O3** for its own options.

| Fact | Number | Source |
| --- | --- | --- |
| Throughput, Docker at 4 VM CPUs, formula model off, `do_ocr=True` | 935 pages in 1,895 s = 29.6 pages/min; peak 5,005 MiB (sampled) | `results/full.json` |
| Formula items in the whole book | 35 (*hand-counted* with grep over the full run's `formula-not-decoded` placeholders) | RESULT.md "Throughput" |
| S-A: Docker, formula model off | 8 of 20 misread (rule A); 0 of 6 display formulas with real notation survive | `results/sample-noformula.json` |
| S-C: native, formula + code models | 7 of 20; code text re-OCR'd from the image and corrupted on every code page | `results/sample-native.json` |
| S-D: native, formula model on, code model off | 3 of 20 (rule A) / 7 of 20 (rule B); tables 11/11; display formulas 15/15 | `results/sample-native-nocode.json`; per-page table in RESULT.md |
| Formula cost | Docker at 4 VM CPUs with `do_ocr=True`: 689 s for one page with 2 formulas, including model load (one *hand-recorded* observation). Native host CPU, `do_ocr=False`: 118.0 s for 6 formulas, ≈ 19.7 s each | RESULT.md "Formula cost" |
| OCR, 10 rendered pages | character accuracy mean 0.870 (0.678–0.970); word recall 0.607–0.900; 1.44 pages/min; figure text lost | `results/ocr.json`, `results/ocr-words.json` |

**Rules:** rule A says a code block flattened onto one line with exact characters is not a misread; rule B says it always is.

**S-D's remaining misreads** are all code blocks:
- page 378: a code block containing emoji glyphs is classified as an image and dropped;
- pages 461 and 468: code blocks are detached from their numbered-list steps.

## Options

| Option | Gives | Costs (numbers) |
| --- | --- | --- |
| **O1. Docker, formula model on, code model off, `do_ocr=False` for born-digital pages** | Keeps ingestion inside the container boundary (ADR-0014 isolation for parsing untrusted files) | Formula cost in Docker unmeasured with `do_ocr=False`; one page took 689 s with `do_ocr=True` at 4 VM CPUs |
| **O2. Native host process, same pipeline** | ≈ 19.7 s per formula measured; the book's 35 formulas ≈ 11.5 min | Parses untrusted documents outside a container; an ADR-0012 knock-on (below) |
| O3. Formula model off | Simplest; 29.6 pages/min | 8 of 20 misread; all real-notation formulas lost |

In O1 and O2 alike, code lines come from the PDF text layer inside Docling's code-item boxes, and Docling's code model stays off.

## Decision

1. **Content pipeline** (amends ADR-0008 Decision item 2):
   - The formula model is **on**, and the code model is **off**.
   - Code blocks: their lines are read from the PDF text layer inside Docling's bounding boxes, keeping line breaks and order.
   - Born-digital pages run with `do_ocr=False`; OCR (EasyOCR, `ru` + `en`) is the fallback only for pages without a text layer.
2. **Where it runs, decided by measurement M1:**
   - **M1 runs** S-D's five formula pages (128, 130, 132, 133, 141) in Docker with `do_ocr=False` and the formula model on, first at 4 VM CPUs.
   - M1 records seconds per formula **excluding model load**, the same basis as the native 19.7 s, and records peak memory.
   - **Bound:** the book's 35 formulas must take **≤ 30 min** in total, i.e. **≤ 51 s per formula**.
     - The owner agreed to this bound (2026-11-20, in chat).
     - Reasoning: the book takes 31.6 min without formulas in Docker, so with the bound a whole course ingest stays near one hour.
     - It is a choice, not a number from ADR-0010, which gives the owner 60 minutes of study a day.
   - **Rule:**
     - if M1 at 4 VM CPUs is within the bound, ingestion stays in Docker (O1);
     - if it misses, the owner raises Docker Desktop's CPU count and M1 runs again (decided by the owner, 2026-11-20);
     - only if that second run also misses does ingestion run natively (O2).
3. **If native (O2), the ADR-0012 knock-on:**
   - The native converter is a host process. It receives one PDF and returns the converted document over `127.0.0.1`, with no database access and no blob volume.
   - The worker in Compose keeps the jobs, the spans and the storage.
   - Requirement: **no network, models pre-fetched**.
   - **Confinement:** the converter runs as a dedicated macOS user or under a sandbox profile. It reads only its input and the models, writes only its output, has no secrets in its environment, and listens on loopback only.
   - Note for the spec: any local process can submit to a loopback listener, so SRV-INGEST must authenticate the worker's requests or accept that local processes can use the converter.
4. **Checks:**
   - The **local check on the owner's Mac** is the deterministic one in `verified_by`: committed per-page expectations, counts and hashes only, no book text. It needs the sha256-matched PDF, which never leaves the owner's Mac.
   - The agent-judged fidelity, with rules A and B and the 20 pages, becomes an **owner live check** with a judge and a budget per run, outside CI. Its target is rule B, at most 2 of 20.

## Rejected options — why (numbers)

- **O3 (formula model off).** 8 of 20 misread, and 0 of 6 display formulas with real notation survive.
- **The code model on (S-C).** 7 of 20 misread, with plausible-looking corruption of commands, hashes and numbers; flattened exact text (S-D) is safer.
- **O1 versus O2 is not decided here.** M1 decides it. The 17-fold native speed-up on page 128 mixes CPU count (18 against 4), the OCR setting and the environment, and their shares were not separated.

## Consequences

**Hypotheses, each with its check:**
- **Text-layer code lines** fix rule-B flattening on pages 133, 141, 143 and 381: measured by the deterministic check (lines per block, sha256 equal to the text layer).
- **Re-attaching code blocks to list steps** fixes pages 461 and 468: the deterministic check's expected block order (block index before or after a list-item index).
- **Page 378:** there is no mechanism hypothesis yet. The block with emoji glyphs is classified as a picture by Docling's layout model, and why is not known. SRV-INGEST must investigate; until then it is a known miss.
- **Native full run:** if O2 is chosen, a full-book run with the formula model must reach **≥ 10 pages/min**, with peak memory recorded.
- **Confinement of the native converter:** macOS cannot easily deny a process network access, so enforcement is a hypothesis. The check runs the converter as its dedicated user or under its sandbox profile and verifies that it:
  - converts the 20 pages;
  - fails any outbound connection attempt;
  - cannot read outside its input and model directories;
  - cannot write outside its output directory;
  - has no secret in its environment;
  - listens on loopback only.

**Other consequences:**
- OCR's word recall of 0.87–0.90 on clean pages is a known limit for full-text search on scanned material. Figure-region text is not recovered by either path.
- **ADR-0020's OCR row is closed:** Docling with EasyOCR runs on `python:3.14-slim-trixie` (the S-A sample and the 10-page OCR run, `results/ocr.json`).

## Open questions

- None. The owner answered both questions on 2026-11-20 (recorded in the Decision):
  - the 30-minute bound is agreed;
  - a second M1 run with more VM CPUs comes before native is chosen.
