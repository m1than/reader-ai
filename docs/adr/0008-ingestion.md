---
id: ADR-0008
title: Ingestion — outline first, Docling for content, source spans for every chunk
status: proposed
date: 2026-11-20
verified_by: "VEC fixture: Bashir PDF bookmark outline (494 entries, 23 chapters) reproduced exactly; spikes/ingest-docling RESULT.md with throughput and fidelity on the pilot"
revisit_when: "Docling processes the 935-page pilot slower than 10 pages per minute on the owner's machine, or misreads more than 2 of 20 sampled pages with formulas or tables"
---

> Proposed amendment: [ADR-0021](0021-ingestion-formulas-text-layer-code.md) (2026-11-20) — `revisit_when` was triggered. The amendment: formula model on, code from the PDF text layer, a deterministic page-expectation check; Docker or native decided by measurement M1.

## Context

> Amendment proposed by [ADR-0039](0039-knowledge-layers-and-amendment-rules.md) (2026-11-26): ingest output is an immutable base pack whose spans take this ADR's locator (page, character offsets) as identity plus a quote hash, and stored citations migrate to it; context prefixes and tags are versioned overlays. In force once ADR-0039 is accepted; then, where the two disagree, ADR-0039 wins.

Owner decision, 2026-11-20: any material can be loaded; the user sets the limits.

Measurement on the pilot PDF (2026-11-20): parsing the printed table of contents as text lost 3 of 23 chapters (8, 20, 23), because their titles wrap to a second line. The PDF bookmark outline had all 23 chapters, 494 entries on three levels. Chapter 23 has no bookmarked sections, so its structure must come from the content itself.

## Decision

1. **Structure first.** The course plan is built from the most reliable structure the format declares, in this order: PDF bookmarks → EPUB navigation document → DOCX heading styles → HTML/Markdown headings → layout-detected headings → agent-proposed outline. Whatever the source, the outline passes owner gate G1 (ADR-0009).
2. **Content.** Docling converts PDF, DOCX, PPTX, HTML and images into a structured document (text, tables, formulas, figures with bounding boxes). EPUB is read directly (zip container, OPF manifest, XHTML). Git repositories and web pages are fetched by URL into a snapshot blob.
3. **Spans.** Every chunk keeps its source span: material sha256, page (or file and line range), and character offsets. Citations in lessons point to spans, so a fact-check can show the exact source text.
4. **Limits are declared per material** in `reader.yaml`: page ranges, max size, allowed fetch domains, language, license note (`personal-use`, `shareable`). `personal-use` material is never exposed through sharing or MCP (ADR-0015, ADR-0016).

## Options

| Option | License | Gives | Costs |
| --- | --- | --- | --- |
| **Docling** | MIT | Many formats, layout model, tables and formulas | Heavier (ML models); speed to be measured |
| PyMuPDF4LLM | AGPL-3.0 | Fast on born-digital PDFs | AGPL obligations once reader-ai is served to partners over a network |
| Marker | GPL-3.0 code, restricted model weights license | Strong PDF → Markdown | Copyleft plus usage restrictions on weights |
| Text extraction only (pypdf) | BSD | Tiny, fast | No tables, formulas or layout; the ToC parse lost 13% of chapters |

## Rejected options — why (numbers)

- **PyMuPDF4LLM / Marker.** reader-ai is served over a network to study partners (ADR-0016); copyleft network clauses would apply to the whole service. MIT Docling carries no such obligation.
- **Text only.** Lost 3 of 23 chapters (13%) on the pilot's ToC alone; formulas in chapters 3–5 (hashes, ECC, consensus probabilities) need layout-aware extraction. pypdf is still used, only for bookmarks.

## Spike

`spikes/ingest-docling`: run Docling on all 935 pages; record pages per minute, peak memory, and fidelity on 20 sampled pages (10 with formulas, 10 with tables) against the PDF. The numbers go into SRV-INGEST NFRs.

## Resolved questions (owner, 2026-11-20)

1. OCR is in v1. Pages without a text layer go through Docling's OCR pipeline; the engine and its Russian accuracy are measured in `spikes/ingest-docling` on 10 rendered-as-image pages of the pilot.
2. Web fetch defaults (agent's choice, configurable per material): same domain only, link depth 2, at most 200 pages, `robots.txt` respected.

Note for Mac-only v1 (ADR-0012): Docker containers on macOS have no access to the Apple GPU, so Docling's layout and OCR models run on CPU. The spike measures that path.
