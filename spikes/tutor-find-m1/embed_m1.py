"""Spike tutor-find-m1, stage 1 (runs in the reader-embed:local image, --network none): the bge-m3 vectors of every
production embedding window of the pilot's spans (SRV-SEARCH FR-SEARCH-3 windows, 1,200/200/80, no prefix, exactly
as `search.embed` cuts them) and of the 40 VEC-TUTOR-7 questions, with the embed service's own encoder.

    python embed_m1.py --spans /in/spans.jsonl --questions /in/questions.json --chunking /src/chunking.py \
        [--reuse /reuse] --out /out

A window that is a whole span (a span of at most 1,200 characters) reuses that span's vector from the
retrieval-ablation spike's `spans.npy` when given (same image, encoder, normalisation and span text); the other
windows and the questions are encoded here, one encode per batch of MAX_TEXTS as the service does.
The book is copyrighted: its text stays in memory. /out receives float32 vectors and the sha256 of each text,
never text.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path

import numpy as np
from reader.search.embed_service import MAX_TEXTS, MODEL_DIR, SentenceEncoder, model_id


def load_chunking(path: str):  # the live module, loaded by file
    spec = importlib.util.spec_from_file_location("chunking", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["chunking"] = module
    spec.loader.exec_module(module)
    return module


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def encode(encoder: SentenceEncoder, texts: list[str]) -> np.ndarray:
    rows = [np.asarray(encoder.model.encode(texts[i:i + MAX_TEXTS], batch_size=MAX_TEXTS,
                                            normalize_embeddings=True, show_progress_bar=False), dtype=np.float32)
            for i in range(0, len(texts), MAX_TEXTS)]
    return np.vstack(rows) if rows else np.zeros((0, 1024), dtype=np.float32)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spans", required=True)
    ap.add_argument("--questions", required=True)
    ap.add_argument("--chunking", required=True)
    ap.add_argument("--reuse")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    chunking = load_chunking(a.chunking)
    spans = [json.loads(line) for line in open(a.spans, encoding="utf-8")]
    passages: dict[str, str] = {}
    whole: set[str] = set()
    for s in spans:
        cut = chunking.windows(s["text"], 1200, 200, 80)
        for w in cut:
            passages.setdefault(sha(w.passage), w.passage)
        if len(cut) == 1:
            whole.add(sha(cut[0].passage))
    reused: dict[str, np.ndarray] = {}
    if a.reuse:
        indexed = [s for s in spans if s["text"].strip()]  # the ablation's span order (its embed.py)
        old = np.load(Path(a.reuse) / "spans.npy")
        assert old.shape[0] == len(indexed), (old.shape, len(indexed))
        for s, v in zip(indexed, old):
            key = sha(s["text"])
            if key in whole:
                reused[key] = v
    fresh_keys = [k for k in passages if k not in reused]
    questions = json.loads(Path(a.questions).read_text(encoding="utf-8"))
    t0 = time.monotonic()
    encoder = SentenceEncoder(MODEL_DIR)
    fresh = encode(encoder, [passages[k] for k in fresh_keys])
    q_vec = np.asarray(encoder.encode("query", [q["question"] for q in questions]), dtype=np.float32)
    wall = round(time.monotonic() - t0, 1)
    keys = list(reused) + fresh_keys + [sha(q["question"]) for q in questions]
    vectors = np.vstack([np.vstack(list(reused.values())) if reused else np.zeros((0, 1024), np.float32),
                         fresh, q_vec]).astype("<f4")
    (out / "vectors.f32").write_bytes(vectors.tobytes())
    (out / "keys.json").write_text(json.dumps(keys))
    print(json.dumps({"model": model_id(), "spans": len(spans), "windows_distinct": len(passages),
                      "reused": len(reused), "encoded": len(fresh_keys), "questions": len(questions),
                      "wall_s": wall}))


main()
