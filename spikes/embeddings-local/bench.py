"""Spike embeddings-local: chunk the pilot PDF's text layer, embed the chunks with one local model on CPU,
measure chunks/s and recall@1/3/10, MRR and the worst rank on a question set. Prints one JSON object with numbers only.

    bench.py <pdf> <questions.yaml> --model intfloat/multilingual-e5-small [--pages 1-935] [--questions-limit N]
             [--cache /cache]

The PDF is copyrighted: its text is held in memory only and never written out. The output names questions by
id and pages by number. --cache keeps chunk vectors per (model, corpus sha256, chunking, page range) so a rerun
with another question set takes seconds; the cache holds vectors and page numbers only.
"""

import argparse
import hashlib
import json
import platform
import time
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
import torch
import yaml
from sentence_transformers import SentenceTransformer

# Input conventions from the model cards: e5 needs "query: " / "passage: "; bge-m3 and USER-bge-m3 need none.
PREFIXES = {"intfloat/": ("query: ", "passage: ")}


def prefixes(model: str) -> tuple[str, str]:
    return next((v for k, v in PREFIXES.items() if model.startswith(k)), ("", ""))


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def cache_key(model: str, corpus_sha256: str, size: int, overlap: int, first: int, last: int) -> str:
    """One cache entry per (model, corpus, chunking parameters, page range)."""
    return f"{model.replace('/', '__')}__{corpus_sha256[:16]}__c{size}o{overlap}min80__p{first}-{last}"


def cache_load(directory: str, key: str, expected: int) -> tuple[np.ndarray | None, dict | None]:
    if not directory:
        return None, None
    vec, meta = Path(directory, key + ".npy"), Path(directory, key + ".json")
    if not (vec.exists() and meta.exists()):
        return None, None
    m = json.loads(meta.read_text())
    emb = np.load(vec)
    return (emb, m["timing"]) if emb.shape[0] == expected == m["chunks"] else (None, None)


def cache_save(directory: str, key: str, emb: np.ndarray, pages: list[int], timing: dict) -> None:
    """Vectors and page numbers only: the book's text is never written."""
    if not directory:
        return
    Path(directory).mkdir(parents=True, exist_ok=True)
    np.save(Path(directory, key + ".npy"), emb)
    Path(directory, key + ".json").write_text(json.dumps({"key": key, "chunks": int(emb.shape[0]),
                                                          "pages": pages, "timing": timing}))


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
    ap.add_argument("--cache", default="", help="directory for cached chunk vectors (vectors and page numbers only)")
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

    key = cache_key(a.model, sha256_file(a.pdf), a.chunk_chars, a.overlap, first, last)
    emb, timing = cache_load(a.cache, key, len(chunks))
    cache_state = "hit" if emb is not None else "miss"
    if emb is None:
        t1 = time.monotonic()
        emb = model.encode([p_prefix + c[1] for c in chunks], batch_size=a.batch, normalize_embeddings=True,
                           convert_to_numpy=True)
        embed_s = time.monotonic() - t1
        timing = {"embed_s": round(embed_s, 2), "chunks_per_s": round(len(chunks) / embed_s, 2),
                  "torch_threads": torch.get_num_threads(), "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        cache_save(a.cache, key, emb, [c[0] for c in chunks], timing)
    qemb = model.encode([q_prefix + q["question"] for q in questions], normalize_embeddings=True,
                        convert_to_numpy=True)

    pages = np.array([c[0] for c in chunks])
    per_q = []
    for q, v in zip(questions, qemb):
        order = np.argsort(-(emb @ v))
        ranked_pages = pages[order]
        gold = np.isin(ranked_pages, q["gold_pages"])
        rank = int(np.argmax(gold)) + 1 if gold.any() else None   # rank over the whole corpus, not capped at k
        per_q.append({"id": q["id"], "rank": rank, "hit": rank is not None and rank <= a.k,
                      "top_pages": [int(p) for p in ranked_pages[: a.k]]})

    ranks = [r["rank"] for r in per_q]
    n = max(len(per_q), 1)
    cps = timing["chunks_per_s"]
    print(json.dumps({
        "model": a.model, "pages": [first, last], "chunk_chars": a.chunk_chars, "overlap": a.overlap,
        "chunks": len(chunks), "full_pilot_chunks": len(all_chunks), "max_seq_length": model.max_seq_length,
        "load_s": round(load_s, 2), "cache": cache_state,
        # On a cache hit the timing is the one measured when the vectors were made (see measured_at).
        "embed_s": timing["embed_s"], "chunks_per_s": cps, "embed_measured_at": timing["measured_at"],
        "embed_torch_threads": timing["torch_threads"],
        "est_full_pilot_min": round(len(all_chunks) / cps / 60, 1),
        "questions_file": a.questions, "questions": len(questions),
        "recall_at_1": round(sum(r is not None and r <= 1 for r in ranks) / n, 3),
        "recall_at_3": round(sum(r is not None and r <= 3 for r in ranks) / n, 3),
        f"recall_at_{a.k}": round(sum(r is not None and r <= a.k for r in ranks) / n, 3),
        "mrr": round(sum(1 / r for r in ranks if r) / n, 3),
        "worst_rank": max((r for r in ranks if r), default=None),
        "torch_threads": torch.get_num_threads(), "cpu": platform.machine(), "per_question": per_q,
    }, ensure_ascii=False))


main()
