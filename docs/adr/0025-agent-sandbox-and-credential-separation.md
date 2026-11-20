---
id: ADR-0025
title: Agent sandbox and credential separation — a credential-injecting egress proxy; agent tools never run where a secret lives
status: proposed
date: 2026-11-20
verified_by: "SRV-AGENT/OPS-SANDBOX tests: in the agent container the provider token is absent from env, /proc/*/environ, /run/secrets, ~/.claude and `docker inspect`; requests to provider hosts succeed only through the proxy; a tool call cannot read any secret of the container that holds one (DB DSN included); no service mounts the Docker socket"
revisit_when: "The Agent SDK's CLI can authenticate through a proxy-only mechanism officially; or an agent stage needs a tool that the separation rules forbid"
supersedes: []
---

## Context

ADR-0005 runs agent stages through the Agent SDK. ADR-0014 sandboxes learner code. `spikes/agent-sdk-container` (2026-11-20, RESULT.md on main, PR #6; deep-reviewer approved in round 3) found:

| Fact | Source |
| --- | --- |
| The SDK's bundled CLI authenticates from `CLAUDE_CODE_OAUTH_TOKEN` in its environment. With Bash allowed, the agent's tools run in the same container, and whether the CLI strips the token from tool processes was not tested. With open egress, an injected `env \| curl` has both the token and a route out | finding 1 |
| `docker run -e` puts the token in `Config.Env` (visible to `docker inspect`), in `/proc/<pid>/environ` and in the host `docker` process's environment | finding 2 |
| The CLI writes `~/.claude`; a tmpfs home on a read-only root is enough | finding 3 |
| The agent container must reach the provider API, unlike the kernel container | finding 4 |

An egress allowlist alone does not stop exfiltration: a stolen token can be sent to the provider host itself, which is allowed. **Separating the credential from code execution is the control.**

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| (a) The agent's tools run in the existing kernel sandbox (ADR-0014/0022) | Reuses a hardened container; no new service | Tool calls go through the kernel relay; the kernel's limits apply to agent work |
| (b) A dedicated tool container per agent run | Agent tools isolated from learner kernels | One more service, with its own limits to maintain |
| **(c) A credential-injecting egress proxy** | The token never enters the agent container. The proxy holds it (from `/run/secrets`, ADR-0023) and adds the auth header for provider hosts only | One more service, and a hypothesis: the CLI must accept a placeholder token plus a base URL or proxy |

## Decision

**(c) for the credential, combined with (a) for tools:**
- Agent stages that need code execution use the kernel sandbox through its relay.
- **(b)** is the fallback if the kernel relay cannot serve agent tools.

**Rules:**
1. **Separation:** in any container that holds a secret (provider token, DB DSN, any `/run/secrets` file), every built-in tool that executes code or touches the local filesystem is disabled: Bash, Read, Write, Edit, Glob, Grep, NotebookEdit and their successors. This is enforced through `disallowed_tools`, and checked in tests.
2. **The agent container** holds no provider token. The proxy holds it, and the agent container's own other secrets, such as the DB DSN, fall under rule 1.
3. **Compose services:**
   - `agent-proxy`: holds the provider token and adds the auth header on provider hosts only;
   - `worker`: runs agent stages; holds the DB DSN; built-in tools off;
   - `kernel`: ADR-0014/0022; holds no secrets.

   All are static Compose services. **No service mounts the Docker socket.**
4. **Egress:**
   - provider hosts only through `agent-proxy`;
   - internal routes to Rotel (telemetry), Postgres, `kernel` (tool relay) and `mcp`.
   - Web fetches for fact-checking are out of scope until SRV-PIPELINE. They will go through the proxy with their own allowlist.

This amends ADR-0014 (an agent-sandbox row) and ADR-0005 (credential placement), and adds the `agent-proxy` service to ADR-0012's Compose layout.

## Rejected options — why

- **(a) or (b) alone, with the token in the agent container.** Findings 1 and 2: the token sits next to code execution and is visible to `docker inspect`. An allowlist cannot stop exfiltration to the provider host itself.

## Consequences

- **Hypothesis 1:** the CLI authenticates through `agent-proxy` with a placeholder token and an overridden base URL or HTTPS proxy. Check: an SRV-AGENT smoke test. If it fails, the token falls back to the per-run environment of the CLI subprocess only (ADR-0023), with rule 1 still applied.
- **Hypothesis 2:** disabling built-in tools in the SDK closes the read path to secrets. Check: tests that ask for `/run/secrets` and the environment and assert refusal.
- **ADR-0012 knock-on:** a new Compose service and an internal network topology, specified by OPS-DEPLOY.

## Open questions

- None for the owner.
