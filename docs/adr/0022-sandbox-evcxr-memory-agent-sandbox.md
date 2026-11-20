---
id: ADR-0022
title: Sandbox amendment — exec tmpfs and per-kernel cache for evcxr, enforced memory, separate containers, and an agent sandbox distinct from the kernel sandbox
status: proposed
date: 2026-11-20
verified_by: "OPS-SANDBOX tests: two concurrent Rust kernels each pass the pilot-crates cell; per-kernel dirs are removed on kernel exit (13+ kernels in one container); a 2.6 GB allocation kills the kernel and the user sees it; the agent's tool process cannot read the provider credential; the agent container reaches only the API host"
revisit_when: "evcxr gains a read-only cache mode or an option to load cells without dlopen from a writable path; a kernel container hosts more than 12 Rust kernels in its lifetime"
supersedes: []
---

## Context

ADR-0014 fixed the sandbox controls. Two spikes ran under them on 2026-11-20:
- `spikes/kernel-roundtrip`: RESULT.md and `results/warm-{1..5}.json`, `cold-1.json`; reviewed by deep-reviewer, approved in round 2.
- `spikes/agent-sdk-container`: RESULT.md and `results/*`; approved in round 3.

| Fact | Number | Source |
| --- | --- | --- |
| evcxr with `/tmp` `noexec` (Docker tmpfs default) | the Rust kernel never becomes usable (`dlopen failed`) | kernel-roundtrip finding 1 |
| evcxr cache on the read-only root | every dependency build fails (`Failed to wrap rustc: Read-only file system`) | finding 2 |
| Shared evcxr cache, two concurrent kernels | 4 of 14 concurrent pilot cells failed (*observed, not recorded*) | finding 2 |
| Per-kernel cache copy | 10 of 10 concurrent pilot cells pass over 5 runs | `results/warm-*.json` |
| Leak per Rust kernel | 86 MiB left in `/run/evcxr`; the 13th kernel fails (`No space left on device`), with no error to the client | finding 3, reviewer |
| `--memory 2g` without `--memory-swap` | Docker allows 4 GiB; with `--memory-swap 2g` a 2.6 GB allocation is OOM-killed | RESULT.md controls table |
| OOM inside the gateway | the kernel dies, the container keeps running, and the gateway restarts the kernel silently, with state lost | reviewer, RESULT.md |
| Agent SDK in a container | the CLI writes `~/.claude`; a tmpfs home on a read-only root is enough; the agent's Bash ran under the process, capability and resource limits | agent-sdk-container "Sandboxed tools run" |
| Agent egress | the agent container must reach the Anthropic API, unlike the kernel container | agent-sdk-container finding 4 |
| Credential placement | the token sits in the environment of the container where the agent's Bash runs; whether the CLI strips it from tool processes was not tested | agent-sdk-container finding 1 |

## Options

| Option | Gives | Costs (numbers) |
| --- | --- | --- |
| **A. Exec tmpfs `/run/evcxr` + per-kernel cache copy with cleanup; separate containers; a distinct agent sandbox** | Rust labs work; concurrent kernels are isolated from each other's cache | One writable and executable path for every kernel (uid 1000); 86 MiB of memory per Rust kernel while it lives |
| B. Shared writable cache | 86 MiB once | Concurrent builds break (4 of 14 observed) and one kernel can poison another's cache |
| C. Drop the Rust kernel | Keeps "read-only root + noexec tmpfs" intact | The pilot's wallet core is Rust (ADR-0003) |

## Decision

This amends ADR-0014's "Process", "Resources", "Network" and "Secrets" rows, and adds an agent-sandbox row.

**Kernel sandbox:**
- **Process:** besides `/home/lab` and `/tmp` (`noexec`), one tmpfs `/run/evcxr` mounted `exec`, used only by the Rust kernel launcher. The launcher gives each kernel a private directory and a copy of the read-only image cache (root-owned), and **removes that directory when the kernel exits**. This needs a launcher that forwards signals; the spike's `exec` launcher leaks.
- **Resources:** `--memory-swap` equals `--memory`. The tmpfs sizes count against the limit and sum below it (spike: 256 + 256 + 1,024 MiB under 2 GiB). After an OOM kill, the kernel relay reports the restart to the learner, never silently.
- **Isolation per purpose:** lab verification (ADR-0009) and pair sessions (ADR-0016) each run in their own kernel container, never next to a learner's kernels. All kernels in one container share a uid and can write each other's `/run/evcxr` directories.

**Agent sandbox** (new; it differs from the kernel sandbox):
- non-root user, read-only root, tmpfs home;
- egress only to the API host, through an allowlist proxy;
- **the provider credential is out of reach of tool execution:** tools (Bash, file edits) run in a separate container or process that does not hold the credential. Never in the container whose environment has it.

## Rejected options — why (numbers)

- **B (shared cache).** 4 of 14 concurrent pilot cells failed (observed, not recorded); per-kernel copies passed 10 of 10.
- **C (no Rust).** It removes the kernel that ADR-0003 chose for the pilot's Rust wallet labs.

## Consequences

- **A deliberate weakening of ADR-0014:** a writable, executable path exists for every kernel, so dropping and running a native binary is possible. The practical loss is small, because rustc, cc, ld and Python's `ctypes` are in the image anyway, and caps, no-new-privileges and seccomp still hold (reviewer: `CapEff=0`, `NoNewPrivs=1`, `Seccomp=2`).
- **Hypotheses with their checks:**
  - signal-forwarding launcher cleanup holds over more than 12 kernels: OPS-SANDBOX test;
  - the CLI cannot be kept from exposing the credential to tools unless tools run elsewhere: SRV-AGENT test;
  - API-only egress through a proxy works with the SDK: OPS-SANDBOX test.
- **Kernel container memory:** peak 1.0 GiB warm and 1.4 GiB cold with up to two Rust kernels (kernel-roundtrip). More concurrent Rust kernels need a bigger limit or one container per session.

## Open questions

- Should pair sessions get one container per pair, or one per session?
