# Spike: embeddings-local — RESULT

**Question (docs/roadmap.md 0.3):** Which local multilingual embedding model to use (no paid embedding API)?
**Measure:** chunks/s on CPU; recall@10 on 30 pilot questions written by the agent and reviewed by the owner.
**Pass:** recall@10 ≥ 0.8; full pilot embedded in ≤ 30 min. **Fallback:** a smaller model plus a larger rerank window.

**Verdict, provisional:** `intfloat/multilingual-e5-small` is the only candidate that meets both criteria: 2.7 min for the full pilot, recall@10 1.0. The two bge-m3 models also reach recall@10 1.0 but take 32–37 min on the Docker VM's 4 CPUs. The verdict is provisional for two reasons:
1. **The questions are still `status: draft`** and await the owner's review.
2. **The question set does not discriminate.** recall@10 is 1.0 for all three models, and recall@1 is 0.733 for all three. The models differ only at ranks 2–6. A harder set (paraphrased questions without the book's keywords, more chapters) is needed before the choice is final.

## Candidates

Facts from the Hugging Face model API and model cards, 2026-11-20.

| Model | Params | Licence | Why it is a candidate |
| --- | --- | --- | --- |
| `intfloat/multilingual-e5-small` | 117.7 M | MIT | Small and fast; the fallback size. Card lists `ru`; needs `query: ` / `passage: ` prefixes |
| `deepvk/USER-bge-m3` | 359.0 M | Apache-2.0 | bge-m3 adapted to Russian. Per its card, ruMTEB average 0.706 vs 0.689 for bge-m3, but retrieval average 0.934 vs 0.945 |
| `BAAI/bge-m3` | about 568 M (2.27 GB fp32 `pytorch_model.bin`) | MIT | Multilingual, 8192-token context, no query instruction needed; best ruMTEB retrieval average of the three per the USER-bge-m3 card |

The benchmark decides between them on this corpus. The model-card numbers are the vendors' own and are only the reason to shortlist.

## Method

- **Corpus:** the pilot PDF's text layer (pypdfium2), sha256 `61e8d4c3…c9e2b2`. Fixed 1200-character windows with 200-character overlap inside each page; chunks under 80 characters (running heads) are dropped. The full pilot gives 2285 chunks.
- **Embedding:** sentence-transformers 6.3.0 on CPU (torch 2.15.0+cpu), batch 32, normalized vectors, cosine via dot product. Image `python:3.14-slim-trixie`; models baked in at build; run with `--network none`.
- **Recall@10:** a question counts as a hit if any of the top-10 chunks comes from one of its `gold_pages`.
- **Copyright:** the book's text exists only in memory inside the container. Results hold numbers, question ids and page numbers only.

## Smoke test (functional only, not a measurement)

`results/20261120T182846-smoke/intfloat__multilingual-e5-small.json`: pages 99–118 (44 chunks), 3 questions (q04, q05, q06). All 3 hit at rank 1. 13.9 chunks/s with 4 torch threads; a Docling build and other containers shared the 4-CPU Docker VM, so the speed is not a result. A 44-chunk corpus makes recall trivially high. The smoke test only proves that the chunking, embedding, ranking and scoring work end to end, offline.

## Full benchmark

```bash
docker build --platform linux/arm64 -t reader-spike-embeddings spikes/embeddings-local && \
  TAG=draft-questions bash spikes/embeddings-local/run.sh "<path to the Bashir PDF>"
```

Run 2026-11-20, 19:57–21:10, `results/20261120T195713-draft-questions/`: all 935 pages, 2285 chunks, 30 draft questions, 4 torch threads on the 4-CPU Docker VM. Other light containers ran on the same VM for part of the time (the trace-backend spike, pytest runs), so the times are upper bounds.

| Model | Chunks/s | Full pilot (min) | recall@10 | recall@3 | recall@1 | MRR | Worst gold rank | Load (s) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| multilingual-e5-small | **14.35** | **2.7** | 1.0 | 0.900 | 0.733 | 0.822 | 6 | 1.43 |
| USER-bge-m3 | 1.02 | 37.4 | 1.0 | 0.933 | 0.733 | 0.839 | 4 | 0.86 |
| bge-m3 | 1.18 | 32.3 | 1.0 | 0.933 | 0.733 | 0.839 | 4 | 1.17 |

recall@3, recall@1, MRR and the worst rank are computed from `per_question[].rank` in the same files, with the `jq` command in the commit message. recall@10 is the runner's own field.

| Measure | Value |
| --- | --- |
| Image size (3 models) | 9,162,791,837 bytes |
| Docker VM CPUs | 4 |
| Peak memory per model | not measured by the runner |

**Reading:**
- On this question set, quality is practically equal: one question moves from rank ≤ 3 to rank 4–6 with e5-small, and MRR differs by 0.017.
- e5-small is 12–14 times faster, and the only model under the 30-minute bound.
- The bge-m3 models would fit only with more CPUs or natively on the host. Neither was measured here; it is the same open question as ADR-0021's M1.
- The roadmap's fallback ("a smaller model plus a larger rerank window") is in effect the e5-small choice. A rerank stage was not measured.

## Needs the owner

- Review `questions.yaml`: 30 questions in Russian over chapters 1–21, each with its gold pages. Chapters 22 and 23 have no question. Edit or replace questions; keep `gold_pages`.
- Agree on a harder set (the agent can draft it): paraphrased questions that avoid the book's key terms, and questions whose answer spans several pages. The current set saturates recall@10 for every model. Then rerun the three models. The runner recomputes embeddings on every run, so a rerun takes about 75 min on the 4-CPU VM; caching the embeddings would cut that to seconds.
