"""Spike embeddings-local: chunk the pilot PDF's text layer, embed the chunks with one local model on CPU,
measure chunks/s and recall@10 on a question set. Prints one JSON object with numbers only.

    bench.py <pdf> <questions.yaml> --model intfloat/multilingual-e5-small [--pages 1-935] [--questions-limit N]

The PDF is copyrighted: its text is held in memory only and never written out. The output names questions by
id and pages by number.
"""

import argparse
import json
import platform
import time

import numpy as np
import pypdfium2 as pdfium
import torch
import yaml
from sentence_transformers import SentenceTransformer

# Input conventions from the model cards: e5 needs "query: " / "passage: "; bge-m3 and USER-bge-m3 need none.
PREFIXES = {"intfloat/": ("query: ", "passage: ")}


def prefixes(model: str) -> tuple[str, str]:
    return next((v for k, v in PREFIXES.items() if model.startswith(k)), ("", ""))


def chunk_pages(pdf: str, first: int, last: int, size: int, overlap: int) -> list[tuple[int, str]]:
    """Fixed-size character windows inside each page; a chunk keeps its page number."""
    doc = pdfium.PdfDocument(pdf)
    chunks = []
    for p in range(first, last + 1):
        text = " ".join(doc[p - 1].get_textpage().get_text_range().split())
        step = size - overlap
        for start in range(0, max(len(text) - overlap, 1), step):
            piece = text[start:start + size]
            if len(piece) >= 80:  # skip running heads and near-empty pages
                chunks.append((p, piece))
    return chunks


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("questions")
    ap.add_argument("--model", required=True)
    ap.add_argument("--pages", default="")
    ap.add_argument("--chunk-chars", type=int, default=1200)
    ap.add_argument("--overlap", type=int, default=200)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--questions-limit", type=int, default=0)
    a = ap.parse_args()

    n_pages = len(pdfium.PdfDocument(a.pdf))
    first, last = (map(int, a.pages.split("-")) if a.pages else (1, n_pages))
    all_chunks = chunk_pages(a.pdf, 1, n_pages, a.chunk_chars, a.overlap)
    chunks = [c for c in all_chunks if first <= c[0] <= last]
    qdoc = yaml.safe_load(open(a.questions, encoding="utf-8"))
    questions = [q for q in qdoc["questions"] if any(first <= p <= last for p in q["gold_pages"])]
    if a.questions_limit:
        questions = questions[: a.questions_limit]

    q_prefix, p_prefix = prefixes(a.model)
    t0 = time.monotonic()
    model = SentenceTransformer(f"/models/{a.model}", device="cpu")
    load_s = time.monotonic() - t0

    t1 = time.monotonic()
    emb = model.encode([p_prefix + c[1] for c in chunks], batch_size=a.batch, normalize_embeddings=True,
                       convert_to_numpy=True)
    embed_s = time.monotonic() - t1
    qemb = model.encode([q_prefix + q["question"] for q in questions], normalize_embeddings=True,
                        convert_to_numpy=True)

    pages = np.array([c[0] for c in chunks])
    per_q = []
    for q, v in zip(questions, qemb):
        top = np.argsort(-(emb @ v))[: a.k]
        top_pages = [int(pages[i]) for i in top]
        rank = next((r + 1 for r, p in enumerate(top_pages) if p in q["gold_pages"]), None)
        per_q.append({"id": q["id"], "hit": rank is not None, "rank": rank, "top_pages": top_pages})

    cps = len(chunks) / embed_s
    print(json.dumps({
        "model": a.model, "pages": [first, last], "chunk_chars": a.chunk_chars, "overlap": a.overlap,
        "chunks": len(chunks), "full_pilot_chunks": len(all_chunks), "max_seq_length": model.max_seq_length,
        "load_s": round(load_s, 2), "embed_s": round(embed_s, 2), "chunks_per_s": round(cps, 2),
        "est_full_pilot_min": round(len(all_chunks) / cps / 60, 1),
        "questions": len(questions), f"recall_at_{a.k}": round(sum(r["hit"] for r in per_q) / max(len(per_q), 1), 3),
        "torch_threads": torch.get_num_threads(), "cpu": platform.machine(), "per_question": per_q,
    }, ensure_ascii=False))


main()
