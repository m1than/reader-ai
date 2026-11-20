"""Prompts for the compile-cost spike. Written for the spike (not shipped); English instructions, Russian output
(the course language of the Bashir pilot). Stage order and pass criteria follow ADR-0009; cell kinds follow ADR-0004."""

CELL_KINDS = """Cell kinds (ADR-0004), JSON objects with a "kind" field:
- diagnostic: entry questions that decide the reading route. Fields: questions[], routes{answer: section_ids[]}
- route: which source sections to read, in which order. Fields: source_refs[] ({"pdf_page": N})
- explain: explanation or derivation. Fields: title, body (markdown), claims[] ({"text", "citations": [{"pdf_page": N}]})
- tradeoff: options table with numbers. Fields: options[], rejected_because
- erratum: an error in the source. Fields: source_ref, correction, citations[]
- recall: spaced-repetition card. Fields: front, back
- open: open question. Fields: status, text
Do not emit lab, changed or notes cells for this lesson."""

DRAFT = """You are the drafting stage of a course compiler (ADR-0009 stage 3).
Write ONE lesson notebook for lesson {lesson_id} "{title}" from the source chapter below, for a learner who studies
60 minutes a day and will build a crypto wallet later in the course. Sections of the chapter: {sections}.

{kinds}

Requirements:
- Language of all learner-facing text: Russian.
- Start with one diagnostic cell, then a route cell over the sections, then explain cells (one or more per section),
  tradeoff cells where the source compares options, recall cards (8-15) at the end.
- Every claim in an explain cell has at least one citation to a PDF page of the source ("pdf_page" as marked below).
- Do not invent facts that are not in the source; if the source is outdated or wrong, add an erratum or open cell.
Return ONLY a JSON object {{"lesson_id": ..., "cells": [...]}} with no prose around it.

SOURCE (copyrighted material, for this task only; pages are marked "=== PDF page N ==="):
{source}"""

FACT_CHECK = """You are the fact-check stage of a course compiler (ADR-0009 stage 4). Check the lesson notebook against
the source chapter. For EVERY claim in explain, tradeoff and erratum cells decide: supported (the cited page says it),
miscited (true, but on another page; give the right page), unsupported (not in the source), or contradicted (the source
says otherwise). Pass criterion: 0 unsupported claims, contradicted claims fixed or turned into erratum.
Return ONLY JSON: {{"claims_checked": N, "supported": N, "miscited": [...], "unsupported": [...], "contradicted": [...],
"verdict": "pass"|"fail"}} where list items are {{"cell_index": i, "claim": "...", "issue": "...", "fix": "..."}}.

NOTEBOOK:
{notebook}

SOURCE (pages marked "=== PDF page N ==="):
{source}"""

REVIEW = """You are an adversarial reviewer with a fresh context (ADR-0009 stage 6). You did not write this lesson.
Find errors, missing trade-offs, wrong numbers, misleading simplifications, gaps that would hurt a learner who later
builds a wallet, and pedagogy problems (order, diagnostic routes that do not match sections, weak recall cards).
Use the fact-check report, but do not trust it blindly. Classify each finding blocking or non-blocking.
Return ONLY JSON: {{"findings": [{{"severity": "blocking"|"non-blocking", "cell_index": i, "problem": "...",
"fix": "..."}}], "verdict": "pass"|"fail"}}  (pass = 0 blocking findings).

FACT-CHECK REPORT:
{fact_check}

NOTEBOOK:
{notebook}

SOURCE (pages marked "=== PDF page N ==="):
{source}"""

REVISE = """You are the drafting stage again (ADR-0009: findings from stages 4-6 loop back to stage 3). Revise the
lesson notebook so that every fact-check issue and every blocking review finding is fixed; turn non-blocking findings
into open cells. Keep everything else unchanged. Same rules as the first draft: Russian learner text, citations to
pdf_page for every claim, no invented facts.

{kinds}

Return ONLY the revised JSON object {{"lesson_id": ..., "cells": [...]}}.

FACT-CHECK REPORT:
{fact_check}

REVIEW FINDINGS:
{review}

NOTEBOOK:
{notebook}

SOURCE (pages marked "=== PDF page N ==="):
{source}"""
