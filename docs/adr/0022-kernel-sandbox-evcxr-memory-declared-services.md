---
id: ADR-0022
title: Kernel sandbox amendment — exec tmpfs and per-kernel evcxr cache with cleanup, enforced memory, a declared set of static kernel services with one principal or session each
status: proposed
date: 2026-11-20
verified_by: "OPS-SANDBOX tests: two concurrent Rust kernels each pass the pilot-crates cell; kernel dirs are removed after normal exit, SIGKILL, OOM and a gateway restart (launcher sweep), so a container starts more than 12 Rust kernels in sequence; a kernel-start failure (e.g. tmpfs full) reaches the client as an error; a 2.6 GB allocation kills the kernel and the client is told; routes match the declared set (api relay → kernel-<user>, kernel-pair-*; worker → kernel-verify, kernel-agent; nothing else); a session waits when the pair pool is exhausted; no service mounts the Docker socket; all of it re-run on python:3.14-slim-trixie"
revisit_when: "More than ≈ 12 concurrent Rust kernels are needed in one container (1,024 MiB tmpfs ÷ ≈ 85 MiB per kernel); or evcxr gains a read-only cache or an exec-free cell loader"
supersedes: []
---

## Context

ADR-0014 fixed the sandbox controls. `spikes/kernel-roundtrip` ran under them on 2026-11-20 (RESULT.md and `results/warm-{1..5}.json`, `cold-1.json` on main; deep-reviewer approved in round 2). It measured `python:3.13-slim-bookworm`; the re-run on `python:3.14-slim-trixie` (ADR-0020) is pending.

| Fact | Number | Source |
| --- | --- | --- |
| evcxr with `/tmp` `noexec` (Docker's tmpfs default) | the Rust kernel never becomes usable (`dlopen failed`) | RESULT.md finding 1 |
| evcxr cache on the read-only root | every dependency build fails (`Failed to wrap rustc: Read-only file system`) | finding 2 |
| Shared cache, two concurrent kernels | 4 of 14 concurrent pilot cells failed (*observed, not recorded*) | finding 2 |
| Per-kernel cache copy | 10 of 10 concurrent pilot cells pass over 5 runs | `results/warm-*.json` |
| Kernel directory after the kernel is deleted | ≈ 85–86 MiB each stays in `/run/evcxr`; the 13th kernel fails (`No space left on device`), with no error to the client (reviewer) | finding 3 |
| `--memory 2g` without `--memory-swap` | Docker allows 4 GiB; with `--memory-swap 2g`, a 2.6 GB allocation is OOM-killed (*observed, not recorded*) | RESULT.md controls table |
| OOM inside the gateway | the kernel dies, the container keeps running, and the gateway restarts the kernel silently, with state lost (reviewer, *observed, not recorded*) | RESULT.md controls table |
| Kernel Gateway auth | `auth_token` empty; reachable only on the internal network in the spike | finding 6 |
| Exec mount reach | `/run/evcxr` is writable and executable by uid 1000, i.e. every kernel; a python3 cell ran a dropped binary there (reviewer) | RESULT.md security section |

## Options

| Option | Gives | Costs (numbers) |
| --- | --- | --- |
| **A. One exec tmpfs for evcxr, per-kernel cache copy, cleanup by a sweeping launcher** | Rust labs work; concurrent kernels cannot break each other's builds | One writable and executable path for every kernel; ≈ 85 MiB of memory per live Rust kernel |
| B. Shared writable cache | 86 MiB once | Concurrent builds break (4 of 14 observed); one kernel can poison the next one's cache |
| C. No Rust kernel | Keeps "read-only root + noexec tmpfs" intact | ADR-0003's Rust wallet labs are lost |

## Decision

This amends ADR-0014's "Process", "Resources" and "Network" rows.

- **Process:**
  - Besides `/home/lab` and `/tmp` (`noexec`), one tmpfs `/run/evcxr` mounted with the options `exec,nosuid,nodev,size=1g` and owned by the kernel user.
  - A Rust kernel launcher (the kernelspec's argv) creates a private directory per kernel with a copy of the image's read-only cache, which is root-owned in the image.
  - **Before creating its own directory, the launcher sweeps the directories of kernels that are no longer running.** That covers every termination: normal exit, SIGKILL, OOM and a gateway restart.
  - Each directory carries an atomic liveness marker, the kernel's PID written by rename after the cache copy completes, so the sweep never removes a directory whose launcher is still copying.
  - Dead directories persist until the next Rust kernel starts in that container.
  - A kernel-start failure, such as a full tmpfs, is reported to the client as an error.
- **Resources:**
  - `--memory-swap` equals `--memory`.
  - The tmpfs sizes count against the limit and must sum below it.
  - After an OOM kill, the kernel relay tells the learner the kernel restarted and its state is gone.
- **Declared kernel services:** a fixed, declared set of static services, which `reader up` generates from `reader.yaml`. There is no spawner and no Docker socket anywhere.
  - `kernel-<user>`: one per configured user, for learner sessions;
  - `kernel-verify`: lab verification (ADR-0009), used by the worker only;
  - `kernel-agent`: agent tools under ADR-0025 option (a), used by the worker only;
  - `kernel-pair-1..N`: a bounded pair pool (ADR-0016), N from config, default 1.
- **Isolation invariant:** one principal or session per kernel container, from the declared set; a session waits when the pool is exhausted. Kernels in one container share a uid and can write each other's `/run/evcxr` directories, so they must never serve two principals.
- **Routes:**
  - the api relay reaches `kernel-<user>` and `kernel-pair-*`;
  - the worker reaches only `kernel-verify` and `kernel-agent`;
  - no other service reaches a kernel.
- **Network:** Kernel Gateway runs with an auth token, or is reachable only from its declared caller on the sandbox network, never published.

## Rejected options — why (numbers)

- **B.** 4 of 14 concurrent pilot cells failed (*observed, not recorded*); per-kernel copies passed 10 of 10.
- **C.** It removes the kernel that ADR-0003 chose for the pilot's Rust labs.

## Consequences

- **A deliberate weakening of ADR-0014:** any kernel can drop and run a native binary in `/run/evcxr`. The practical loss is small: rustc, cc, ld and Python's `ctypes` are in the image anyway, and caps, no-new-privileges and seccomp hold (reviewer: `CapEff=0`, `NoNewPrivs=1`, `Seccomp=2`).
- **Hypotheses:**
  - the sweeping launcher frees every dead kernel's directory: measured by the `verified_by` tests;
  - the numbers hold on 3.14 and trixie: the re-run.
- **Memory:** peak 1.0 GiB warm and 1.4 GiB cold with up to two Rust kernels (`results/*.json`). More concurrent Rust kernels need a larger limit, or fewer kernels per container.
- **Agent sandbox:** the agent sandbox and the provider credential are decided separately, in ADR-0025. `kernel-agent` has no route to `agent-proxy`.
- **ADR-0012 knock-on:** the Compose file gains the generated kernel services, and adding a user or pair slot means re-running `reader up`.

## Open questions

- None for the owner.
