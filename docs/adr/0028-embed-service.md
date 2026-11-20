---
id: ADR-0028
title: Embedding service — bge-m3 in a dedicated internal `embed` service on its own internal network (api, worker, embed only); model baked into the image at build
status: proposed
date: 2026-11-21
verified_by: "OPS-DEPLOY config check: service `embed` joins only network `embed` (internal: true), whose members are exactly api, worker and embed; embed is not a /run/secrets consumer and has no secret in its environment; read-only root, cap_drop ALL, no-new-privileges, non-root, mem_limit == memswap_limit from spikes/embed-service; VEC-DEPLOY-2's reachability pairs include api→embed and worker→embed and exclude embed→postgres, embed→mcp, embed→agent-proxy, embed→any kernel and embed→internet. SRV-SEARCH [image] tests: the image build fails on a model-file hash mismatch; the service starts with HF_HUB_OFFLINE=1 and no network; weights load through torch.load with weights_only=True"
revisit_when: "A second embedding consumer outside api and worker appears; or the model no longer fits a bounded container on the owner's Mac; or local models move to the GX10 or MLX per ADR-0018"
supersedes: []
---

> Amends [ADR-0012](0012-deployment-and-auth.md) (the Compose stack and its networks gain `embed`) and [ADR-0025](0025-agent-sandbox-and-credential-separation.md) rule 5 (the worker's reachable set gains `embed`). Both amendments are notes for the owner to add when this ADR is accepted.

## Context

SRV-SEARCH (slice step V3) needs BAAI/bge-m3 dense vectors, which the owner confirmed on 2026-11-21 (spikes/embeddings-local/RESULT.md). The vectors are needed in two places:
- the worker embeds every chunk of an ingested material (job `search.embed`);
- the api embeds every search query at request time, and so do V.COMPILE-min and V.TUTOR-min in the worker.

Numbers:
- **Weights:** bge-m3's fp32 `pytorch_model.bin` is 2.27 GB (spike, Candidates). It is a pickle file, so it is loaded with `torch.load(..., weights_only=True)`, which refuses arbitrary objects.
- **Container limits** (deploy/compose.yaml): the worker has `mem_limit: 2g`, the api `1g`.
- **Speed:** 2.71 chunks/s with 13 torch threads in the Docker VM; the full pilot of 2,285 chunks took 841.74 s, or 14.03 min (results/20261120T232835-v2/BAAI__bge-m3.json).
- **Memory:** the service's peak RSS (model load plus the largest admitted request) is measured by `spikes/embed-service`. That number sets `mem_limit`.
- **Networks:** every stack network except `edge`/`egress` is `internal: true`. Since ADR-0027, nothing on the host listens for the stack.

## Options

| Option | Gives | Costs (numbers) |
| --- | --- | --- |
| (a) In the worker, with `mem_limit` raised | One process holds the model; passage embedding has no network hop | The worker needs about 2.27 GB more than its 2g limit for weights alone. The api cannot embed queries: the worker has no listener, and query embedding is on the request path. The model would share the worker's cgroup with agent stages, so an OOM in one kills the other |
| (b) In the worker, and the api calls the worker | Query embedding works with one model copy | The worker gains a listener on a network the api shares, while it holds the DB DSN and runs agent stages (ADR-0025 rule 2). It adds attack surface to the most privileged service. The memory cost is the same as (a) |
| **(c) A dedicated `embed` service** | api and worker both reach one model. The service holds no secret and no DB access, so a compromised model loader reaches nothing of value. It has its own memory limit | One more service, and an image of several GB because the model is baked in. Passage batches cross a network hop, which is negligible against about 0.37 s per chunk of compute |
| (d) Native on the host, as ingest does (ADR-0027) | Uses the Mac's CPUs directly | Query embedding happens at request time from containers on internal networks, which have no route to the host. A host listener is exactly what ADR-0027 removed. Only the passage half could move, and then two model builds would produce the vectors |

## Decision

**(c).** A dedicated service `embed`, image `reader-embed:local`, on its own network `embed` (`internal: true`), whose members are exactly `api`, `worker` and `embed`.
- **Network:** `embed` is not on `core`, so it has no route to Postgres, `mcp` or `agent-proxy`, nor to any kernel network. It has no egress at run time.
- **Runtime hardening:** read-only root, `cap_drop: [ALL]`, `no-new-privileges`, a non-root uid, tmpfs `/tmp`, and `mem_limit == memswap_limit` from `spikes/embed-service`. It is not a `/run/secrets` consumer (ADR-0026) and holds no credential.
- **Model delivery:** the model files are baked into the image at build time. They come from a pinned Hugging Face revision, and every loaded file's sha256 is verified during the build, which fails on a mismatch. Build-time network is the same as the pip installs every image already does. At run time the service sets `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` and loads weights with `weights_only=True`.
- **Image dependencies:** torch and sentence-transformers exist only in this image, as an optional extra of the server package, with hash-pinned versions (torch 2.15.0+cpu and sentence-transformers 6.3.0, as in the spike). The api and worker images do not carry them.
- **Startup:** no service `depends_on` embed. Callers wait for its `/health` (SRV-SEARCH), so the stack's start does not hinge on a multi-GB model load.

## Rejected options — why

- **(a)** The api cannot reach the model. It also needs about 2.27 GB more in the worker, where an OOM would also kill agent stages.
- **(b)** It puts a listener on the service that holds the DB DSN and runs agents.
- **(d)** Query-time embedding has no route to the host, and a listener there reverses ADR-0027.

## Consequences

- OPS-DEPLOY gets a paired change: the `embed` service and network, `deploy/images/embed.Dockerfile`, the new VEC-DEPLOY-2 reachability pairs, and the backup labels (the service has no volume).
- ADR-0012's stack list and ADR-0025 rule 5 get amendment notes (the owner's edit).
- SRV-SEARCH implements the service and its callers.

## Hypotheses (unmeasured)

- The service's peak RSS for the largest admitted request (32 texts of 1,200 characters, `max_seq_length` 512) fits a limit the Mac can spare. `spikes/embed-service` measures it.
- The rate measured in the spike's container (2.71 chunks/s) holds for the service, whose batches are smaller (8). The bench records the job's wall time.
