---
id: ADR-0027
title: Native ingestion transport — the confined converter is a child of a host command with no listener; its artifact enters the stack over `docker exec` stdin into the worker (amends ADR-0021 Decision item 3)
status: proposed
date: 2026-11-21
verified_by: "SRV-INGEST [local] confinement check on the owner's Mac. The converter child under its sandbox profile converts the 20 pilot pages, and these attempts are refused: outbound TCP, loopback TCP, `host.docker.internal`, every Unix-socket connect or bind (the Docker socket included), mach lookups, exec of other binaries, reads outside its input copy, models and runtime, and writes outside its work directory. Its environment equals the allow-list, and it holds no listening socket. SRV-INGEST receive tests: malformed artifacts write nothing and enqueue nothing"
revisit_when: "Ingestion moves into Docker (a Docker run meets ADR-0021's 51 s per formula bound); or materials must be ingested from the browser without a host command; or macOS removes sandbox-exec"
supersedes: []
---

## Context

ADR-0021 (accepted, 2026-11-20) chose a native host converter after both M1 runs missed its bound of ≤ 51 s per formula: 284.1 s per formula at 4 VM CPUs and 266.0 s at 13 (`spikes/ingest-docling/RESULT.md`, PR #24). Its Decision item 3 gives the mechanism:
- the converter "receives one PDF and returns the converted document over `127.0.0.1`";
- the worker in Compose keeps jobs, spans and storage;
- the converter "listens on loopback only".
It also notes that any local process can submit to a loopback listener, so SRV-INGEST would have to authenticate the worker.

Two facts weigh against that mechanism:
- **No path from the worker to the host.** OPS-DEPLOY (approved, merged in PR #27) puts the worker only on `internal: true` networks (`core`, `agentnet`, `sb-verify`, `sb-agent`). An internal network has no route to the host, so the worker cannot reach a host loopback listener. Adding one means an egress path from the worker, which holds the database password.
- **Authenticating a loopback listener is extra machinery.** It needs a credential that both the worker and the host side hold. The worker can receive one through ADR-0026, and a host process other than the confined converter could hold it, so this is possible. It still adds a credential, its rotation, and a listener that every local process can reach, while a channel already exists.

The owner's single Mac already runs `reader`, the host CLI, with the owner's Docker socket. It delivers secrets into containers over `docker exec` stdin (ADR-0026).

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **A. Host command, confined child, artifact over `docker exec` stdin** | No listener and no network at all for the converter; no new path into the stack; no new credential; the worker validates everything it receives | Ingestion starts from a host command (`reader-ingest <material>`), not from the browser; one more use of the owner's Docker socket by a `reader` command |
| B. Loopback listener as ADR-0021 states | The browser could trigger ingestion | Needs worker egress to the host, a credential held by the worker and a host process, and a listener any local process can reach |
| C. Shared bind mount between host and worker | No exec | A host directory bound into the worker, which OPS-DEPLOY FR-DEPLOY-5 forbids; the worker reads files a host process wrote without a transfer boundary |
| D. Upload through Caddy to the api (`https://localhost/api/…`) | Uses the existing front door | The host command needs an api credential: passkey sessions cannot be scripted, so it would be a new long-lived CLI token, plus an upload route with its own size and abuse handling |

## Decision

**A.** The native converter runs as a child process of the host command `reader-ingest`, confined by a deny-by-default macOS sandbox profile (`sandbox-exec`), on CPU only.
- **Reads:** only a private copy of its input PDF, the pre-fetched models and its runtime.
- **Writes:** only its work directory.
- **Network:** none. No listener, no outbound connection, no Unix-socket connect or bind, no mach services.
- **Environment:** an allow-list with no secrets.

The parent validates the output, then streams it as a tar over stdin into `docker exec -i <worker container> python -I -m reader.ingest.receive`. The worker container is resolved by its Compose labels. The worker validates the tar again, stores it content-addressed on the `blobs` volume (ADR-0007), and enqueues the import job (SRV-EVENTS).

Everything else in ADR-0021 stands:
- formula model on, code model off;
- code lines from the text layer;
- the deterministic page check;
- `≥ 10 pages/min` for a native full run.

This replaces only Decision item 3's transport ("over `127.0.0.1`", "listens on loopback only") and its loopback-authentication note. "No database access and no blob volume" for the converter still holds.

## Rejected options — why

- **B:** it needs an egress path from the worker, which holds the database password, plus a new credential and a listener reachable by every local process. A needs none of these.
- **C:** it contradicts OPS-DEPLOY FR-DEPLOY-5 (no bind mounts except the reader.yaml override) and removes the validation boundary.
- **D:** it needs a new long-lived api credential for a script and a new upload route. A needs neither and reuses ADR-0026's channel.

## Consequences

**Ownership and use:**
- SRV-INGEST owns the host command, the sandbox profile, the receive command and the import job.
- Ingestion of the slice is started on the Mac (`reader-ingest bashir-2025`). Browser upload waits for a later decision, which this ADR's `revisit_when` names.

**Confinement:**
- **Measured** (`spikes/ingest-confine`, RESULT.md and `converter.sb`): a deny-by-default `sandbox-exec` profile runs Docling 2.151.1 configuration D on Python 3.14, on CPU, at native speed.
  - Throughput on the 20 pilot pages: 15.69–15.85 pages/min confined, against 15.85–16.57 unconfined. The output is identical: 6 formulas and 11 tables.
  - Every negative probe was denied: TCP and Unix sockets (the Docker socket included), mach lookups, exec of other binaries, signalling the parent, `/dev/tty`, reads of the home directory, and writes outside WORK.
  - The dedicated-macOS-user fallback is not needed.
- **Planned live check:** `e2e/live/slice.sh` probes `host.docker.internal` from the confined child against the running stack. That is the one network path the spike could not test without `reader up`.
- **CPU only.** Docling's layout and table models could use MPS, but MPS would need Metal mach services inside the sandbox. The formula model, which dominates the time, is CPU-only anyway (spike ingest-docling).
- **No new capability.** Any process with the owner's Docker socket can already exec into the worker (ADR-0026). The receive command adds nothing to that: it accepts only validated artifacts for materials declared in reader.yaml.

**Paperwork:** if this ADR is accepted, ADR-0021 needs an amendment note on Decision item 3 pointing here. That edit is the owner's.

## Open questions

- None. The owner accepted this decision in chat on 2026-11-21 and sets the status line.
