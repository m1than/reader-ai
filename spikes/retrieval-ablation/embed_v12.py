"""Spike retrieval-ablation, free step between the paid stages (reader-embed image, --network none): embed V1
(each span with its context prefix, B1: prefix + span text) and V2 (V1 plus one entry per L2 summary), rank by exact
dense distance, and write /out/dense_v12.json. Ids and numbers only; the prefixes and summaries are read from the
gitignored text outputs of paid.py.

    python embed_v12.py --spans /in/spans.jsonl --outline /in/outline.json --text /text --work /out
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import torch
from reader.search.embed_service import MAX_TEXTS, MODEL_DIR, SentenceEncoder

DEPTH = 200


def encode(encoder: SentenceEncoder, texts: list[str]) -> tuple[np.ndarray, float]:
    t0 = time.monotonic()
    rows = [np.asarray(encoder.model.encode(texts[i:i + MAX_TEXTS], batch_size=MAX_TEXTS, normalize_embeddings=True,
                                            show_progress_bar=False), dtype=np.float32)
            for i in range(0, len(texts), MAX_TEXTS)]
    return np.vstack(rows), time.monotonic() - t0


def main() -> None:
    ap = argparse.ArgumentParser()
    for name in ("spans", "outline", "text", "work"):
        ap.add_argument(f"--{name}", required=True)
    a = ap.parse_args()
    work = Path(a.work)
    spans = [json.loads(line) for line in open(a.spans, encoding="utf-8")]
    indexed = [s for s in spans if s["text"].strip()]
    # last row per id wins (append-only file); a None prefix ("prefix: none") leaves the span's own text
    rows = map(json.loads, Path(a.text, "prefixes.jsonl").read_text().splitlines())
    pre = {i: p for i, p in {r["id"]: r["prefix"] for r in rows}.items() if p}
    summ = {str(r["id"]): r["summary"] for r in map(json.loads, Path(a.text, "summaries.jsonl").read_text().splitlines())}
    outline = json.loads(Path(a.outline).read_text(encoding="utf-8"))
    nodes = sorted((o for o in outline if o["level"] <= 4 and o["page"]), key=lambda o: (o["page"], o["seq"]))
    last_page = max(s["page"] for s in spans)
    # a summary entry's pages: its own text's range (node page up to the next node's page), as paid.py summarised it
    node_pages = {}
    for i, o in enumerate(nodes):
        nxt = nodes[i + 1]["page"] if i + 1 < len(nodes) else last_page + 1
        node_pages[str(o["seq"])] = list(range(o["page"], max(nxt, o["page"] + 1)))
    entries = [k for k in summ if k in node_pages]   # the root summary has no page and is not indexed

    encoder = SentenceEncoder(MODEL_DIR)
    v1_emb, v1_s = encode(encoder, [f"{pre[s['seq']]}\n{s['text']}" if s["seq"] in pre else s["text"] for s in indexed])
    n_emb, n_s = encode(encoder, [summ[k] for k in entries])
    q = np.load(work / "queries.npy")
    meta = json.loads((work / "meta.json").read_text())
    qids = [x["id"] for x in meta["questions"]]

    v1_ids = [s["seq"] for s in indexed]
    v2_ids: list = [*v1_ids, *(f"n{k}" for k in entries)]
    out = {"node_pages": {f"n{k}": node_pages[k] for k in entries}}
    for name, emb, ids in (("V1_prefix", v1_emb, v1_ids), ("V2_summaries", np.vstack([v1_emb, n_emb]), v2_ids)):
        order = np.argsort(-(q @ emb.T), axis=1, kind="stable")[:, :DEPTH]
        out[name] = {qid: [ids[j] for j in row] for qid, row in zip(qids, order.tolist())}
    out["embed"] = {"v1_texts": len(indexed), "v1_wall_s": round(v1_s, 1), "spans_with_prefix": len(pre),
                    "summary_entries": len(entries), "summary_wall_s": round(n_s, 1),
                    "torch_threads": torch.get_num_threads()}
    (work / "dense_v12.json").write_text(json.dumps(out))
    print(json.dumps(out["embed"]))


main()
