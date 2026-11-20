# Spike: kernel-roundtrip — RESULT

**Question (docs/roadmap.md 0.3):** Kernel Gateway with python3, evcxr and deno: image size, cold start per kernel, evcxr first compile of the pilot crates; execute and stream outputs through a WebSocket client.
**Pass criterion:** each kernel executes and streams; evcxr warm compile ≤ 10 s.
**Verdict: PASS**, with sandbox changes stated under "Findings". Two of them weaken ADR-0014 and need the owner's decision.

Date: 2026-11-20. Command: `bash spikes/kernel-roundtrip/run.sh` builds the image, then does 5 warm runs and 1 cold run (`RUNS=5`). Every number below comes from `results/warm-{1..5}.json` and `results/cold-1.json`, except where marked *observed, not recorded*.

## Environment

| Item | Value |
| --- | --- |
| Host | Apple M5 Max, 36 GiB, macOS 27.2 |
| Docker | Docker Desktop, engine 29.9.2; VM 4 CPUs, 11.6 GiB |
| Image | `python:3.13-slim-bookworm`, linux/arm64; Kernel Gateway 3.0.1 (`jupyter_websocket`), ipykernel 7.4.0, rustc 1.100.0 + evcxr_jupyter 0.23.0, deno 2.9.8 |
| Pilot crates | sha2 0.11, hmac 0.13, pbkdf2 0.13, aes-gcm 0.11, k256 0.14 (ecdsa), hex 0.4: the wallet core of plan stages S02a/S02b (`pilot/bashir/plan.yaml`: SHA-256, HMAC, PBKDF2, AES-GCM; ECDSA in S02b) |
| Client | `client.py` in a separate container on the same internal network (Jupyter messaging 5.3 over the kernel WebSocket) |

### ADR-0014 controls: applied or not

| Control | In this spike |
| --- | --- |
| Non-root | Applied: uid 1000 (`USER lab`) |
| Read-only root, tmpfs home and /tmp | Applied: `--read-only`; `/home/lab` and `/tmp` are 256 MiB tmpfs, `noexec` (Docker default). An extra exec tmpfs was added; see finding 1 |
| `cap_drop: [ALL]`, `no-new-privileges`, default seccomp | Applied: the reviewer saw `CapEff=0`, `NoNewPrivs=1` and `Seccomp=2` in the container's `/proc/self/status` |
| 2 CPUs, 2 GiB, 256 PIDs | Applied: `--cpus 2 --memory 2g --memory-swap 2g --pids-limit 256`. Without `--memory-swap`, Docker allows 4 GiB with swap. With it, a 2.6 GB allocation is OOM-killed (*observed, not recorded*). A plain process exits 137. Inside the gateway, only the **kernel** dies: the container keeps running, and the gateway silently restarts the kernel with its state lost (seen by the reviewer). OPS-SANDBOX must report this to the learner |
| No route to the internet | Applied: the gateway is only on a `docker network create --internal` network. The python3 `egress` cell gets `URLError` in 0.06–0.07 s |
| Egress allowlist proxy | **Not applied** (no lab here needs the network) |
| Per-execution 60 s timeout | **Not applied** (the gateway has no such limit; it is the api relay's job) |
| No secrets, no route to Postgres/api | Not exercised: no secrets or other services exist in the spike |

## Numbers

Every cell's output is checked against an expected value, such as the SHA-256 of `abc`. All 90 cells in the 6 runs pass.

| Measure (5 warm runs, min–max) | python3 | rust (evcxr) | deno |
| --- | --- | --- | --- |
| Kernel start (POST → `kernel_info_reply`) | 0.19–0.77 s | 0.25–0.52 s | 0.18–0.65 s |
| First cell | 0.00–0.05 s (SHA-256) | 0.05–0.09 s (`println!`) | 0.04–0.05 s (SHA-256) |
| Pilot crates, warm (private copy of the pre-built cache) | — | **1.00–1.17 s** | — |
| Next cell using the crates | — | 0.08–0.11 s | — |
| Two Rust kernels at once, pilot crates each | — | 1.30–1.46 s; each keeps its own state | — |
| Streaming: 5 lines, 0.3 s apart (warm-1) | 0.04, 0.30, 0.61, 0.91, 1.21 s | 0.13, 0.43, 0.73, 1.03, 1.33 s | 0.04, 0.32, 0.62, 0.92, 1.22 s |

| Measure (cold run: empty evcxr cache, crates vendored, no network) | Value |
| --- | --- |
| Pilot crates, one kernel | 4.92 s |
| Pilot crates, two kernels at once | 9.97 s and 10.17 s (they share 2 CPUs) |

| Measure | Value |
| --- | --- |
| Image size | 1,905,968,091 bytes (1.91 GB). Largest layers (`docker history`): Rust toolchain + evcxr 620 MB, apt build tools 291 MB, Kernel Gateway + ipykernel 161 MB, evcxr cache + vendored crates 124 MB, deno 86 MB |
| Image build without layer cache | 313 s (*observed, not recorded*: first build of an earlier revision of this Dockerfile with the same toolchain layers). `build_s: 1` in `results/*.json` is a layer-cached build and is not a build time |
| Container memory, idle | 53.5–54.0 MiB |
| Container memory, peak over a run | 453–1023 MiB warm; 1404 MiB cold |
| `/run/evcxr` after a run (3 Rust kernels started and deleted) | 256 MiB warm, 194 MiB cold |

Streaming: outputs reach the client 0.3 s apart, the same spacing the cell sleeps, in all three kernels. Kernel Gateway forwards `stream` messages as they happen.

Warm is warm only because of the pre-built cache. cargo still walks the whole dependency graph (`Compiling …` for all 37 crates), and evcxr's rustc wrapper serves each unit from the cache. Every new Rust kernel pays about 1 s again. A crate version or feature set outside the cache costs the cold time.

## Findings that change later work

1. **evcxr needs an executable, writable directory.** Each cell becomes a `.so` that evcxr `dlopen`s. Docker's `--tmpfs` is `noexec` by default, and on a `noexec` mount the Rust kernel never becomes usable (repeated `dlopen failed`, then the WebSocket handshake times out). This spike adds one tmpfs, `/run/evcxr` (exec, 1 GiB). `/tmp` and `/home/lab` stay `noexec`.
2. **evcxr's compile cache must be writable and per kernel.**
   - The rustc wrapper writes to the cache even on hits. On the read-only root every dependency build fails.
   - A cache shared by two concurrent kernels breaks builds, at least intermittently: 4 of 14 concurrent pilot cells failed with `could not compile const-oid` / `num-traits` and no diagnostics (*observed, not recorded*; the reviewer's one shared-cache run passed). Before that, a shared temp dir (`EVCXR_TMPDIR`) also mixed the kernels' projects.
   - Fix: the rust kernelspec starts `evcxr-kernel.sh`. It makes a private dir under `/run/evcxr` with its own copy of the read-only pre-built cache (`/opt/evcxr-cache`, 86 MB) and sets `TMPDIR` and `XDG_CACHE_HOME` there. With it, 10 of 10 concurrent pilot cells passed.
3. **Per-kernel dirs are never cleaned up.** The launcher `exec`s evcxr, so nothing removes the dir when the kernel ends. Each Rust kernel leaves about 85 MiB (256 MiB after 3 kernels). The tmpfs counts against the 2 GiB memory limit, so a long-lived container fills up. The reviewer saw 12 Rust kernels start and the 13th fail (`cp: No space left on device`). The gateway's restart attempts then filled the 1 GiB tmpfs, and the kernel never became usable, with no error sent to the client. OPS-SANDBOX must either clean up on kernel exit (a launcher that forwards signals and removes the dir) or use one container per session.
4. **Crates are vendored at image build.** With no network, only crates in the vendored registry (`/opt/cargo-lab`) build. That includes transitive dependencies of the pre-built set: the reviewer saw `serde` build. Others fail in about 0.04 s (`no matching package`). `CARGO_NET_OFFLINE=true` is a convenience a cell can override, not a control. The control is the internal network. A course's crates must be declared when the image is built (ADR-0003 already says this).
5. **Kernel listing is off** (`GET /api/kernels` → 403). The api tracks kernel ids itself.
6. **The gateway has no auth** (`auth_token` empty). This is acceptable here: it is reachable only on the internal network. OPS-SANDBOX needs a token or network-only reachability from the api.
7. **`/opt/evcxr-cache` is owned by `lab`** in this image. Only `--read-only` protects it. In `sandbox/` it must be owned by root.
8. **The image is large** (1.91 GB). evcxr needs a linker at runtime (`gcc`, `libc6-dev`), not all of `build-essential`. The size saved by trimming is not measured. The `curl | sh` installers for rustup and deno are not pinned by checksum; `sandbox/` must pin them.

### Security consequences the owner should accept or reject (proposed ADR-0014 amendment, for S0.exit)

- **Exec tmpfs.** `/run/evcxr` is writable and executable by uid 1000, which is every kernel, not only evcxr. A python3 cell can drop a native binary there and run it; in `/tmp` the same is refused. The practical loss is small: rustc, cc and ld are in the image, Python has `ctypes`, and caps, no-new-privileges and seccomp still hold. It is still a deliberate weakening of "read-only root + noexec tmpfs".
- **Writable compile cache.** Per-kernel copies stop one Rust kernel from poisoning the cache of the next, because each copy comes from the read-only image. Code in any kernel still runs as the same uid, so it can write into another live kernel's dir in `/run/evcxr`. Lab verification (ADR-0009) and pair sessions (ADR-0016) should therefore run in their own container, not next to learner kernels.
- **Memory.** tmpfs sizes (256 + 256 + 1024 MiB) count against the 2 GiB limit. `--memory-swap` must equal `--memory`, or the limit is not enforced.

## Hands to stage 4 (OPS-SANDBOX, SRV-KERNEL)

- Kernel set confirmed: `python3`, `rust`, `deno`; each starts in under 0.8 s and streams.
- Pilot crates: about 1.1 s warm (target ≤ 10 s); 4.9 s cold; about 10 s cold when two kernels compile at once.
- Mounts: `/home/lab` and `/tmp` (noexec, 256 MiB), `/run/evcxr` (exec, 1 GiB), plus the rust launcher; per-kernel cleanup is still open (finding 3).
- Memory: peak 1.0 GiB warm and 1.4 GiB cold with up to two Rust kernels at once, against 2 GiB. More concurrent Rust kernels per container need a bigger limit or one container per session.
