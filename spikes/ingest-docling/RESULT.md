# Spike: ingest-docling — RESULT

**Question (docs/roadmap.md 0.3; ADR-0008):** Docling on all 935 pages of the pilot PDF on CPU in Docker: pages/min, peak RAM, fidelity on 20 pages (10 with formulas, 10 with tables), OCR on 10 pages rendered as images (Russian).
**Pass criterion:** ≥ 10 pages/min; ≤ 2 of 20 sampled pages misread.

**Verdict: throughput PASS (29.6 pages/min); fidelity FAIL in every configuration tried.**
- The best configuration (D: formula model on, code model off) misreads **3 of 20** under rule A, where flattening a code block onto one line is not a misread. Under rule B, where flattening always is, it misreads **7 of 20**.
- All remaining misreads are code blocks.
- Formulas need Docling's formula model. At the Docker VM's 4 CPUs that took 689 s for one page; natively on the host it took about 20 s per formula. More VM CPUs were not tried.
- This triggers ADR-0008's `revisit_when` ("misreads more than 2 of 20"). A proposed amendment goes to S0.exit (see "Proposal").

Date: 2026-11-20. Input: the owner's Bashir PDF, 935 pages, sha256 `61e8d4c3…c9e2b2` (matches `pilot/bashir/plan.yaml`). The PDF and all converted text stay outside git (`data/spikes/ingest-docling/`, gitignored); this file and `results/` hold numbers and page numbers only.

Numbers marked *hand-recorded* were observed in a terminal or log that was not kept. Everything else comes from `results/*.json`.

## Environment

| Item | Value |
| --- | --- |
| Host | Apple M5 Max, 18 CPUs, 36 GiB, macOS 27.2 |
| Docker | Docker Desktop 29.9.2, VM 4 CPUs (of 18), 11.6 GiB, no GPU |
| Image | `python:3.14-slim-trixie` (ADR-0020: accepted by the owner in chat on 2026-11-20; the file's `status:` flip is pending), Docling 2.151.1 with EasyOCR, **CPU-only torch**. The default linux/aarch64 torch wheel pulls CUDA libraries; cuDNN alone is 595 MB (*hand-recorded* from the build log). Image 5,499,009,123 bytes (`results/image.json`, from `docker image inspect reader-spike-docling --format '{{.Size}}'`). Models are fetched at build time; runs use `--network none`. Docker runs keep OCR on for bitmap regions (`do_ocr=True`) |
| Native | `uv run --script convert_native.py` on the host, Python 3.14, same Docling, `do_ocr=False`, device AUTO (MPS available), 18 threads. Docling refuses MPS for the formula and code model (`AcceleratorDeviceNotAvailableError: MPS is not supported by this model. Supported devices: cpu, cuda, xpu`), so that model runs on the host CPU |

Commands:
- Docker: `bash spikes/ingest-docling/run.sh <pdf> full|sample|ocr [pages]`. `EXTRA=--no-formulas` runs the sample without the formula model.
- Native: `uv run --script spikes/ingest-docling/convert_native.py <pdf> <pages> <out>`. `CODE=0` turns code enrichment off.
- Peak memory is the maximum of `docker stats`, sampled every 2 s.

## Throughput (Docker, all 935 pages, `results/full.json`)

| Measure | Value |
| --- | --- |
| Pages | 935 in 19 batches of 50 (OCR on bitmap regions, tables on, formula model off) |
| Time | 1,895 s (31.6 min) → **29.6 pages/min** (criterion ≥ 10) |
| Peak memory (sampled) | 5,005 MiB |
| Docling items | 67 tables, 586 pictures, 35 formula items (all `<!-- formula-not-decoded -->` without the formula model) |

## Fidelity (20 pages)

**Sample:**
- pages 128, 130, 132, 133, 141, 143, 378, 381, 461, 468, chosen by math symbols in the text layer;
- pages 483, 497, 559, 592, 602, 661, 672, 700, 776, 812, chosen by "Таблица N.M" captions.

**Judging:** a separate agent judged each page against the rendered PDF page and kept no quotations.
- **OK:** usable for study.
- **MISREAD:** a formula, table, code block or paragraph lost, wrong or out of order in a way that changes meaning.
- **Rule A:** a code block, command output or key flattened onto one line with exact characters is not a misread.
- **Rule B:** such flattening always is.
- Text inside figures (`<!-- image -->`) is expected and not counted.

"Display formulas" below is the judge's count of display formulas on the 20 pages (15). Docling itself detected 6 formula items on those pages, and 35 in the whole book.

| Configuration | Time for 20 pages | Misread (rule A) | Display formulas usable | Tables correct | Code blocks |
| --- | --- | --- | --- | --- | --- |
| A. Docker, no formula model (`results/sample-noformula.json`) | 40.4 s | **8 / 20** | 9 / 15 (0 of the 6 with real math notation) | 11 / 11 | exact text, flattened; 2 pages out of order; 1 lost |
| B. Docker, formula model, page 128 only | 689 s for that one page including model load (*hand-recorded*: container start to the output file's mtime; its result file was not written because the run was stopped) | — | 2 / 2 on that page | — | — |
| C. Native, formula + code model (`results/sample-native.json`) | 580.3 s | **7 / 20** | 15 / 15 | 11 / 11 | line breaks kept, but text re-OCR'd from the image: wrong commands, hashes and numbers on every code page |
| D. Native, formula model, no code model (`results/sample-native-nocode.json`) | 126.2 s | **3 / 20** (rule B: 7 / 20) | 15 / 15 | 11 / 11 | exact text-layer characters, flattened |

**Configuration D, all verdicts (rule A), for re-use as a regression check:**

| Pages | Verdict | Reason |
| --- | --- | --- |
| 128, 130, 132 | OK | display formulas decoded (identical to C) |
| 133, 141 | OK | formulas decoded; code and key blocks exact but flattened |
| 143, 381 | OK | certificate, transaction and log blocks exact but flattened |
| 378 | MISREAD | a log block containing emoji glyphs is classified as an image and lost |
| 461 | MISREAD | a code block sits after the wrong list step. Flattened JavaScript: `//` comments swallow the following code |
| 468 | MISREAD | all four code blocks are detached from their list steps; a spurious item "3." |
| 483–812 (10 table pages) | OK | 11 tables, 0 wrong cells; soft hyphens remain inside words |

**Failure patterns:**
1. **Display formulas** need the formula model (pages 128, 130, 132, 133, 141). With it, all 15 are correct LaTeX; multi-line blocks use `&` without an aligned environment.
2. **Code enrichment corrupts code** (C), because it re-reads code from the page image instead of the text layer. The errors look plausible: misspelled commands, altered hashes, a value missing three zeros, Cyrillic noise inside blocks. D keeps it off.
3. **Code block structure** is wrong without it: line breaks are lost (rule B), blocks move out of their numbered-list steps (461, 468), and one emoji-containing block is dropped (378).
4. **Minor and widespread:** lost sub- and superscripts in running text ("F 23", "s -1"), soft hyphens inside words, `&lt;` entities, doubled list markers.

## Formula cost

| Where | Observation |
| --- | --- |
| Docker, 4 VM CPUs, `do_ocr=True` | page 128 with 2 formulas: 689 s including model load (B, one *hand-recorded* observation) |
| Native, host CPU (18 threads), `do_ocr=False` | D's five formula pages: 118.0 s for 6 formulas, about 19.7 s per formula. Page 128 alone: 50.7 s in D, 38.6 s in C, and 41.5 s in a first probe (*hand-recorded*) |
| Model load, native | 107 s on first use, including download and warm-up (*hand-recorded*); 11.6 s and 5.2 s afterwards (C, D) |
| Book estimate | 35 formulas × about 19.7 s ≈ 11 min natively. Docker at 4 CPUs: not estimated beyond the single page-128 observation |

Native was about 17 times faster on page 128 (344 s per formula from B against 19.7 s). The runs differ in CPU count (18 against 4), OCR setting and environment. The share of each factor was not measured, and raising the Docker VM's CPU count was not tried.

### M1 (ADR-0021): Docker at 4 VM CPUs, `do_ocr=False`, formula model on

`bash spikes/ingest-docling/run.sh <pdf> m1 128,130,132,133,141` → `results/m1.json` (2026-11-20):

| Page | Formulas decoded | Seconds |
| --- | --- | --- |
| 128 | 2 | 643.4 |
| 130 | 1 | 214.7 |
| 132 | 1 | 218.4 |
| 133 | 1 | 402.6 |
| 141 | 1 | 225.4 |
| **Total** | **6** (0 placeholders) | **1,704.5 s → 284.1 s per formula** |

- Model load: 3.3 s (separate from the per-page times).
- Peak memory (sampled): 1,852 MiB.
- **Against ADR-0021's bound of ≤ 51 s per formula, this is 5.6 times over: M1 at 4 CPUs fails.**
- Turning OCR off did not close the gap (B, with OCR on, was ≈ 344 s per formula including model load), so the difference from native (19.7 s) comes mainly from the environment and CPU count.
- Light containers from other spikes ran on the same VM for part of the run, so these are upper bounds.
- Per ADR-0021, the next step is a second M1 run after the owner raises Docker Desktop's CPU count, before native is chosen.

## OCR (10 pages rendered as images, Russian + English)

**Method:** each page is rendered at 200 dpi with no text layer and converted with full-page EasyOCR (`ru`, `en`) in Docker.
- **Character accuracy:** 1 − normalized Levenshtein distance against the PDF's text layer, whitespace-normalized (`results/ocr.json`).
- **Word recall and precision:** multiset overlap of lowercased word tokens (`results/ocr-words.json`, generated by `uv run --script spikes/ingest-docling/ocr_words.py <pdf> data/spikes/ingest-docling/ocr`).
- **Target for SRV-INGEST:** rule B. Restoring line breaks from the text layer makes flattening avoidable.

| Page | Words (text layer → OCR) | Word recall | Word precision | Character accuracy | OCR time |
| --- | --- | --- | --- | --- | --- |
| 30 | 339 → 322 | 0.900 | 0.947 | 0.963 | 46.4 s |
| 95 | 224 → 142 | **0.607** | 0.958 | **0.678** | 36.4 s |
| 170 | 325 → 314 | 0.892 | 0.924 | 0.970 | 40.7 s |
| 260 | 329 → 316 | 0.872 | 0.908 | 0.952 | 34.4 s |
| 320 | 377 → 352 | 0.867 | 0.929 | 0.956 | 53.8 s |
| 420 | 240 → 226 | 0.875 | 0.929 | 0.853 | 67.5 s |
| 520 | 235 → 219 | 0.855 | 0.918 | 0.886 | 37.3 s |
| 600 | 331 → 311 | 0.876 | 0.932 | 0.953 | 35.4 s |
| 700 | 234 → 192 | **0.752** | 0.917 | **0.709** | 34.7 s |
| 850 | 215 → 205 | 0.842 | 0.883 | 0.779 | 28.8 s |
| **All** | | 0.607–0.900 | 0.883–0.958 | mean **0.870**, min 0.678 | 415.4 s in total, **1.44 pages/min** |

**Readings:**
- **Low scores are missing content, not reordering.** On page 95 OCR returns 142 of 224 words: precision stays high (0.958), recall falls to 0.607. The page holds two labelled diagrams and display formulas, and `export_to_text` drops text in picture regions, while the text layer keeps it. Page 700 (a table) loses a quarter of its words.
- **On clean text pages,** character accuracy is 0.95–0.97, but word recall is only 0.87–0.90. About 1 word in 9 is misspelled or missing, which matters for full-text search on scanned material.
- **OCR is about 20 times slower** than text-layer extraction (1.44 against 29.6 pages/min). The pilot PDF has a text layer on all but 6 pages (1–4, 392, 935; under 200 characters), so OCR matters for other materials, not for this book.
- **Peak memory** was 5,642 MiB (sampled).
- **Overlap:** the OCR run overlapped with the native run D on the same machine, so OCR times are an upper bound.

## Proposal (for S0.exit; ADR-0008 amendment, owner's decision)

- **Run ingestion natively on the Mac**, not in Docker at 4 VM CPUs. This is the ADR-0008 fallback, measured here:
  - the formula model ran at about 20 s per formula natively, against 689 s for a 2-formula page in Docker;
  - MPS is not usable for that model;
  - giving the Docker VM more CPUs is an untested alternative worth one run first.
- **Formula model on, code model off** (configuration D).
- **Code blocks from the PDF text layer:** take each code item's bounding box and read its lines from the text layer, keeping line breaks. Not measured. It addresses rule B's flattening; the list-order failures (461, 468) and the dropped emoji block (378) need their own handling in SRV-INGEST, with a fidelity test.
- **The fidelity check becomes a regression test:** the 20 page numbers, the rubric with rules A and B, and the per-page verdicts above, re-run whenever the ingest pipeline changes.

## Hands to stage 3 (SRV-INGEST)

- **Throughput is not the constraint:** 29.6 pages/min in Docker; the book takes about 32 min. A native full run (time, peak memory) was not measured. The estimate of about 32 + 11 min with formulas is Docker throughput plus the native formula estimate.
- **Tables:** 11 of 11 correct as Docling emits them.
- **Formulas:** use the formula model, natively.
- **Code:** read from the text layer, not from Docling's code model.
- **Figures:** text inside them is lost (`<!-- image -->`), both from the text-layer path and from OCR. Lessons that need figure text need OCR of the figure region (not measured).
