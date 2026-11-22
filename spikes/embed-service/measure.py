"""Spike embed-service: memory and speed of bge-m3 as a service process, inside the hardened container.

Service limits under test (SRV-SEARCH review): at most 32 texts of at most 1,200 chars per call, queries of at
most 1,000 chars, max_seq_length 1,024 tokens (512 kept as a comparison; --max-seq), one encode at a time, job
passage batches of 8.

Phases, in order of rising cost, so getrusage's monotonic peak after each phase is that phase's peak:
  load -> idle query -> batch of 8 (prose) -> batch of 32 (prose) -> worst batch of 8 -> worst batch of 32
  -> throughput run (prose, batches of 8).
"Worst" texts: --worst-pool cyrillic (random Cyrillic letters, no spaces) or dense (mixed scripts and symbols,
more than one token per char), so every text reaches the cap; the real token count is reported.
Text is synthetic (seeded), never book text. Prints one JSON object.
"""

import argparse
import json
import os
import random
import resource
import time

import numpy as np
import sentence_transformers
import torch
import transformers
from sentence_transformers import SentenceTransformer

SYLLABLES = "ка ло ми ре на то ви за ст пр ов ен ни ть ра ле ко но ли ва се ти ро ме де ба гу шо".split()
LETTERS = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"


def prose(rng: random.Random, chars: int = 1200) -> str:
    out: list[str] = []
    while sum(len(w) + 1 for w in out) < chars:
        n = rng.randint(6, 14)
        sentence = ["".join(rng.choice(SYLLABLES) for _ in range(rng.randint(1, 4))) for _ in range(n)]
        sentence[0] = sentence[0].capitalize()
        sentence[-1] += "."
        out.extend(sentence)
    return " ".join(out)[:chars]


DENSE = LETTERS + "αβγδεζηθλμξπσφψω0123456789+-*/=^_{}()[]<>|\\$%&#@~;:,.!?" + "".join(
    chr(c) for c in range(0x4E00, 0x4E00 + 200))  # mixed scripts and symbols: more than one token per char


def worst(rng: random.Random, chars: int = 1200, pool: str = LETTERS) -> str:
    return "".join(rng.choice(pool) for _ in range(chars))


def rss_mib() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)  # Linux: KiB


def cgroup_peak_mib() -> float | None:
    try:
        with open("/sys/fs/cgroup/memory.peak") as f:
            return round(int(f.read()) / 2**20, 1)
    except (OSError, ValueError):
        return None


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="/models/BAAI/bge-m3")
    p.add_argument("--dtype", choices=["float32", "bfloat16"], default="float32")
    p.add_argument("--throughput-chunks", type=int, default=256)
    p.add_argument("--weights-only-check", action="store_true")
    p.add_argument("--max-seq", type=int, default=512)
    p.add_argument("--worst-pool", choices=["cyrillic", "dense"], default="cyrillic")
    p.add_argument("--only-batch8-worst", action="store_true",
                   help="load, then one worst batch of 8 only: isolates that phase's peak")
    a = p.parse_args()

    if a.weights_only_check:  # explicit torch.load(weights_only=True) of the shipped checkpoint
        t = time.perf_counter()
        sd = torch.load(f"{a.model}/pytorch_model.bin", map_location="cpu", weights_only=True)
        print(json.dumps({"weights_only_load_ok": True, "tensors": len(sd), "load_s": round(time.perf_counter() - t, 2),
                          "torch": torch.__version__, "transformers": transformers.__version__,
                          "sentence_transformers": sentence_transformers.__version__}))
        return

    threads = int(os.environ.get("THREADS", "0")) or os.cpu_count() or 1
    torch.set_num_threads(threads)
    rng = random.Random(20261121)
    out: dict[str, object] = {"dtype": a.dtype, "torch_threads": threads, "torch": torch.__version__,
                              "transformers": transformers.__version__,
                              "sentence_transformers": sentence_transformers.__version__}

    def phase(name: str, texts: list[str], batch: int) -> None:
        t = time.perf_counter()
        model.encode(texts, batch_size=batch, normalize_embeddings=True)
        out[f"{name}_s"] = round(time.perf_counter() - t, 3)
        out[f"rss_mib_after_{name}"] = rss_mib()
        out[f"cgroup_peak_mib_after_{name}"] = cgroup_peak_mib()

    t = time.perf_counter()
    kwargs = {} if a.dtype == "float32" else {"torch_dtype": torch.bfloat16}
    model = SentenceTransformer(a.model, device="cpu", model_kwargs=kwargs)
    model.max_seq_length = a.max_seq
    out["max_seq_length"] = a.max_seq
    out["worst_pool"] = a.worst_pool
    out["load_s"] = round(time.perf_counter() - t, 2)
    out["rss_mib_after_load"] = rss_mib()
    out["cgroup_peak_mib_after_load"] = cgroup_peak_mib()
    tok = model.tokenizer
    pool = DENSE if a.worst_pool == "dense" else LETTERS
    out["worst_text_tokens_untruncated"] = len(tok(worst(rng, pool=pool))["input_ids"])
    out["prose_text_tokens_untruncated"] = len(tok(prose(rng))["input_ids"])

    if a.only_batch8_worst:
        phase("batch8_worst", [worst(rng, pool=pool) for _ in range(8)], 8)
        print(json.dumps(out))
        return

    query = prose(rng, 1000)
    model.encode([query[:60]], normalize_embeddings=True)  # warm-up of the query path
    phase("query_idle", [query], 1)
    phase("batch8_prose", [prose(rng) for _ in range(8)], 8)
    phase("batch32_prose", [prose(rng) for _ in range(32)], 32)
    phase("batch8_worst", [worst(rng, pool=pool) for _ in range(8)], 8)
    phase("batch32_worst", [worst(rng, pool=pool) for _ in range(32)], 32)

    texts = [prose(rng) for _ in range(a.throughput_chunks)]
    t = time.perf_counter()
    vecs = model.encode(texts, batch_size=8, normalize_embeddings=True)
    dt = time.perf_counter() - t
    out["throughput_chunks"] = len(texts)
    out["throughput_s"] = round(dt, 2)
    out["chunks_per_s"] = round(len(texts) / dt, 2)
    out["rss_mib_peak"] = rss_mib()
    out["cgroup_memory_peak_mib"] = cgroup_peak_mib()
    out["dim"] = int(vecs.shape[1])
    out["first8"] = vecs[:8].astype(np.float32).round(5).tolist()  # fp32 vs bf16 agreement; no text
    print(json.dumps(out))


if __name__ == "__main__":
    main()
