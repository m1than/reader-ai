---
id: ADR-0027
title: Native ingestion transport — the confined converter is a child of a host command with no listener; its artifact enters the stack over `docker exec` stdin into the worker (amends ADR-0021 Decision item 3)
status: proposed
date: 2026-11-21
verified_by: "SRV-INGEST [local] confinement check on the owner's Mac (the converter child under its sandbox profile converts the 20 pilot pages; outbound TCP, loopback TCP and the Docker socket are refused; reads outside its input, model and runtime directories and writes outside its output directory are refused; its environment equals the allow-list; it holds no listening socket); SRV-INGEST receive tests (malformed artifacts write nothing and enqueue nothing)"
revisit_when: "Ingestion moves into Docker (a Docker run meets ADR-0021's 51 s per formula bound); or materials must be ingested from the browser without a host command; or macOS removes sandbox-exec"
supersedes: []
---

## Context

ADR-0021 (accepted, 2026-11-20) chose a native host converter once both M1 runs missed its bound: 284.1 s per formula at 4 VM CPUs and 266.0 s at 13, against ≤ 51 s (`spikes/ingest-docling/RESULT.md`, PR #24). Its Decision item 3 gives the mechanism: the converter "receives one PDF and returns the converted document over `127.0.0.1`", the worker in Compose keeps jobs, spans and storage, and the converter "listens on loopback only". It also notes that any local process can submit to a loopback listener, so SRV-INGEST would have to authenticate the worker.

Two accepted facts make that mechanism unworkable as written:
- **No path from the worker to the host.** OPS-DEPLOY (approved, merged in PR #27) puts the worker only on `internal: true` networks (`core`, `agentnet`, `sb-verify`, `sb-agent`). An internal network has no route to the host, so the worker cannot reach a host loopback listener. Adding one means an egress path from the worker, which holds the database password.
- **Authenticating a loopback listener needs a shared secret.** That secret would have to reach both the worker (ADR-0026 delivery) and the host converter, which ADR-0021 requires to run without secrets.

The owner's single Mac already runs `reader`, the host CLI, with the owner's Docker socket. It delivers secrets into containers over `docker exec` stdin (ADR-0026).

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **A. Host command, confined child, artifact over `docker exec` stdin** | No listener and no network at all for the converter; no new path into the stack; no shared secret; the worker validates everything it receives | Ingestion starts from a host command (`reader-ingest <material>`), not from the browser; one more use of the owner's Docker socket by a `reader` command |
| B. Loopback listener as ADR-0021 states | The browser could trigger ingestion | Needs worker egress to the host and a secret shared between the worker and the converter; a listener any local process can reach |
| C. Shared bind mount between host and worker | No exec | A host directory bound into the worker, which OPS-DEPLOY FR-DEPLOY-5 forbids; the worker reads files a host process wrote without a transfer boundary |

## Decision

**A.** The native converter runs as a child process of the host command `reader-ingest`, confined by a macOS sandbox profile (`sandbox-exec`). The child reads only its input PDF, the pre-fetched models and its runtime, writes only its output directory, has no network (no listener, no outbound, no Unix sockets), and gets an allow-listed environment with no secrets.

The parent validates the output, then streams it as a tar over stdin into `docker compose exec -T worker python -I -m reader.ingest.receive`. The worker validates it again, stores it content-addressed on the `blobs` volume (ADR-0007), and enqueues the import job (SRV-EVENTS). Everything else in ADR-0021 stands: formula model on, code model off, code lines from the text layer, the deterministic page check, `≥ 10 pages/min` for a native full run.

This replaces only Decision item 3's transport ("over `127.0.0.1`", "listens on loopback only") and its loopback-authentication note. "No database access and no blob volume" for the converter still holds.

## Rejected options — why

- **B:** an egress path from the worker, which holds the database password, plus a new shared secret, against an option that needs neither.
- **C:** contradicts OPS-DEPLOY FR-DEPLOY-5 (no bind mounts except the reader.yaml override) and removes the validation boundary.

## Consequences

- SRV-INGEST owns the host command, the sandbox profile, the receive command and the import job.
- Ingestion of the slice is started on the Mac (`reader-ingest bashir-2025`). Browser upload waits for a later decision; this ADR's `revisit_when` names it.
- **Hypothesis:** `sandbox-exec` with a deny-by-default profile can run Docling 2.151.1 on Python 3.14 at the native speed ADR-0021 measured. The spike `spikes/ingest-confine` measures this before SRV-INGEST's Green. If `sandbox-exec` cannot run it, the fallback is ADR-0021's other confinement, a dedicated macOS user, which needs the owner's one-time `sudo`.
- Any process with the owner's Docker socket can already exec into the worker (ADR-0026). The receive command adds no capability: it accepts only validated artifacts for materials declared in reader.yaml.

## Open questions

- None beyond the hypothesis above. The owner decides the status.
