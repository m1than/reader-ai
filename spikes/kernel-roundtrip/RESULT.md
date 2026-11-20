# Spike: kernel-roundtrip — RESULT

**Question (docs/roadmap.md 0.3):** Kernel Gateway with python3, evcxr and deno: image size, cold start per kernel, evcxr first compile of the pilot crates; execute and stream outputs through a WebSocket client.
**Pass criterion:** each kernel executes and streams; evcxr warm compile ≤ 10 s.
**Verdict: PASS.** It needs two sandbox details that ADR-0014 does not state yet (see "Findings that change later work").

Date: 2026-11-20. Command: `bash spikes/kernel-roundtrip/run.sh` (builds the image, runs it hardened, times all kernels with `client.py`; raw output in `results.json`).

## Environment

| Item | Value |
| --- | --- |
| Host | Apple M5 Max, 36 GiB, macOS 27.2 |
| Docker | Docker Desktop, engine 29.9.2; VM 4 CPUs, 11.6 GiB |
| Image | `python:3.13-slim-bookworm`, linux/arm64; Kernel Gateway 3.0.1 (`jupyter_websocket`), ipykernel 7.4.0, rustc 1.100.0 + evcxr_jupyter 0.23.0, deno 2.9.8 |
| Container | ADR-0014 limits: `--read-only`, tmpfs `/home/lab` and `/tmp`, `--cap-drop ALL`, `no-new-privileges`, 2 CPUs, 2 GiB, 256 PIDs, `CARGO_NET_OFFLINE=true` |
| Pilot crates | sha2 0.11, hmac 0.13, pbkdf2 0.13, aes-gcm 0.11, k256 0.14 (ecdsa), hex 0.4 — the wallet core of plan stage S02 (`pilot/bashir/plan.yaml`: SHA-256, HMAC, PBKDF2, AES-GCM) plus ECDSA |

## Numbers

Three runs; the table shows the range. Cell code is in `client.py`; every cell's output was checked against an expected value (SHA-256 of `abc`).

| Measure | python3 | rust (evcxr) | deno |
| --- | --- | --- | --- |
| Kernel start (POST → `kernel_info_reply`) | 0.23–0.76 s | 0.25–0.26 s | 0.24–0.72 s |
| First cell (SHA-256) | 0.04–0.05 s | 0.05–0.06 s (`println!`) | 0.04 s |
| Pilot crates, warm (pre-built cache) | — | **0.96–1.0 s** | — |
| Pilot crates, cold (empty cache, crates vendored, offline) | — | 8.2 s | — |
| Next cell using the crates | — | 0.07–0.08 s | — |
| Streaming: 5 lines, 0.3 s apart | arrive at 0.04, 0.31, 0.61, 0.91, 1.21 s | (not measured) | arrive at 0.04, 0.32, 0.62, 0.93, 1.23 s |

| Measure | Value |
| --- | --- |
| Image size | 1.91 GB. Largest layers: Rust toolchain + evcxr 620 MB, apt build tools 291 MB, Kernel Gateway + ipykernel 161 MB, evcxr cache + vendored crates 124 MB, deno 86 MB |
| Image build, first (no layer cache) | 313 s |
| Container memory, idle | 60–141 MiB |
| Container memory, peak over the run | 226–519 MiB of 2 GiB |

The outputs reach the client 0.3 s apart, the same spacing the code sleeps. So Kernel Gateway forwards `stream` messages as they happen, not when the cell ends.

## Findings that change later work

1. **evcxr needs an executable writable directory.** evcxr compiles each cell into a `.so` and `dlopen`s it. Docker mounts `--tmpfs` with `noexec` by default, and with `/tmp` noexec every Rust cell fails (`dlopen failed`). Fix used here: one dedicated tmpfs `/run/evcxr` mounted `exec`, `EVCXR_TMPDIR=/run/evcxr`. `/tmp` and `/home/lab` stay `noexec`. OPS-SANDBOX must declare this mount; ADR-0014's process row should name it.
2. **evcxr's compile cache must be writable.** evcxr wraps rustc with its own cache, which writes even on hits. With the cache on the read-only root filesystem every dependency build fails (`Failed to wrap rustc: Read-only file system`). Fix used here: the image ships the pre-built cache in `/opt/evcxr-cache` (86 MB), and the entrypoint copies it into `/run/evcxr` at container start. That copy is under 1 s and counts against the container's memory limit as tmpfs.
3. **Crates must be vendored at build time.** Egress is denied (ADR-0014), so `CARGO_HOME` with the downloaded registry is baked into the image. With `CARGO_NET_OFFLINE=true`, any crate outside the pre-built set fails, so a course's crates have to be declared at image build time. This fits ADR-0003 ("the sandbox image pre-builds the crates the pilot needs").
4. **Kernel listing is off by default** (`GET /api/kernels` → 403). That is fine: the api tracks kernel ids itself and relays by id.
5. **The image is large.** 1.91 GB, of which the Rust toolchain is a third. A multi-stage build that drops `build-essential` after compiling evcxr is not possible, because evcxr needs a linker at runtime. Expected saving from trimming rustup docs and the apt cache: about 100–150 MB (not measured).

## Hands to stage 4 (OPS-SANDBOX, SRV-KERNEL)

- Kernel set confirmed: `python3`, `rust`, `deno`, all starting in under 1 s.
- The pre-built pilot crates compile in about 1 s warm against the ≤ 10 s target, and 8.2 s cold.
- Mounts: tmpfs `/home/lab` (noexec), `/tmp` (noexec), `/run/evcxr` (exec, 1 GiB, evcxr only).
- Memory: under 0.6 GiB peak for one kernel of each kind, run in sequence. Concurrent kernels were not measured.
- Proposed ADR-0014 amendment (owner's decision): add the `/run/evcxr` exec tmpfs to the process row.
