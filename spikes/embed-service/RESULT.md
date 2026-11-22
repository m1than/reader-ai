# Spike embed-service: bge-m3 as a hardened embed service (memory and speed)

Requested by SRV-SEARCH (branch `V/V.SRV-SEARCH-min`, FR-SEARCH-1/2): the draft places embedding in a dedicated internal `embed` service and needs a measured `mem_limit` before approval.

**Status:** measured on 2026-11-21/22. Numbers only; every text is synthetic (seeded pseudo-Russian prose and random character strings, generated in `measure.py`); no book text.

## Setup

| Item | Value |
| --- | --- |
| Host | owner's Mac, Docker Desktop VM with 13 CPUs, 11.6 GiB memory (`docker info`) |
| Image | `reader-spike-embed-service` from `Dockerfile`: `python:3.14-slim-trixie`, torch 2.15.0+cpu, transformers 5.23.0, sentence-transformers 6.3.0 |
| Model | `BAAI/bge-m3` at revision `5617a9f61b028005a4858fdac845db406aefb181`, **baked into the image at build time**; dense path only (no ONNX, no colbert/sparse heads, no images). `pytorch_model.bin` is 2,271,145,830 bytes |
| Image size | 5,604,639,231 bytes (≈ 5.6 GB) |
| Container flags (OPS-DEPLOY hardening) | `--network none --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,size=64m`, non-root uid 10003, `--memory 8g --memory-swap 8g` (a measuring ceiling, not the recommendation), `--cpus N` with `torch.set_num_threads(N)` |
| Service limits under test (SRV-SEARCH review) | ≤ 32 texts of ≤ 1,200 chars per call; queries ≤ 1,000 chars; `max_seq_length` **1,024** (512 kept as a comparison); one encode at a time; job passage batches of 8 |
| Memory readings | `getrusage` peak RSS (monotonic, so a phase's value is the peak so far) and the container's cgroup `memory.peak` |

Phases in one process, in rising order of cost: load → idle query (1,000 chars) → batch of 8 prose → batch of 32 prose → worst batch of 8 → worst batch of 32 → throughput (256 prose chunks in batches of 8).

Token counts of the inputs (bge-m3 tokenizer, untruncated):
- prose chunk of 1,200 chars: 476–495 tokens;
- "cyrillic" worst text (random Cyrillic letters, no spaces): 828–840 tokens;
- "dense" worst text (random mix of Cyrillic, Greek, digits, symbols and CJK): 1,092–1,126 tokens, so every text reaches the 1,024 cap.

Commands:
```bash
docker build --platform linux/arm64 -t reader-spike-embed-service spikes/embed-service
bash spikes/embed-service/run.sh                           # cap 1,024, dense worst texts
CAP=512 POOL=cyrillic bash spikes/embed-service/run.sh     # 512-cap comparison
ONLY_BATCH8=1 bash spikes/embed-service/run.sh             # isolated worst batch of 8 at cap 1,024
```

## Results: cap 1,024, dense worst texts (`results/20261122T184120-cap1024/`, `results/20261122T210110-cap1024/`)

| fp32, CPUs | Load s | Idle query s | Batch 8 prose s | Batch 8 worst s | Batch 32 worst s | Prose chunks/s (batch 8) | Peak RSS MiB | cgroup `memory.peak` MiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2 | 1.18 | 2.19 | 20.80 | 46.90 | 194.88 | 0.41 | 3,634.8 | 2,289.5 |
| 4 | 0.88 | 0.98 | 9.29 | 23.45 | 105.62 | 0.69 | 3,637.6 | 2,415.1 |
| 8 | 1.41 | 0.93 | 6.55 | 13.29 | 62.44 | 1.05 | 3,643.9 | 2,327.1 |

- **Worst admitted request** (32 texts × 1,024 tokens): peak RSS **3,644 MiB**, `memory.peak` **2,289–2,415 MiB**.
- **Worst batch of 8, isolated** (load, then one worst batch of 8 only; 8 CPUs): 11.27 s, peak RSS **2,410 MiB**, `memory.peak` **1,137 MiB**. In the full runs the batch-of-8 values are masked by the earlier batch of 32 prose.
- After load: RSS 877 MiB, `memory.peak` 786–840 MiB. RSS rises to about 2,030 MiB at the first encode, while `memory.peak` stays near 800 MiB.

## Results: cap 512 comparison, cyrillic worst texts (`results/20261121T011550-cap512/`)

| fp32, CPUs | Idle query s | Batch 8 worst s | Batch 32 worst s | Prose chunks/s | Peak RSS MiB | cgroup `memory.peak` MiB |
| --- | --- | --- | --- | --- | --- | --- |
| 2 | 2.02 | 21.01 | 97.69 | 0.41 | 2,956.1 | 1,625.9 |
| 4 | 0.97 | 9.18 | 41.20 | 0.91 | 2,930.3 | 1,602.9 |
| 8 | 0.48 | 4.90 | 21.18 | 1.72 | 2,910.5 | 1,567.7 |

Raising the cap from 512 to 1,024 adds about 700 MiB to peak RSS and about 750 MiB to `memory.peak` for the worst batch of 32, and makes the worst batch about 3 times slower (attention is quadratic in sequence length).

The prose throughput run is the same input in both passes but measured 1.72 chunks/s (cap-512 pass) against 1.05 chunks/s (cap-1,024 pass) at 8 CPUs; prose stays under both caps, so the difference is run-to-run variance on the shared VM, not the cap. Read the throughput column as ±40 %.

## bf16 on CPU (`bf16-cpus8` in the cap-1,024 run)

Rejected. Loading with `torch_dtype=bfloat16` gave RSS 3,838 MiB right after load (the fp32 checkpoint is read, then cast) and `memory.peak` 3,027 MiB, both above fp32. It was also about 9–30 times slower: idle query 10.1 s, worst batch of 32 1,972 s, prose 0.08 chunks/s. Vectors agreed with fp32 (cosine 0.998–1.004 on 8 samples), but bf16 brings no gain on this CPU. fp16 was not run.

## `weights_only`

`torch.load("/models/BAAI/bge-m3/pytorch_model.bin", map_location="cpu", weights_only=True)` succeeds and returns 391 tensors (torch 2.15.0+cpu, transformers 5.23.0, sentence-transformers 6.3.0; `results/*/weights-only.json`). sentence-transformers loads the same checkpoint through transformers, which calls `torch.load` with the torch ≥ 2.6 default `weights_only=True`. That library path was not instrumented separately; the explicit load above is the measured fact.

## Recommendation

- **`mem_limit` for the embed service: 4.5 GiB** (`4608m`, with `memswap_limit` equal).
  - Base: the worst admitted request's peak RSS, 3,644 MiB × 1.25 = 4,555 MiB, rounded up to 0.25 GiB.
  - Peak RSS is used rather than `memory.peak` (2,415 MiB × 1.25 → 3.0 GiB). RSS includes the memory-mapped 2.27 GB checkpoint, whose file pages can be charged to this container's cgroup on a cold start, when nothing else has the file cached. Clean file pages are reclaimable, so 3 GiB would probably run, but it would evict weights under load and thrash.
- **If the service took at most 8 texts per call:** 2,410 MiB × 1.25 → 3.0 GiB. This is an option for the reviewer; the current limit is 32.
- **CPU:** 8 CPUs gives an idle query in about 0.5–0.9 s and a worst batch of 8 in 11–13 s. 4 CPUs roughly doubles both.
- **Full pilot (2,285 chunks) at 1.05–1.72 chunks/s on 8 CPUs:** about 22–36 min. The embeddings-local spike measured 14 min at 13 CPUs with batch 32.
- **dtype:** fp32. bf16 is rejected (above).
- **Image:** 5.6 GB with the model baked in.
