---
id: ADR-0003
title: Notebook runtime — Jupyter kernel protocol through Kernel Gateway in a sandbox container
status: proposed
date: 2026-11-20
verified_by: "spikes/kernel-roundtrip: browser → api relay → Kernel Gateway executes a cell and streams outputs; e2e test in OPS-SANDBOX"
revisit_when: "Kernel Gateway has no release for 12 months, or a lab needs a kernel feature its API does not expose"
---

## Context

Owner decision, 2026-11-20: build on an existing kernel so that anything can be built on top of it. Labs in the pilot are Python today (reference computations for hashes, ECDSA, Nakamoto's attacker probability) and may need other kernels later (Rust through evcxr, TypeScript through Deno's Jupyter kernel).

The kernel executes code written by the learner and by the agent. It must run where it cannot read secrets or the database (ADR-0014).

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **Kernel Gateway (`jupyter-websocket` mode) in its own container** | Jupyter Server-compatible REST + WebSocket kernel API; any installed kernel; the browser library `@jupyterlab/services` speaks it; isolation is a container boundary | One more container; the api relays WebSockets for auth |
| `jupyter_client` inside the api process | No extra service | Kernels share the api container: labs could read api secrets and the database network |
| Full Jupyter Server | Kernels plus contents and terminals APIs | Terminals and contents APIs are attack surface reader-ai does not use |
| Pyodide / JupyterLite in the browser | No server-side execution at all | Python only, no native wheels for some crypto libraries, no shared execution for pair sessions (ADR-0016) |

## Decision

Run Jupyter Kernel Gateway in `jupyter-websocket` mode inside the `kernel` container built from `sandbox/`. The api authenticates the user, then relays the kernel WebSocket; the browser never talks to the gateway directly. Kernel Gateway is built on `jupyter_server`, so plain `jupyter_server` with only the kernels API is a configuration-level fallback with the same client protocol.

## Rejected options — why (numbers)

- **`jupyter_client` in the api.** Zero isolation layers between learner code and the process holding the provider credentials and database DSN, against one container boundary plus the controls in ADR-0014.
- **Full Jupyter Server.** Exposes 3 API groups (kernels, contents, terminals) where reader-ai needs 1; the other 2 add surface without function.
- **Pyodide.** Runs Python only; the pilot's Rust labs (the wallet core is Rust) and pair sessions on one shared kernel are impossible.

## Consequences

- Kernel images are declared per course in `reader.yaml` (`kernels: [python3, evcxr, deno]`) and built into the sandbox image.
- Kernel state is disposable: lessons must be re-runnable from top to bottom; lab verification (ADR-0009) checks exactly this.

## Open questions

1. Which kernels does v1 ship besides `python3`? Rust (evcxr) matches the wallet core; it adds build time and image size.
