---
id: ADR-0023
title: Provider token delivery — the Keychain is the store; only the credential holder receives the token, through /run/secrets or a per-run subprocess environment
status: accepted
date: 2026-11-20
verified_by: "OPS-CLI invariant: while the stack runs, no container's `docker inspect` Config.Env holds a value resolved from a secret:// reference; an SRV-AGENT owner live check authenticates from the credential holder (ADR-0025) with the token delivered as specified here"
revisit_when: "The CLI reads its OAuth token from a file natively; or `reader up` runs where no platform keyring exists"
supersedes: []
---

> Proposed amendment: [ADR-0026](0026-secret-delivery-read-only-services.md) (2026-11-20) — "through a Compose secret under `/run/secrets`" is not possible for the read-only holder (Compose refuses environment-sourced secrets there). The holder receives the token through a tmpfs `/run/secrets` filled by a watching `reader up` over `docker exec` stdin (option A, chosen by the owner on 2026-11-20).

## Context

ADR-0011 already says:
- `reader up` resolves `secret://` references on the host;
- Compose maps the values into containers as files under `/run/secrets/`.

The roadmap (docs/roadmap.md, "Two budget facts", item 2, line 20) says CI has no provider credentials. `spikes/agent-sdk-container` (2026-11-20, RESULT.md on main) found:

| Fact | Source |
| --- | --- |
| `docker run -e VAR` copies the value into `Config.Env`, readable through `docker inspect` while the container runs; also in `/proc/<pid>/environ` and in the host `docker` process's environment | finding 2 |
| bootstrap's check for `CLAUDE_CODE_OAUTH_TOKEN` keeps failing in the agent's shell, "because the variable does not reach the agent's commands". The owner stored the token in the Keychain item `reader-ai/claude-oauth-token`, which the spike's runner read successfully | progress.yaml S0.owner.machine; agent-sdk-container RESULT.md |
| The owner allowed Keychain reads for these spike runners only: agent-sdk-container, compile-cost and trace-backend ("allow keychain for compile-cost and trace-backend", 2026-11-20) | progress.yaml on main, task notes of S0.spike.agent-sdk-container and S0.spike.compile-cost |

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **A. Deliver only to the credential holder: under ADR-0025(c) the proxy reads `/run/secrets`; otherwise the CLI subprocess gets the token in its per-run environment only** | No `Config.Env` exposure; the worker's own environment never holds the token | The token is in one process's environment during a run (fallback case) |
| B. Service-level `environment:`, `env_file:` or `docker run -e` | Simple; the spike's method | Fails the OPS-CLI invariant; visible to the Docker socket for the container's lifetime |

## Decision

- **Store:** the Keychain (`secret://keychain/reader-ai/claude-oauth-token`), per user.
- **Delivery (option A):**
  - only to the credential holder;
  - under ADR-0025(c), the holder is `agent-proxy`, through a Compose secret under `/run/secrets`;
  - in ADR-0025's fallback, it is the per-run environment of the CLI subprocess only, never the worker's own environment.
- **Never** service-level `environment:` or `env_file:`, and never `docker run -e`.
- **Per user:** each user's token is used only for that user's runs (ADR-0005's invariant, unchanged).

## Rejected options — why

- **B.** It fails the OPS-CLI invariant by construction.

## Consequences

- **Residual exposure:** the host `docker compose` process started by `reader up` holds the resolved values in its own environment while it starts the stack (ADR-0011), and the same user can read them with `ps eww`. This is accepted on a single-user Mac.
- **Hypothesis:** the CLI accepts its token only from an environment variable. Check: the SRV-AGENT owner live check. If a file mechanism exists, delivery moves to it.
- **`bootstrap-mac.sh`'s env check** fails on the owner's Mac by design. It should check the Keychain item's presence instead, without reading it.
- **CI** holds no provider credential: unchanged from the roadmap, so ADR-0005 needs no amendment for this.

## Open questions

- None. The owner answered on 2026-11-20, in chat: **yes**, an agent-run `reader up` may resolve the Keychain item for stage live checks, and the agent itself never reads it. Two AGENTS.md edits follow:
  - **Applied** on branch `0/S0.exit-adr-drafts`: "The owner's machine", the provider credential is the Keychain item `reader-ai/claude-oauth-token`, referenced as `secret://keychain/reader-ai/claude-oauth-token`.
  - **Added by the owner, 2026-11-20:** a new line under "Secrets": "The agent may run `reader up`, which resolves `secret://keychain/…` references without printing them; the agent itself never reads Keychain items." The agent's own edit of this secrets rule had been refused by the auto-mode classifier ("Undermining Oversight"), so the owner made it himself.
