---
id: ADR-0023
title: Provider token delivery — the Keychain is the store; only the credential holder receives the token, through /run/secrets or a per-run subprocess environment
status: proposed
date: 2026-11-20
verified_by: "OPS-CLI invariant: while the stack runs, no container's `docker inspect` Config.Env holds a value resolved from a secret:// reference; an SRV-AGENT owner live check authenticates from the credential holder (ADR-0025) with the token delivered as specified here"
revisit_when: "The CLI reads its OAuth token from a file natively; or `reader up` runs where no platform keyring exists"
supersedes: []
---

## Context

ADR-0011 already says:
- `reader up` resolves `secret://` references on the host;
- Compose maps the values into containers as files under `/run/secrets/`.

The roadmap (docs/roadmap.md, "Two budget facts", item 2, line 20) says CI has no provider credentials. `spikes/agent-sdk-container` (2026-11-20, RESULT.md on main) found:

| Fact | Source |
| --- | --- |
| `docker run -e VAR` copies the value into `Config.Env`, readable through `docker inspect` while the container runs; also in `/proc/<pid>/environ` and in the host `docker` process's environment | finding 2 |
| bootstrap's check for `CLAUDE_CODE_OAUTH_TOKEN` keeps failing in the agent's shell, "because the variable does not reach the agent's commands". The owner stored the token in the Keychain item `reader-ai/claude-oauth-token`, which the spike's runner read successfully | progress.yaml S0.owner.machine; agent-sdk-container RESULT.md |
| The owner allowed Keychain reads for these spike runners only: agent-sdk-container (progress.yaml on main), compile-cost and trace-backend (branch `0/S0.spike.compile-cost` @ 582f326, until it lands) | progress.yaml |

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

- **For the owner:** may `reader up` run by the agent, for stage live checks, resolve the Keychain item? Today AGENTS.md says the agent never reads Keychain items, and the provider token is read only by `reader up`. If yes, AGENTS.md needs these edits:
  - a new rule: "The agent may run `reader up`, which resolves `secret://keychain/…` references without printing them; the agent itself never reads Keychain items."
  - "The owner's machine", the "Provider credential" line, which today reads "`CLAUDE_CODE_OAUTH_TOKEN` in the environment, referenced as `secret://env/CLAUDE_CODE_OAUTH_TOKEN`", becomes: "Provider credential: the Keychain item `reader-ai/claude-oauth-token`, referenced as `secret://keychain/reader-ai/claude-oauth-token`."
