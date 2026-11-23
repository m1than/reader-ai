---
id: ADR-0029
title: Browser upload of sources with host-side confined conversion — the api stores the PDF in an inbox on the blobs volume without parsing it; a dedicated host process (`reader-ingest --watch`) claims it and fetches the bytes over `docker exec` stdout, converts it under ADR-0027's unchanged profile and returns the artifact over the existing stdin receive (amends ADR-0027's Consequences "Ingestion … is started on the Mac")
status: proposed
date: 2026-11-23
verified_by: "SRV-INGEST [db] upload tests: a non-PDF, a truncated PDF, a body over the cap, a second concurrent upload and an upload over the inbox quota are refused with nothing left under <BLOBS_DIR>/inbox/; the same bytes uploaded twice give one inbox file and one upload row; no module of the api or worker image opens an inbox file other than through the byte-copy of `inbox fetch` (import-graph and source scan). SRV-INGEST [unit, fake runner] watch tests: the host converts only bytes whose sha256 equals the claimed upload's; a fetch stream over the cap, a sha256 that is not 64 lowercase hex, an unknown output line and a cancelled claim each stop the run with nothing sent; the host never opens a host path in watch mode. [local] INV-INGEST-4's probes hold for a converter started by the watcher. [live] e2e/live: a synthetic PDF uploaded in the browser reaches `ready` with the watcher running, stays `queued` with the watcher stopped and shows the 'converter offline' state, and a cancelled upload leaves no inbox file"
revisit_when: "Ingestion moves into Docker (ADR-0021's 51 s per formula bound is met in a container); or reader-ai leaves the owner's single Mac (ADR-0012: VPS), where no host CLI holds the Docker socket; or macOS removes sandbox-exec; or a second user role may upload"
supersedes: []
---

> Amends [ADR-0027](0027-native-ingest-transport-no-listener.md): its Consequences say "Ingestion of the slice is started on the Mac (`reader-ingest bashir-2025`). Browser upload waits for a later decision, which this ADR's `revisit_when` names." This is that decision. ADR-0027's Decision (no listener, the confined child, artifact over `docker exec` stdin into the worker) is unchanged. The amendment note on ADR-0027 is the owner's edit when this ADR is accepted.

## Context

**Owner request (2026-11-23):** a first step in which the learner imports a source in the browser, then says how long they want to study it. Decision 2 of that request: "upload in the browser. An 'Import source' page: drag-and-drop PDF → progress (conversion, indexing) → the study-time question → the course plan. The host-side sandboxed converter must stay isolated. The server queues the file, and the host-side converter picks it up."

**Facts that constrain the design:**
- **The converter is native and confined** (ADR-0021, accepted: Docker missed the 51 s per formula bound, 266–284 s measured). ADR-0027 (accepted) runs it as a child of a host command under a deny-default `sandbox-exec` profile: no network, no Unix sockets, no mach services, reads only its runtime, the models and its work directory, writes only that directory. Measured: 15.69–15.85 pages/min on the 20-page sample (spikes/ingest-confine), 65.5 pages/min on the full pilot with a child peak RSS of 4,025 MiB (SRV-INGEST NFR-INGEST-1).
- **Nothing on the host listens for the stack.** The worker is only on `internal: true` networks (OPS-DEPLOY), so it has no route to the host. ADR-0027 rejected a loopback listener (B), a host bind mount (C, against OPS-DEPLOY FR-DEPLOY-5) and a host command calling the api (D, a new long-lived api credential).
- **The api already mounts `blobs`** (deploy/compose.yaml, OPS-DEPLOY FR-DEPLOY-5: `blobs` on api and worker), so an upload can land on the same volume the worker reads, with no new mount.
- **The owner's Docker socket is the existing trust channel.** `reader` delivers secrets over `docker exec` stdin (ADR-0026), and `reader-ingest` sends artifacts the same way (ADR-0027).
- **The OPS-CLI watcher holds resolved secret values in memory for its lifetime** (OPS-CLI FR-CLI-8 step 4). A process that also manages a converter child would widen what that process does.
- **The pilot PDF is 20,922,693 bytes for 935 pages** (verified 2026-11-20 against plan.yaml's sha256).
- **ADR-0014** keeps code derived from uploaded material out of anything that holds secrets; ADR-0012 fixes v1 to the owner's single Mac with passkey sessions; ADR-0021 Options O1/O2 call parsing untrusted documents outside a container the cost of the native path.

## Options

### Upload

There is one sensible shape, so this part has no competing options: the owner's browser sends the PDF to the api through Caddy with the owner's session, and the api stores it without parsing it. The details are the Decision's.

### Host pickup: how the host learns of an upload and gets its bytes

| Option | Gives | Costs |
| --- | --- | --- |
| **P1. A dedicated host process `reader-ingest --watch` long-polls the worker over `docker exec`; it claims an upload, fetches its bytes on the exec's stdout, and sends the artifact back on the existing receive's stdin** | No listener, no new credential, no new mount; the same channel ADR-0027 already uses in the other direction. The process holds no secret. Every byte the host takes in is bounded and hash-checked against the claimed sha256 | One more long-lived host process (started by `reader up`, OPS-CLI paired change). A `docker exec` per poll (one per 25 s when idle) and per progress report (one per 30 s while converting) |
| P2. The OPS-CLI secret watcher also does the pickup | No new process | That process holds every resolved secret in memory (OPS-CLI FR-CLI-8). Running and supervising a converter of untrusted input from it widens the blast radius of a bug in the supervision code from "a converter child" to "a process with all secrets"; its lifecycle (restart on `reader up`, never re-resolving) also does not fit a 4 h conversion |
| P3. The host polls the api over HTTPS (`https://localhost/api/…`) | Uses the front door | The same objection as ADR-0027's D: a script cannot hold a passkey session, so it needs a new long-lived api credential, which any process of the owner's user could read and use against the edge-facing service |
| P4. A host directory mounted into the api as the inbox | The host reads files with no exec | A bind mount, which OPS-DEPLOY FR-DEPLOY-5 forbids. The host would read files that the browser-facing service wrote, through a path that service controls (symlinks, FIFOs, renames under the reader), with no transfer boundary; and the host directory becomes writable from inside a container |
| P5. `docker cp` from the worker's inbox path | No in-container command | The host parses a daemon-made tar of a path the container controls; there is no claim, lease or cancel, so two host runs could convert the same file, and progress still needs P1's channel |

### Who drives the queue

| Option | Gives | Costs |
| --- | --- | --- |
| **Q1. An `uploads` table with an explicit state machine and a lease, driven by the host's exec calls; the import and indexing after it stay SRV-EVENTS jobs (`ingest.import`, `search.embed`)** | The conversion is not a worker job (it runs on the host), so its states are honest: queued, claimed, converting, then the existing jobs. A lease recovers a host that died | One table and its transitions |
| Q2. Model the conversion as an SRV-EVENTS job | Reuses retries | A job is run by the worker; the worker would have to wait for an external process, holding a job slot and its timeout for up to 4 h, and procrastinate's heartbeat semantics do not cover a host that vanishes |

## Decision

**P1 with Q1.** In order:

1. **Upload** (api, SRV-INGEST):
   - **Route.** `POST /api/materials/uploads?filename=<display name>&language=<ru|en|other>` with the raw PDF as the body and `Content-Type: application/pdf`. Owner role only (any other user 403 `owner_only`, as SRV-INGEST FR-INGEST-15). SRV-AUTH FR-AUTH-12's Origin check applies unchanged; its JSON-only Content-Type rule gets one exception, this route with `application/pdf` (SRV-AUTH paired change). No multipart: the api runs no multipart parser on untrusted input.
   - **Size.** Caddy refuses a body over 200 MiB on this path (`request_body max_size`, OPS-DEPLOY paired change). The api enforces the same cap while streaming and never holds the body in memory: it writes 1 MiB chunks to a temporary file under `<BLOBS_DIR>/inbox/.tmp-<random>` while computing sha256, and stops at cap + 1 byte with 413 `too_large`. Reason for 200 MiB: a structural bound, about 10× the pilot (20.9 MB for 935 pages). The converted artifact stays under the existing 80 MiB cap independently of the PDF's size, because it holds text, not images.
   - **Magic check.** The first 8 bytes must match `%PDF-1.[0-7]` or `%PDF-2.0`, and the last 1,024 bytes must contain `%%EOF` (ISO 32000-2 7.5.2 and 7.5.5). Anything else is 415 `not_pdf`. This is the only inspection the stack does: **no service in the stack parses, renders or previews the PDF.** The browser does not preview it either.
   - **Dedupe by sha256.** If an upload or a declared material (reader.yaml) with that sha256 exists, the temporary file is removed and the answer is 200 with the existing upload or material; nothing is stored twice.
   - **Storage.** `fsync`, then rename to `<BLOBS_DIR>/inbox/<sha256>.pdf`, mode 0440, owned by the service uid. The path comes only from the server-computed sha256; the filename is display metadata (NFC, control characters removed, at most 200 characters) and never a path. The blobs volume holds data only: nothing in the stack executes or imports from it.
   - **Abuse bounds** (structural): one upload in flight per owner (a Postgres advisory lock; a second gets 409 `upload_busy`); at most 5 uploads not yet `ready`, `failed` or `cancelled`, and at most 1 GiB of inbox files in total (409 `inbox_full`).
   - **Row.** One `uploads` row: `{id, owner_id, sha256, size, filename, language, state: queued, attempts: 0, created_at}`.

2. **Host pickup** (`reader-ingest --watch`, SRV-INGEST; started by `reader up` as its own detached process with its own lock and no secrets, OPS-CLI paired change; also runnable in a terminal):
   - **Wait and claim.** It runs `docker exec -i <worker> python -I -m reader.ingest.inbox next --wait 25 --host-state <ready|setup_missing>`. Inside the worker, `next` records the host heartbeat, then waits on Postgres `LISTEN` for at most 25 s and atomically claims the oldest `queued` upload (`FOR UPDATE SKIP LOCKED`): state `claimed`, a fresh random claim token, a lease of 120 s. It prints one JSON line: the claim `{upload_id, sha256, size, claim}` or `{upload: null}`. A host whose converter is not set up (SRV-INGEST FR-INGEST-3 `setup_missing`/`setup_stale`) reports that state and claims nothing.
   - **Fetch.** `docker exec -i <worker> python -I -m reader.ingest.inbox fetch --upload-id <id> --claim <token>` writes the inbox file's bytes to stdout, only for a live claim. The host reads at most the claimed `size` bytes (and at most 200 MiB), straight into `<work>/in/material.pdf`, hashing as it reads.
   - **Confused deputy.** The host converts only if sha256 of what it read equals the claimed `sha256`, the byte count equals `size`, and the magic check passes again; otherwise it reports `fetch_mismatch` and stops. In watch mode the host never opens a host path given by the stack: its input is only the fetched bytes, and every argument it puts into an exec argv (upload id, sha256, claim) is validated against a fixed pattern first and passed as a list, never through a shell.
   - **Convert.** Exactly ADR-0027's child, profile and environment (SRV-INGEST FR-INGEST-4..10), plus two host-side watchdogs that the sandbox cannot provide: the child's session is killed when its resident memory exceeds 8 GiB (the pilot's measured 4,025 MiB peak × 2, sampled every 2 s with `ps`) or when the work directory exceeds 2 GiB (structural). The 4 h wall clock stays.
   - **Return.** The artifact goes over the existing `reader.ingest.receive` stdin (ADR-0027), which gains `--upload-id <id> --claim <token>`: receive accepts an uploaded material only while that upload is `converting` under that claim and its sha256 equals the artifact's `material_sha256`. The import job (`ingest.import`) and indexing (`search.embed`) then run as today.

3. **Confinement: unchanged.** The same `converter.sb` profile, child environment, realpath'd parameters and untrusted-WORK handling as ADR-0027 and SRV-INGEST FR-INGEST-4. The watcher adds no rule to the profile.

4. **Progress back to the UI.** The upload's displayed state is derived, never guessed:
   - from the `uploads` row: `queued` → `claimed` → `converting` (with `pages_done`/`pages_total` from the converter's validated progress lines, sent by the host with `inbox progress --upload-id --claim --pages-done n --pages-total m` at most once per 30 s and after each 50-page batch; each call renews the lease) → `sending`;
   - then from the jobs: `importing` while `ingest.import` is queued or running, `indexing` while `search.embed` is, `ready` when both succeeded;
   - terminal: `failed` with a code, or `cancelled`.
   The browser polls `GET /api/materials/uploads/{id}` (every 2 s while not terminal). Server-sent events are deferred with the rest of the outbox events (SRV-INGEST defers).

5. **Failure, retry and cancel.**
   - **Host lost.** A lease that expires (no `progress` for 120 s) returns the upload to `queued` with `attempts + 1`; after 3 attempts it is `failed` with `host_lost`.
   - **Deterministic failures** (`not_pdf` after fetch, `fetch_mismatch`, `convert_failed`, `memory_limit`, `work_limit`, `timeout`, a receive rejection) end the upload `failed` with that code and are **not** retried automatically: retrying a malicious or broken file only spends CPU. The owner's **Retry** returns a `failed` upload to `queued` with `attempts = 0`.
   - **Import and indexing failures** are SRV-EVENTS `dead` keys and keep their existing recovery (`--requeue`, `ensure`); the UI shows `failed` with the job's code.
   - **Cancel.** The owner may cancel an upload in `queued`, `claimed` or `converting`. The row becomes `cancelled` and the inbox file is deleted. The host's next `progress` or `fetch` call answers `{"cancel": true}`, and the host kills the child's session and removes its work directory. Receive refuses an artifact for a cancelled upload (`upload_cancelled`). After `sending`, cancel is not offered (deleting a material is deferred).
   - **Retention.** The inbox file of a `ready` upload is kept: it is the owner's original for the deferred re-ingest and export (roadmap SRV-EXPORT, "own uploads"). A `failed` upload's file is kept until Retry or Discard; Discard deletes it.

6. **When the host isn't running the watcher.** Nothing times out: the upload stays `queued` and the stack never tries to reach the host. The api reports the watcher as `online` while its last heartbeat (written by every `next`) is under 60 s old, else `offline`, and `setup_missing` when the host said so. The Import page shows the state and the one command that fixes it (`reader up`, or `reader-ingest --setup`). The CLI path (`reader-ingest <material id>` for reader.yaml materials) stays as it is, so the pilot keeps working without the watcher.

## Threat model

| Threat | Where it could act | Control |
| --- | --- | --- |
| **Malicious PDF** (an exploit in pdfium, Docling or torch) | Only in the converter child: the stack never parses the file, and the browser never renders it | ADR-0027's deny-default profile: no network, sockets or mach services, no exec beyond its Python, reads only its runtime, models and WORK, writes only WORK, no secrets in its environment. The parent treats WORK as untrusted (FR-INGEST-10) and validates the artifact twice (host, then receive) |
| **Decompression bomb** (Flate streams, huge page trees, object streams) | Converter child memory and disk | The 8 GiB RSS and 2 GiB WORK watchdogs; the 4 h wall clock; the existing artifact caps (80 MiB tar, 500,000 spans, 5,000 pages). sandbox-exec limits no resources, so these watchdogs are the control, not the profile |
| **Upload DoS** (huge, slow or many bodies) | api, Caddy, blobs volume | Owner-only route; Caddy's 200 MiB `max_size`; the streamed cap with no buffering; one upload in flight; 5 open uploads and 1 GiB of inbox; Caddy's request-body read timeout (OPS-DEPLOY) |
| **Path traversal** | api storage, host work directory, exec argv | The inbox name is the server-computed sha256; the filename is never a path; the host validates `^[0-9a-f]{64}$`, the upload id (UUID) and the claim (`^[0-9a-f]{32}$`) before building argv; the host's work name stays random (FR-INGEST-1) |
| **Confused deputy** (the host made to convert something the server did not queue, or a file of the owner's Mac) | Host watcher | The host converts only bytes it fetched for its own claim, whose sha256 and size equal the claim's; in watch mode it never opens a path from the stack. Receive accepts uploaded materials only for a live claim with a matching sha256 |
| **A compromised api or worker** | Feeds the host bytes or lines | Bytes are a malicious PDF (row 1). Lines are parsed against fixed JSON schemas, one line of at most 4 KiB per call; anything else stops the run. Such a container can already exec nothing on the host: `docker exec` runs from the host into the container, never the reverse, so no new capability is granted (ADR-0027 "No new capability") |
| **CSRF** | Upload route | FR-AUTH-12's Origin check; the session cookie is SameSite=Lax; the only non-JSON body type accepted is `application/pdf` on this one route |
| **Copyright and privacy** | Logs, events, the repository | Only sizes, hashes, counts, codes and timings are logged (SRV-INGEST INV-INGEST-5). Uploaded files live only on the blobs volume. A canary test covers the upload path |

## Rejected options — why

- **P2:** the pickup would run in the process that holds every resolved secret, for up to 4 h per conversion. P1 needs no secret.
- **P3:** a new long-lived api credential for a script, the reason ADR-0027 rejected its D.
- **P4:** a bind mount (OPS-DEPLOY FR-DEPLOY-5), and the host would read through a path a browser-facing container controls.
- **P5:** no claim or cancel, and a daemon-made tar of a container-controlled path to parse on the host.
- **Q2:** a worker job slot held for up to 4 h waiting on a process the worker cannot see.

## Consequences

- **SRV-INGEST** owns the upload route, the `uploads` table, the `inbox` commands (`next`, `fetch`, `progress`, `fail`), receive's claim check, `reader-ingest --watch`, and the two watchdogs.
- **Paired changes:** SRV-AUTH (FR-AUTH-12's Content-Type exception for one route), OPS-DEPLOY (Caddy `request_body` limit and read timeout on the upload path), OPS-CLI (`reader up` starts and `reader status` reports the ingest watcher; `reader down` stops it), SRV-SEARCH (an uploaded material's `fts_config` comes from its upload's language, since it has no reader.yaml entry).
- **The planner and the Import page** (SRV-PLAN, WEB-IMPORT) build on the upload's `ready` state.
- **Easier:** importing any PDF without editing reader.yaml or opening a terminal, once `reader up` runs.
- **Harder:** one more long-lived host process to supervise; the owner's Mac must be on, with the stack up, for a conversion to progress.
- **Forbidden:** any parser of the uploaded file inside the stack; any host listener; a host pickup that opens a path the stack names.
- **Hypotheses:** the 8 GiB memory and 2 GiB work-directory watchdogs are structural bounds from one measured peak (4,025 MiB on the pilot); a full run of the pilot under the watcher records the peak again (SRV-INGEST [local]).

## Open questions

1. Is 200 MiB the right upload cap for your sources (scanned books are larger per page than the born-digital pilot, and pages without a text layer are not OCR'd yet, SRV-INGEST defers)?
2. Should `reader up` start the ingest watcher by default, or only when reader.yaml sets `ingest.watch: true`? The draft starts it by default when the converter is set up and reports `setup_missing` otherwise.
