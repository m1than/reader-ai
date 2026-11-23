# Spike: study-rate — RESULT (step 1 and 1b; step 2 pending)

**Question (SRV-PLAN, owner 2026-11-23):** how many minutes of study does 1,000 characters of this kind of material
take, so the planner can size lessons to the learner's minutes per day without an invented number? And how many
characters does L02 hold on the planner's basis, so the compile estimate can scale its measured cost?

**Method (no model spend, numbers only).** `measure.py` reads the owner's own hand-made study plan
(`pilot/bashir/plan.yaml`: theory hours per stage, from the blockchain study plan in the owner's knowledge base,
2026-11-06) and the PDF page ranges of its 23 lessons (`pilot/bashir/course.yaml`), and counts characters per page:
- **step 1:** the PDF text layer (pypdfium2), `results/step1.json`;
- **step 1b:** the stack's own spans, re-converted with the pinned confined converter (SRV-INGEST, docling
  2.151.1+r2, deterministic for a converter version, as spikes/tutor-chunks did), into a scratch directory that is
  deleted afterwards, `results/step1-spans.json`. This is what the planner counts (SRV-INGEST FR-INGEST-24).

Only theory hours are used: the plan's code hours (209.8) are for building a Rust wallet alongside the book, not for
studying its text.

Date: 2026-11-23. Commands:
`uv run --project server python -I spikes/study-rate/measure.py <pilot.pdf> spikes/study-rate/results/step1.json`
and the same with `results/step1-spans.json <data_dir> <scratch work dir>`.

## Results

| | Text layer (step 1) | Spans (step 1b, planner basis) |
| --- | --- | --- |
| Planned pages (23 lessons) | 890 | 890 |
| Characters on those pages | 1,883,175 | 1,841,091 |
| Theory minutes in the owner's plan | 4,860 (81 h) | 4,860 |
| **Minutes per 1,000 characters** | 2.58 | **2.64** |
| Per stage: min / median / max | 1.40 / 2.30 / 5.18 | 1.40 / 2.36 / 5.30 |
| Minutes per PDF page | 5.46 | 5.46 |
| Characters per page: p10 / median / p90 | 1,299 / 2,229 / 2,765 | 1,189 / 2,149 / 2,748 |
| **L02 (PDF 60–84) characters** | 54,742 | **58,938** |

## Findings

1. **Default study rate: 2.64 minutes per 1,000 characters** (spans basis), from the owner's own plan. It is a
   sourced owner estimate, not a measurement of study: the plan's hours were set by hand.
2. **The spread is wide:** 1.40–5.30 per stage. The cryptography stages (S02a, S02b) and consensus and privacy
   (S04, S11) are budgeted 1.5–2× the median, orientation (S01) 0.6×. One rate per material mis-sizes such stages;
   per-kind or per-chapter rates are an SRV-PLAN defer.
3. **Cross-check against reading speed:** silent reading of English non-fiction averages 238 words per minute
   (Brysbaert 2019, J. Memory and Language 109, 104047). A median pilot page of about 2,150 characters is roughly 300
   Russian words (an assumed 7 characters per word including the space, not measured), about 1.3 min of reading. The
   plan budgets 5.46 min per page, about 4× reading time, which is plausible for study with notes and recall.
4. **Pages are the wrong unit:** characters per page range 1,189 (p10) to 2,748 (p90), a factor of 2.3.
5. **Compile scaling basis:** L02 holds 58,938 span characters. With the converged product run ($1.2587, V.live
   2026-11-23) that is $0.0214 per 1,000 characters; with the dearest spike run ($2.9714, one loop, not converged)
   $0.0504. Over the 1,841,091 planned characters of the pilot: about $39–93 for the whole course, against ADR-0005's
   $100 target. One lesson measured: the hypothesis that cost scales with characters is untested.

## Step 2 (pending, needs the owner's time, no credit)

Measure the owner's real pace: for at least 5 study sessions on L02, record start, end and the PDF pages covered
(numbers only, `sessions.yaml`). The measured minutes per 1,000 characters replace the default when they differ by
more than 25 % (a chosen bound), recorded in SRV-PLAN's review line.
