---
id: ADR-0025
title: Agent sandbox and credential separation — a credential-injecting egress proxy reachable only by the worker; tools by allow-list where a secret lives
status: accepted
date: 2026-11-20
verified_by: "SRV-AGENT/OPS-SANDBOX tests: in the worker and every kernel-* service, the provider token is absent from env, /proc/*/environ, /run/secrets, ~/.claude and `docker inspect`; kernel-agent cannot connect to agent-proxy; a request without a valid per-run handle gets no credential; user A's handle never yields user B's token; injection never applies to a non-provider host; the tool list the CLI reports at session start (init SystemMessage) equals the stage's declared set; no service mounts the Docker socket"
revisit_when: "The Agent SDK's CLI can authenticate through a proxy-only mechanism; or an agent stage needs a tool that the separation rules forbid"
supersedes: []
---

## Context

> Amendment proposed by [ADR-0039](0039-knowledge-layers-and-amendment-rules.md) (2026-11-26): a second worker service, `worker-judge`, joins `worker` on the agent-proxy network for the `judge.find` task only (rules 1–5 and `verified_by`): the caller's per-run handle issued after the reservation, no tools, one CLI spawned per call with no warm pool, at most 4 live tokens and never evicting a `worker` token; the api still never reaches agent-proxy. In force once ADR-0039 is accepted; then, where the two disagree, ADR-0039 wins.

ADR-0005 runs agent stages through the Agent SDK. ADR-0014 sandboxes learner code, and ADR-0022 declares the kernel services. `spikes/agent-sdk-container` (2026-11-20, RESULT.md on main, PR #6; deep-reviewer approved in round 3) found:

| Fact | Source |
| --- | --- |
| The SDK's bundled CLI authenticates from `CLAUDE_CODE_OAUTH_TOKEN` in its environment. With Bash allowed, the agent's tools run in the same container, and whether the CLI strips the token from tool processes was not tested. With open egress, an injected `env \| curl` has both the token and a route out | finding 1 |
| `docker run -e` puts the token in `Config.Env` (visible to `docker inspect`), in `/proc/<pid>/environ` and in the host `docker` process's environment | finding 2 |
| The CLI writes `~/.claude`. A tmpfs home on a read-only root is enough; the run used non-root, read-only root, cap-drop ALL and no-new-privileges | finding 3, "Sandboxed tools run" |
| The agent container must reach the provider API, unlike the kernel container | finding 4 |

An egress allowlist alone does not stop exfiltration: a stolen token can be sent to the provider host itself, which is allowed. **Separating the credential from code execution is the control.**

## Options

| Option | Gives | Costs |
| --- | --- | --- |
| (a) Agent tools run in the kernel sandbox, in the declared `kernel-agent` service (ADR-0022) | Reuses a hardened container; no new tool service | Tool calls go through the kernel relay; kernel limits apply to agent work |
| (b) A dedicated tool container per agent run | Agent tools isolated from kernels | One more service with its own limits |
| **(c) A credential-injecting egress proxy** | The token never enters the worker or any kernel. The proxy holds it (from `/run/secrets`, ADR-0023) and adds the auth header for the pinned provider hosts only | One more service, and a hypothesis: the CLI must accept a placeholder token plus a base URL or proxy |

## Decision

**(c) for the credential, combined with (a) for tools:**
- Agent stages that need code execution use `kernel-agent` through its relay.
- **(b)** is the fallback if the relay cannot serve agent tools.
- `kernel-agent` follows ADR-0022's rule for shared services: one run at a time, and a fresh container between runs.

**Rules:**
1. **Caller control.**
   - Only `worker` is on the `agent-proxy` network. No `kernel-*` service and no tool sandbox has a route to it.
   - The worker presents a **per-run handle** that maps to exactly one user's token (ADR-0005's invariant; ADR-0023 per-user delivery).
   - Injection happens only for the pinned provider hosts.
2. **Tools by allow-list.** In any container that holds a secret (the DB DSN, any `/run/secrets` file):
   - only the tools the stage declares are available;
   - every built-in tool is off by default;
   - examples the allow-list excludes unless declared: Bash, Read, Write, Edit, Glob, Grep, NotebookEdit, WebFetch, subagent tools and background-shell tools.

   A test reads the tool list the CLI reports at session start (the init `SystemMessage`) and asserts that it equals the declared set, so the rule fails closed when the SDK adds tools.
3. **Process controls:**
   - the worker (the agent-running container): non-root, read-only root, tmpfs home, cap-drop ALL, no-new-privileges;
   - `agent-proxy`: non-root, read-only root, cap-drop ALL, no-new-privileges.
4. **Compose services:**
   - `agent-proxy`: holds the provider tokens;
   - `worker`: runs agent stages; holds the DB DSN; tools by allow-list;
   - the declared kernel services of ADR-0022: no secrets.

   All are static Compose services. **No service mounts the Docker socket.**
5. **Routes and egress:**
   - the worker reaches `agent-proxy`, Postgres, Rotel (telemetry), `mcp`, `kernel-verify` and `kernel-agent`;
   - the api relay reaches `kernel-<user>` and `kernel-pair-*` (ADR-0022);
   - provider hosts are reached only through `agent-proxy`.
   - Web fetches for fact-checking are out of scope until SRV-PIPELINE. They will go through a **separate** web-fetch proxy with its own allowlist, never through the credential proxy. Credential injection never applies to non-provider hosts, and that is tested.
6. **ADR-0005's terms:** the proxy changes where the token sits, not whose requests it serves. Each user's runs still use only that user's credentials.

This amends ADR-0014 (an agent-sandbox row) and ADR-0005 (credential placement), and adds `agent-proxy` and the network topology to ADR-0012's Compose layout.

## Rejected options — why

- **(a) or (b) alone, with the token in the agent container.** Findings 1 and 2: the token sits next to code execution and is visible to `docker inspect`. An allowlist cannot stop exfiltration to the provider host itself.

## Consequences

- **Hypothesis 1:** the CLI authenticates through `agent-proxy` with a placeholder token and an overridden base URL or HTTPS proxy. Check: an SRV-AGENT owner live check.
  - **If it fails:** the fallback is that the worker holds the token file (`/run/secrets`) and gives the token only to the per-run environment of the CLI subprocess (ADR-0023). Rule 2 still applies, and the provider egress allowlist then applies to the worker.
- **Hypothesis 2:** the allow-list closes the read path to secrets. Check: tests that ask for `/run/secrets` and the environment, and assert that no tool can answer.
- **ADR-0012 knock-on:** new services and an internal network topology, specified by OPS-DEPLOY.

## Open questions

- None for the owner.
