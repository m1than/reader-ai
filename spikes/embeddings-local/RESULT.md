# Spike: embeddings-local — RESULT (draft: benchmark not run yet)

**Question (docs/roadmap.md 0.3):** Which local multilingual embedding model to use (no paid embedding API)?
**Measure:** chunks/s on CPU; recall@10 on 30 pilot questions written by the agent and reviewed by the owner.
**Pass:** recall@10 ≥ 0.8; full pilot embedded in ≤ 30 min. **Fallback:** a smaller model plus a larger rerank window.
**Verdict:** pending. Only the functional smoke test has run (below). The full benchmark waits for two things:
1. A free Docker VM. It is CPU-bound, and the ingest-docling measurement shares the VM.
2. The owner's review of `questions.yaml` (`status: draft`).

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

## Full benchmark (to run)

On a quiet Docker VM (no other CPU-bound containers):

```bash
docker build --platform linux/arm64 -t reader-spike-embeddings spikes/embeddings-local && \
  bash spikes/embeddings-local/run.sh "<path to the Bashir PDF>"
```

| Model | Chunks/s | Est. full pilot (min) | recall@10 (30 q) | Load (s) |
| --- | --- | --- | --- | --- |
| multilingual-e5-small | | | | |
| USER-bge-m3 | | | | |
| bge-m3 | | | | |

| Measure | Value |
| --- | --- |
| Image size (3 models) | |
| Docker VM CPUs | 4 |
| Peak memory per model | not measured by the runner yet |

## Needs the owner

- Review `questions.yaml`: 30 questions in Russian over chapters 1–21, each with its gold pages. Chapters 22 and 23 have no question. Edit or replace questions; keep `gold_pages`.
