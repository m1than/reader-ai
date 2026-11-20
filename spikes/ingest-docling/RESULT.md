# Spike: ingest-docling — RESULT

**Question (docs/roadmap.md 0.3; ADR-0008):** Docling on all 935 pages of the pilot PDF on CPU in Docker: pages/min, peak RAM, fidelity on 20 pages (10 with formulas, 10 with tables), OCR on 10 pages rendered as images (Russian).
**Pass criterion:** ≥ 10 pages/min; ≤ 2 of 20 sampled pages misread.
**Verdict: throughput PASS (29.6 pages/min); fidelity FAIL in every configuration tried (best: 3 of 20).** All remaining misreads are code blocks. Formulas need the formula model, which is only practical **natively on the Mac**, not in Docker. This triggers ADR-0008's `revisit_when` ("misreads more than 2 of 20"). A proposed amendment goes to S0.exit (see "Proposal").

Date: 2026-11-20. Input: the owner's Bashir PDF, 935 pages, sha256 `61e8d4c3…c9e2b2` (matches `pilot/bashir/plan.yaml`). The PDF and all converted text stay outside git (`data/spikes/ingest-docling/`, gitignored); this file holds numbers and page numbers only.

## Environment

| Item | Value |
| --- | --- |
| Host | Apple M5 Max, 18 CPUs, 36 GiB, macOS 27.2 |
| Docker | Docker Desktop 29.9.2, VM 4 CPUs, 11.6 GiB, no GPU |
| Image | `python:3.14-slim-trixie` (proposed ADR-0020), Docling 2.151.1 with EasyOCR, **CPU-only torch** (the default linux/aarch64 torch wheel pulls CUDA libraries, cuDNN alone 595 MB). Image 5,499,009,123 bytes. Models fetched at build time; runs use `--network none` |
| Native | `uv run --script convert_native.py` on the host, Python 3.14, same Docling; device AUTO (MPS available). Docling refuses MPS for the formula/code model (`AcceleratorDeviceNotAvailableError: MPS is not supported by this model. Supported devices: cpu, cuda, xpu`), so that model runs on the host CPU with 18 threads |

Commands: `bash spikes/ingest-docling/run.sh <pdf> full|sample|ocr [pages]` (Docker, `EXTRA=--no-formulas` for the sample without the formula model); `uv run --script spikes/ingest-docling/convert_native.py <pdf> <pages> <out>` (native; `CODE=0` turns code enrichment off).

## Throughput (Docker, CPU, all 935 pages, `results/full.json`)

| Measure | Value |
| --- | --- |
| Pages | 935 in 19 batches of 50 (OCR on bitmap regions only, tables on, formula model off) |
| Time | 1,895 s (31.6 min) → **29.6 pages/min** (criterion ≥ 10) |
| Peak memory | 5,005 MiB |
| Found | 67 tables, 586 pictures, **35 display formulas** (all `<!-- formula-not-decoded -->` without the formula model) |

## Fidelity (20 pages)

Sample: pages 128, 130, 132, 133, 141, 143, 378, 381, 461, 468 (chosen by math symbols in the text layer) and 483, 497, 559, 592, 602, 661, 672, 700, 776, 812 (chosen by "Таблица N.M" captions). Each page was judged against the rendered PDF page by a separate agent (no quotations kept), with the rubric: OK = usable for study, MISREAD = a formula, table, code block or paragraph lost or wrong in a way that changes meaning. Text inside figures (`<!-- image -->`) is expected and not counted.

| Configuration | Time for 20 pages | Misread | Display formulas usable | Tables correct | Code blocks |
| --- | --- | --- | --- | --- | --- |
| A. Docker, no formula model (`results/sample-noformula.json`) | 39 s | **8 / 20** | 9 / 15 (0 of 6 with real math notation) | 11 / 11 | exact text, flattened to one line; 2 pages out of order; 1 lost |
| B. Docker, formula model (page 128 only) | 689 s for one page, incl. model load | — | 2 / 2 on that page | — | — |
| C. Native, formula + code model (`results/sample-native.json`) | 580 s | **7 / 20** | 15 / 15 | 11 / 11 | line breaks kept, but text re-OCR'd from the image: wrong commands, hashes and numbers on every code page |
| D. Native, formula model, no code model (`results/sample-native-nocode.json`) | 126 s | **3 / 20** (derived, see below) | 15 / 15 | 11 / 11 | as A: exact text, flattened; 378, 461, 468 still wrong |

D was not judged separately. Its pages are byte-identical to A on the 10 table pages, its formula lines are identical to C (where all 15 were judged usable), and its code pages differ from A only in the formula lines. So D's misreads are A's minus the pages that failed only on formulas (128, 130, 132, 133, 141): **378** (a log block with emoji glyphs classified as an image and dropped), **461** and **468** (code blocks pulled out of their numbered-list steps; 461's JavaScript flattened so `//` comments swallow the following code).

Failure patterns, with pages:
1. **Display formulas** need the formula model (128, 130, 132, 133, 141). With it, 15/15 are correct LaTeX; multi-line blocks use `&` without an aligned environment.
2. **Code enrichment corrupts code**, because it re-reads code from the page image instead of the text layer (133, 141, 143, 378, 381, 461, 468 in C). The errors are plausible-looking: misspelled commands, altered hashes, a value missing three zeros, Cyrillic noise pulled into blocks. Configuration D therefore keeps it off.
3. **Code blocks lose line breaks and their place in numbered lists** without it (461, 468), and one emoji block is dropped (378).
4. Minor and widespread: lost sub/superscripts in running text ("F 23", "s -1"), soft hyphens left inside words, `&lt;` entities, doubled list markers.

## Formula cost

| Where | Cost |
| --- | --- |
| Docker, CPU (4 VM CPUs) | page 128 with 2 formulas: 689 s incl. model load (≈ 5 min per formula) → the book's 35 formulas ≈ 3 h (estimate) |
| Native, host CPU (18 threads) | page 128: 41.5 s and 38.6 s in two runs; D: 126 s for 20 pages with 6 formulas, plain pages 0.1–1 s → ≈ 19 s per formula → the book's 35 formulas ≈ 11 min (estimate) |
| Model load, native | 107 s first time (download and warm-up), 5–12 s afterwards |

## OCR (10 pages rendered as images, Russian + English)

Each page is rendered at 200 dpi with no text layer, converted with full-page EasyOCR (`ru`, `en`) in Docker, and scored as 1 − normalized Levenshtein distance against the PDF's own text layer, whitespace-normalized (`results/ocr.json`).

| Page | Characters (text layer) | OCR time | Character accuracy |
| --- | --- | --- | --- |
| 30 | 2,718 | 46.4 s | 0.963 |
| 95 | 1,312 | 36.4 s | **0.678** |
| 170 | 2,353 | 40.7 s | 0.970 |
| 260 | 2,496 | 34.4 s | 0.952 |
| 320 | 2,682 | 53.8 s | 0.956 |
| 420 | 1,869 | 67.5 s | 0.853 |
| 520 | 1,874 | 37.3 s | 0.886 |
| 600 | 2,611 | 35.4 s | 0.953 |
| 700 | 1,665 | 34.7 s | **0.709** |
| 850 | 1,621 | 28.8 s | 0.779 |
| **All** | | 28.8–67.5 s per page (≈ 1.3 pages/min) | mean **0.870**, min 0.678 |

- Text-only pages reach 0.95–0.97.
- The low scores are on pages with less running text: 95, 700 and 850 have 1,312–1,665 characters, against 2,353–2,718 for the high pages. Page 700 holds a table.
- Likely cause, *not verified*: the metric punishes reading order. Text-layer order through tables, figure labels and code differs from OCR order even when every word is read, so these scores are a lower bound for such pages. A word-level or order-insensitive score would separate misreads from reordering.
- OCR is about 20 times slower than text-layer extraction, but the pilot PDF has a text layer on all but 6 pages (1–4, 392, 935; under 200 characters). So OCR matters for other materials, not for this book.
- Peak memory 5,642 MiB.
- The 10 OCR pages and the native sample D overlapped in time on the same machine for part of the run, so OCR times are an upper bound.

## Proposal (for S0.exit; ADR-0008 amendment, owner's decision)

- **Run ingestion natively on the Mac**, not in Docker: the formula model is about 15 times faster on the host CPU, and MPS speeds up layout. ADR-0008 already names "run the ingestion worker natively on macOS with the Apple GPU (MPS)" as the fallback. Measured: MPS is not usable for the formula model itself; the gain comes from host CPU threads.
- **Formula model on, code model off** (configuration D).
- **Code blocks from the PDF text layer**: take each code item's bounding box and read its lines from the text layer, keeping line breaks and order. Not measured here; it targets the three remaining misreads (378, 461, 468) and is a task for SRV-INGEST with its own fidelity test.
- **Fidelity criterion as a regression test**: the 20 pages and the rubric above become a fixture-free check (page numbers + verdicts), re-run whenever the ingest pipeline changes.

## Hands to stage 3 (SRV-INGEST)

- Throughput is not the constraint: 29.6 pages/min in Docker, the book in about 32 min; natively with formulas an estimated 45 min including the 35 formulas.
- Tables: 11 of 11 correct as Docling emits them.
- Formulas: formula model, natively. Code: text layer, not Docling's code model. Figures: text inside them is lost (`<!-- image -->`); figure text needs OCR of the figure region if lessons need it (not measured).
- Memory: 5.0 GiB peak in Docker for the full run.
