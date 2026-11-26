"""Spike retrieval-ablation, stage 1 (runs in the reader-embed image, --network none): embed the pilot's spans
and today's chunks with the embed service's own encoder, time the re-embedding, and rank both by exact dense
distance for every question of the evaluation set.

    python embed.py --spans /in/spans.jsonl --questions /in/eval.yaml --chunking /src/chunking.py --out /out

The book is copyrighted: its text stays in memory. /out receives vectors, ids, pages, kinds, lengths and ranks,
never text. Vectors are reused when present (delete /out/*.npy for a fresh, timed run).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch
import yaml
from reader.search.embed_service import MAX_TEXTS, MODEL_DIR, SentenceEncoder, model_id

DEPTH = 200  # ranked ids kept per question: enough for a filter that drops up to 95 % of the top


def load_chunking(path: str):  # the live module, loaded by file: chunk_spans is the reference by construction
    spec = importlib.util.spec_from_file_location("chunking", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["chunking"] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


def pages_of(entries: list[str]) -> list[int]:
    out: list[int] = []
    for entry in entries:
        a, _, b = str(entry).partition("-")
        out.extend(range(int(a), int(b or a) + 1))
    return out


def encode_timed(encoder: SentenceEncoder, texts: list[str]) -> tuple[np.ndarray, dict]:
    """One encode per batch of MAX_TEXTS in index order, as the search.embed job sends them to the service."""
    t0 = time.monotonic()
    rows: list[np.ndarray] = []
    for i in range(0, len(texts), MAX_TEXTS):
        rows.append(np.asarray(encoder.model.encode(texts[i:i + MAX_TEXTS], batch_size=MAX_TEXTS,
                                                    normalize_embeddings=True, show_progress_bar=False),
                               dtype=np.float32))
    wall = time.monotonic() - t0
    emb = np.vstack(rows) if rows else np.zeros((0, 1024), dtype=np.float32)
    return emb, {"texts": len(texts), "wall_s": round(wall, 1), "texts_per_s": round(len(texts) / wall, 2),
                 "batch": MAX_TEXTS, "torch_threads": torch.get_num_threads(),
                 "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S")}


def cached(out: Path, name: str, encoder: SentenceEncoder, texts: list[str]) -> tuple[np.ndarray, dict]:
    vec, meta = out / f"{name}.npy", out / f"{name}.timing.json"
    if vec.exists() and meta.exists():
        emb = np.load(vec)
        if emb.shape[0] == len(texts):
            return emb, {**json.loads(meta.read_text()), "cache": "hit"}
    emb, timing = encode_timed(encoder, texts)
    np.save(vec, emb)
    meta.write_text(json.dumps(timing))
    return emb, {**timing, "cache": "miss"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spans", required=True)
    ap.add_argument("--questions", required=True)
    ap.add_argument("--chunking", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    spans = [json.loads(line) for line in open(a.spans, encoding="utf-8")]
    chunking = load_chunking(a.chunking)
    # Today's reference: the search.embed job chunks every span of the material (search/jobs.py), 1,200/200/80.
    chunks = chunking.chunk_spans([chunking.Span(s["seq"], s["page"], s["text"]) for s in spans], 1200, 200, 80)
    # V0: one Docling element per span (ADR-0039 B1); a span with no text cannot be embedded and is not indexed.
    indexed = [s for s in spans if s["text"].strip()]

    questions = yaml.safe_load(open(a.questions, encoding="utf-8"))["questions"]

    t0 = time.monotonic()
    encoder = SentenceEncoder(MODEL_DIR)
    load_s = round(time.monotonic() - t0, 2)
    max_seq = encoder.model.max_seq_length
    tok = encoder.model.tokenizer
    span_tokens = [len(tok(s["text"], add_special_tokens=True)["input_ids"]) for s in indexed]

    span_emb, span_t = cached(out, "spans", encoder, [s["text"] for s in indexed])
    chunk_emb, chunk_t = cached(out, "chunks", encoder, [c.text for c in chunks])
    t1 = time.monotonic()
    q_emb = np.asarray(encoder.encode("query", [q["question"] for q in questions]), dtype=np.float32)
    query_s = time.monotonic() - t1
    np.save(out / "queries.npy", q_emb)

    meta = {
        "model": model_id(), "max_seq_length": max_seq, "load_s": load_s,
        "spans_total": len(spans), "spans_indexed": len(indexed), "spans_empty": len(spans) - len(indexed),
        "chunks": len(chunks),
        "span_chars": [len(s["text"]) for s in indexed],
        "span_bge_tokens": span_tokens,
        "spans_over_1200_chars": sum(len(s["text"]) > 1200 for s in indexed),
        "spans_over_max_seq": sum(t > max_seq for t in span_tokens),
        "embed_spans": span_t, "embed_chunks": chunk_t,
        "query_embed_ms_mean": round(1000 * query_s / len(questions), 1),
        "span_ids": [s["seq"] for s in indexed], "span_pages": [s["page"] for s in indexed],
        "span_kinds": [s["kind"] for s in indexed],
        "chunk_pages": [c.page for c in chunks], "chunk_chars": [len(c.text) for c in chunks],
        "chunk_primary": [c.primary_seq for c in chunks],
        "questions": [{"id": q["id"], "type": q["type"], "chapter": q["chapter"],
                       "gold": pages_of(q["gold_pages"])} for q in questions],
    }
    (out / "meta.json").write_text(json.dumps(meta))

    dense = {}
    for name, emb, ids in (("spans", span_emb, meta["span_ids"]), ("chunks", chunk_emb, list(range(len(chunks))))):
        scores = q_emb @ emb.T
        order = np.argsort(-scores, axis=1, kind="stable")[:, :DEPTH]
        dense[name] = {q["id"]: [ids[j] for j in row] for q, row in zip(questions, order.tolist())}
    (out / "dense.json").write_text(json.dumps(dense))

    # pgvector input for the latency step: seq and vector literal, numbers only
    with open(out / "spans_vec.tsv", "w") as f:
        for s, v in zip(indexed, span_emb):
            f.write(f"{s['seq']}\t{s['page']}\t{s['kind']}\t[{','.join(f'{x:.7g}' for x in v)}]\n")
    with open(out / "queries_vec.tsv", "w") as f:
        for q, v in zip(questions, q_emb):
            # the question text is the evaluation set's own wording (no book text); full text needs it
            f.write(f"{q['id']}\t{' '.join(q['question'].split())}\t[{','.join(f'{x:.7g}' for x in v)}]\n")
    print(json.dumps({k: meta[k] for k in ("model", "spans_total", "spans_indexed", "chunks",
                                           "spans_over_1200_chars", "spans_over_max_seq")}
                     | {"embed_spans": span_t, "embed_chunks": chunk_t}))


main()
