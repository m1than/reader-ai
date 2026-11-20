---
id: ADR-0014
title: Sandbox for lab execution — isolated kernel container, no secrets, egress denied by default
status: proposed
date: 2026-11-20
verified_by: "OPS-SANDBOX tests run inside the kernel: cannot resolve postgres or api, cannot reach the internet, cannot read /run/secrets, is killed at the memory and CPU limits; a disallowed host in a lab's allowlist is blocked"
revisit_when: "A lab legitimately needs a capability the sandbox denies, or a container escape CVE affects the runtime in use"
---

## Context

The kernel runs code written by the learner and by the agent, and the agent's code is derived from uploaded material that may contain prompt injection. Some labs need the network: the wallet pilot talks to Bitcoin testnet4 and Ethereum Sepolia endpoints.

## Decision

| Control | Setting |
| --- | --- |
| Process | Kernel Gateway runs as a non-root user; read-only root filesystem; `/home/lab` and `/tmp` on tmpfs |
| Capabilities | `cap_drop: [ALL]`, `no-new-privileges`, default seccomp profile |
| Resources | Per container: 2 CPUs, 2 GiB memory, 256 PIDs; per execution: wall-clock timeout 60 s (configurable per lab) |
| Network | Only the `sandbox` network; no route to `core` (Postgres) or to the internet |
| Egress | Labs with `network: allowlist` go through an egress proxy (Caddy forward-proxy or Squid) that allows only the hosts listed in the lab cell metadata and the course config |
| Secrets | None mounted. Labs that need an RPC key get a per-lab scoped value injected for that execution only, never a provider token |
| Data | The kernel sees only the lesson's lab files, copied in per session |
| Isolation upgrade | `runtime: runsc` (gVisor) is a config switch on Linux hosts that have it |

Prompt-injection guard in the pipeline: text from materials is data. Agent tools that execute code or fetch URLs run only in the sandbox; the agent in the pipeline has no shell on the api or worker host.

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **Hardened container + egress allowlist (+ gVisor where available)** | Runs on Docker Desktop (macOS, Windows) and Linux | Shares the host kernel unless gVisor is enabled |
| Firecracker microVMs | Strong isolation | Linux with KVM only; not available on Docker Desktop for macOS or Windows |
| Hosted sandboxes (E2B, Vercel Sandbox) | Isolation without ops | Material and learner code leave the self-hosted instance; per-minute cost |
| No sandbox (kernel next to the api) | Simplest | Learner and agent code can read secrets and the database |

## Rejected options — why (numbers)

- **Firecracker.** Runs on 1 of the 3 operating systems the owner requires.
- **Hosted sandboxes.** Contradicts self-hosting (ADR-0012): every lab sends code and data to a third party.
- **No sandbox.** 0 boundaries between injected material and provider credentials.
