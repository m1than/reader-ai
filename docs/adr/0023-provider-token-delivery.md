---
id: ADR-0023
title: Provider token delivery — Keychain as the store, /run/secrets into containers, never docker run -e
status: proposed
date: 2026-11-20
verified_by: "OPS-CLI invariant: while the stack runs, no container's `docker inspect` Config.Env contains a value resolved from a secret:// reference; an SRV-AGENT smoke test authenticates the Agent SDK from a /run/secrets file"
revisit_when: "The Claude Code CLI reads its OAuth token from a file natively; or `reader up` runs where no platform keyring exists"
supersedes: []
---

## Context

ADR-0011 already says `reader up` resolves `secret://` references on the host and that Compose maps them into containers as files under `/run/secrets/`. ADR-0005 says CI has no provider credentials. `spikes/agent-sdk-container` (2026-11-20) found three facts that the implementation must respect:

| Fact | Source |
| --- | --- |
| `docker run -e VAR` copies the value into the container's `Config.Env`, readable through `docker inspect` by anything with the Docker socket while the container runs. It is also in `/proc/<pid>/environ` and in the host `docker` process's environment | agent-sdk-container finding 2 |
| The Agent SDK's bundled CLI authenticates from `CLAUDE_CODE_OAUTH_TOKEN` in its environment; reading the token from a `/run/secrets` file through an entrypoint export was not tested | finding 2 |
| On the owner's Mac, a variable exported in `~/.zshrc` does not reach the commands the Claude Code agent runs (bootstrap's env check failed even after a restart), and the agent's sandbox denies reading environment variables. The Keychain item `reader-ai/claude-oauth-token` worked as the store, read straight into `docker run -e` by the spike's runner | progress.yaml S0.owner.machine, agent-sdk-container RESULT.md |
| GitHub Actions do not run on the owner's account (billing), so local checks are the gate (`tools/ci-local.sh`); no provider credential is needed for them | progress.yaml `ci_gate` |

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| **A. `/run/secrets` file → entrypoint exports the variable just before exec'ing the CLI** | Nothing in `Config.Env`; ADR-0011's path kept | The token is still in the CLI process's environment inside the container (unavoidable while the CLI reads only an env var) |
| B. `docker run -e` (the spike's method) | Simple; it worked | Fails the OPS-CLI `docker inspect` invariant; the token is visible to the Docker socket |
| C. `.env` file | Headless Linux fallback (ADR-0011) | Plain-text file on disk |

## Decision

This amends ADR-0011's container-delivery wording and ADR-0005's credential handling:
- **Store:** on the owner's Mac, the provider token's store is the Keychain (`secret://keychain/reader-ai/claude-oauth-token`), not a shell variable.
- **Delivery (option A):** `reader up` delivers it into containers only as a Compose secret (`/run/secrets/…`). The worker's entrypoint exports `CLAUDE_CODE_OAUTH_TOKEN` from that file immediately before starting the SDK process, and never passes it through `docker run -e` or `environment:`.
- **Spikes:** spike runners may read the Keychain item only where the owner allowed it per spike, so far agent-sdk-container, compile-cost and trace-backend. They are labelled as diverging from this rule.
- **CI:** CI, local or GitHub, never holds a provider credential (ADR-0005 unchanged).

## Rejected options — why

- **B.** It fails the OPS-CLI invariant by construction: the token is in `Config.Env` for the container's lifetime.
- **C.** It stays the headless fallback of ADR-0011; on the owner's Mac the Keychain exists.

## Consequences

- **Hypothesis:** the bundled CLI authenticates when its environment variable is set by an entrypoint from a `/run/secrets` file. It is measured by the SRV-AGENT smoke test named in `verified_by`.
- **Tools and the credential:** combined with ADR-0022, the agent's tools run where the exported variable is not visible.
- **`bootstrap-mac.sh`'s env check** reports FAIL on the owner's Mac by design. It should learn to check the Keychain item's presence instead, without reading it.

## Open questions

- None for the owner.
