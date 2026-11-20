---
id: ADR-0026
title: Secret delivery to read-only services — tmpfs /run/secrets filled by `reader up` over `docker exec` stdin, re-delivered on every start
status: proposed
date: 2026-11-20
verified_by: "OPS-DEPLOY config check: no top-level Compose `secrets`, no secret variable interpolated anywhere, a tmpfs /run/secrets (service uid, mode 0700) on exactly the secret-consuming services; stage-1 live check: after `reader up`, each consumer reads its file (0600, owner = service uid), `docker inspect` and `docker diff` of every container show no value and no file under /run/secrets, and after `docker compose restart <svc>` the file is re-delivered before the service reports healthy"
revisit_when: "Compose supports environment-sourced secrets on read-only services, or a host RAM-disk file source is measured to keep the value off every disk; or a secret-consuming service must restart without `reader up` running"
supersedes: []
---

## Context

ADR-0011 delivers resolved secrets as Compose secrets sourced from the `docker compose` child process's environment (`secrets: <name>: environment: <VAR>`), mounted at `/run/secrets/`, with "nothing written to disk". ADR-0023 (accepted) uses `/run/secrets` for the credential holder. ADR-0022 and ADR-0025 (accepted) require read-only root filesystems for kernel services, `agent-proxy` and `worker`.

Measured in `spikes/compose-secrets-ro` (2026-11-20, Docker 29.9.2, Compose 5.6.1, canary values only; RESULT.md):

| Variant | Outcome |
| --- | --- |
| (a) env-sourced Compose secret, `read_only: true` | Refused at `up`: "`file` is the sole supported option" |
| (b) env-sourced Compose secret, writable root | Works; not in `docker inspect`; **copied into the container's writable layer** (`docker diff`: `A /run/secrets/tok`), i.e. onto the Docker VM's disk |
| (c) `read_only: true`, tmpfs `/run/secrets` (service uid, 0700), value written after start with `docker exec -i … sh -c 'umask 077; cat > /run/secrets/<name>'` from stdin | Works; file 0600 owned by the service uid; not in `docker inspect`; not in the container layer; **lost on restart** |

So ADR-0011's mechanism cannot serve the read-only services ADR-0022 and ADR-0025 require, and its writable-root form breaks ADR-0011's own "nothing written to disk".

## Options

| Option | Gives | Costs (numbers) |
| --- | --- | --- |
| **A. (c): tmpfs `/run/secrets` per consuming service, filled by `reader up` over `docker exec` stdin after each start** | Read-only roots kept; value only in memory, 0600, out of `docker inspect` and off disk (measured) | Lost on every container restart (measured); `reader up` must re-deliver; a service restarted while `reader up` is not running stays without its secret |
| B. (b): writable root for secret consumers | Compose-native, survives restarts | Value on the VM disk (measured); drops ADR-0022/0025's read-only hardening for those services |
| C. File-sourced Compose secret from a host RAM disk | Compose-native, read-only roots | Not measured; a macOS RAM disk must be created and secured per run; restart behaviour unknown |

## Decision

Option A. Every service that consumes a resolved secret mounts a tmpfs at `/run/secrets` (owner = the service's uid, mode 0700, size ≤ 64 KiB). It has no Compose `secrets:` entries and no secret in `environment:`, `env_file:`, labels, command or healthcheck. After starting the stack, and after any start of such a service, `reader up` writes each value with `docker exec -i <container> sh -c 'umask 077; cat > /run/secrets/<name>'` from its own process memory over stdin. File names follow SRV-CONFIG's `secret_file_name(ref)`; the database password uses the fixed name `db-password`. A consuming service waits for its file before reporting healthy.

This replaces ADR-0011's delivery mechanism (Compose secrets from the child environment) and the "Compose secret under `/run/secrets`" wording in ADR-0023. Their store and holder decisions are unchanged: the Keychain is the store, and only the credential holder receives a token.

## Rejected options — why (numbers)

- **B (writable root).** The value was found in the container's writable layer (`docker diff`), so it sits on the VM disk, which ADR-0011 forbids. It would also give up the read-only root that ADR-0022 and ADR-0025 require.
- **C (host RAM disk).** Unmeasured; it adds a host resource to create, secure and clean on every run. It stays as the `revisit_when` alternative.

## Consequences

- OPS-CLI owns delivery and re-delivery: after `docker compose up`, and whenever a consuming container's start time changes, for example from a restart policy or a crash. While `reader up` is not running, a restarted consumer stays unhealthy until the next `reader up`. This fails closed, and the owner's stack is driven by `reader up` anyway.
- The `docker exec` is made by the host CLI through the owner's Docker socket. No service holds the socket (ADR-0022, ADR-0025 unchanged).
- postgres: the official image reads `POSTGRES_PASSWORD_FILE` at initdb, so the file must exist before init. The postgres entrypoint waits for `/run/secrets/db-password`. This is a hypothesis, checked in the stage-1 live check.
- OPS-DEPLOY's FR-DEPLOY-9 and the SRV-CONFIG `host-resolved` resolver read `/run/secrets/<name>` unchanged. Only the way the file gets there changes.

## Hypotheses (unmeasured)

- Re-delivery after a restart policy restart is fast enough that health checks do not flap. Checked in the stage-1 live check.
- The postgres entrypoint wait works with the official image's init. Checked in the stage-1 live check.
- Root inside a consumer could read the file, but no consumer runs a root process (cap-drop ALL, non-root user). Verified per service by OPS-DEPLOY's config check.

## Open questions

- None for the owner beyond accepting this ADR.
