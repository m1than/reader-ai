---
id: ADR-0028
title: Embedding service — bge-m3 in a dedicated internal `embed` service on its own internal network (api, worker, embed only); model baked into the image at build
status: accepted
date: 2026-11-21
verified_by: "OPS-DEPLOY config check: service `embed` joins only network `embed` (internal: true), whose members are exactly api, worker and embed; embed is not a /run/secrets consumer and has no secret in its environment; read-only root, cap_drop ALL, no-new-privileges, non-root, mem_limit == memswap_limit from spikes/embed-service; VEC-DEPLOY-2's reachability pairs include api→embed and worker→embed and exclude embed→postgres, embed→mcp, embed→agent-proxy, embed→any kernel and embed→internet. SRV-SEARCH [image] tests: the image build fails on a model-file hash mismatch; the service starts with HF_HUB_OFFLINE=1 and no network; loading a pickle with a non-allow-listed global through the service's loader is refused (weights_only=True is behavioural, not just a flag)"
revisit_when: "A second embedding consumer outside api and worker appears; or the model no longer fits a bounded container on the owner's Mac; or local models move to the GX10 or MLX per ADR-0018"
supersedes: []
---

> Amends [ADR-0012](0012-deployment-and-auth.md) (the Compose stack and its networks gain `embed`) and [ADR-0025](0025-agent-sandbox-and-credential-separation.md) rule 5 (the worker's reachable set gains `embed`). Both amendments are notes for the owner to add when this ADR is accepted.

> Measurement note (2026-11-23, SRV-SEARCH code review of dd364ba; the Decision is unchanged): the Context's memory figure (2,410 MiB peak RSS, spikes/embed-service) did not include the service's own `weights_only` precheck, which first loaded the whole 2.27 GB checkpoint without mmap: `memory.peak` 3,169 MiB after load and 3,459 MiB after a worst batch, over the 3g limit and survived only through reclaim (`memory.events max` 2,870). The precheck now loads with `mmap=True`. Re-measured under compose's 3g, a real service start plus the worst admitted request (8 texts, each over the 1,024-token cap) peaks at `memory.peak` 2,114 MiB on a cold page cache (1,153 MiB warm) with `memory.events max` 0; 2,114 × 1.25 = 2,643 MiB, so `mem_limit` 3g stands. Numbers in OPS-DEPLOY FR-DEPLOY-9; the SRV-SEARCH [image] test `test_worst_batch_stays_under_the_compose_limit` re-checks them on every build.

## Context

SRV-SEARCH (slice step V3) needs BAAI/bge-m3 dense vectors, which the owner confirmed on 2026-11-21 (spikes/embeddings-local/RESULT.md). The vectors are needed in two places:
- the worker embeds every chunk of an ingested material (job `search.embed`);
- the api embeds every search query at request time, and so do V.COMPILE-min and V.TUTOR-min in the worker.

Numbers:
- **Weights:** bge-m3's fp32 `pytorch_model.bin` is 2.27 GB (spike, Candidates). It is a pickle file, so it is loaded with `torch.load(..., weights_only=True)`, which refuses arbitrary objects.
- **Container limits** (deploy/compose.yaml): the worker has `mem_limit: 2g`, the api `1g`.
- **Speed:** 2.71 chunks/s with 13 torch threads in the Docker VM; the full pilot of 2,285 chunks took 841.74 s, or 14.03 min (results/20261120T232835-v2/BAAI__bge-m3.json).
- **Memory** (`spikes/embed-service`, fp32, `max_seq_length` 1,024, worst-case texts): peak RSS 877 MiB after load; **2,410 MiB for the worst admitted request of 8 texts**; 3,644 MiB for a request of 32. The service admits at most 8 texts per call (SRV-SEARCH's passage batch size), so **`mem_limit` is 3g** (2,410 MiB × 1.25, rounded), with `memswap_limit` equal. bf16 was measured and rejected: more memory and 9–30× slower. `torch.load(weights_only=True)` loads the checkpoint (391 tensors).
- **Speed at 8 CPUs** (same spike): an idle query takes 0.5–0.9 s, a worst batch of 8 takes 11–13 s, and the full pilot about 22–36 min.
- **Standing memory budget** against the Docker VM's 11.6 GiB (ADR-0022): postgres 1g, api 1g, worker 2g, mcp 1g, agent-proxy 256m, one `kernel-<user>` 2g, plus embed's 3g, about 10.25 GiB in all. Limits are not reservations, but the sum shows what is left for more kernels.
- **Token lengths** of a 1,200-character chunk, measured by the reviewer with bge-m3's tokenizer: Russian prose 266, Rust code 603, hex 740, LaTeX 887. So the cap is `max_seq_length` 1,024, which covers all four kinds (SRV-SEARCH).
- **Networks:** every stack network except `edge`/`egress` is `internal: true`. Since ADR-0027 (on V/V.SRV-INGEST-min until merged), nothing on the host listens for the stack.

## Options

| Option | Gives | Costs (numbers) |
| --- | --- | --- |
| (a) In the worker, with `mem_limit` raised | One process holds the model; passage embedding has no network hop | The worker needs about 2.27 GB more than its 2g limit for weights alone. The api cannot embed queries: the worker has no listener, and query embedding is on the request path. The model would share the worker's cgroup with agent stages, so an OOM in one kills the other |
| (b) In the worker, and the api calls the worker | Query embedding works with one model copy | The worker gains a listener on a network the api shares, while it holds the DB DSN and runs agent stages (ADR-0025 rule 2). It adds attack surface to the most privileged service. The memory cost is the same as (a) |
| (e) In the api, and the worker calls the api | Queries embed in-process on the request path | The api's limit is 1g, against 2.27 GB of weights. The api faces the browser through Caddy, so a model-loader or tokenizer bug becomes reachable from the network edge. Passage jobs would load the request-serving process for about 14 min per book |
| **(c) A dedicated `embed` service** | api and worker both reach one model. The service holds no secret and no DB access, so a compromised model loader reaches nothing of value. It has its own memory limit | One more service, and an image of several GB because the model is baked in. Passage batches cross a network hop, which is negligible against about 0.37 s per chunk of compute |
| (d) Native on the host, as ingest does (ADR-0027) | Uses the Mac's CPUs directly | Query embedding happens at request time from containers on internal networks, which have no route to the host. A host listener is exactly what ADR-0027 removed. Only the passage half could move, and then two model builds would produce the vectors |

## Decision

**(c).** A dedicated service `embed`, image `reader-embed:local`, on its own network `embed` (`internal: true`), whose members are exactly `api`, `worker` and `embed`.
- **Network:** `embed` is not on `core`, so it has no route to Postgres, `mcp` or `agent-proxy`, nor to any kernel network. It has no egress at run time.
- **Runtime hardening:** read-only root, `cap_drop: [ALL]`, `no-new-privileges`, a non-root uid, tmpfs `/tmp`, and `mem_limit == memswap_limit` from `spikes/embed-service`. It is not a `/run/secrets` consumer (ADR-0026) and holds no credential.
- **Model delivery:** the model files are baked into the image at build time. They come from a pinned Hugging Face revision, and every loaded file's sha256 is verified during the build, which fails on a mismatch. The build fetches from three sources: PyPI, the PyTorch CPU wheel index (download.pytorch.org) and huggingface.co. A first build therefore fails offline; later builds reuse the layer cache. At run time the service sets `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` and loads weights with `weights_only=True`. The [image] test checks this by behaviour: a pickle with a non-allow-listed global, loaded through the service's loader, is refused.
- **Image dependencies:** torch and sentence-transformers exist only in this image, as an optional extra of the server package, with hash-pinned versions (torch 2.15.0+cpu and sentence-transformers 6.3.0, as in the spike). The api and worker images do not carry them.
- **Startup:** no service `depends_on` embed. Callers wait for its `/health` (SRV-SEARCH), so the stack's start does not hinge on a multi-GB model load.

## Rejected options — why

- **(a)** The api cannot reach the model. It also needs about 2.27 GB more in the worker, where an OOM would also kill agent stages.
- **(b)** It puts a listener on the service that holds the DB DSN and runs agents.
- **(d)** Query-time embedding has no route to the host, and a listener there reverses ADR-0027.
- **(e)** The api cannot hold 2.27 GB in 1g. It is the edge-facing service, and passage jobs would load the request path.

## Residual risk

The service holds no secret and reaches no store, so a compromise of `embed` (through a crafted request, or a poisoned model file that slipped past the hashes) is about **retrieval integrity, not confidentiality**. It could return wrong vectors and so steer which chunks search returns to the learner, the compiler and the tutor. It cannot read the database, the tokens or other users' data. The model hashes and the `weights_only` loader are the controls. Compile's fact-check stage (ADR-0009) checks claims against the cited spans, not against the ranking.

## Consequences

- OPS-DEPLOY gets a paired change: the `embed` service and network, `deploy/images/embed.Dockerfile`, the new VEC-DEPLOY-2 reachability pairs, and the backup labels (the service has no volume).
- ADR-0012's stack list and ADR-0025 rule 5 get amendment notes (the owner's edit).
- SRV-SEARCH implements the service and its callers.

## Hypotheses (unmeasured)

- Measured since: the memory of the largest admitted request (8 texts) is 2,410 MiB peak RSS, so the limit is 3g (Context).
- The service's speed under the full stack's load matches the spike's 8-CPU numbers. The SRV-SEARCH bench records the job's wall time.
