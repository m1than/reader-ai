---
id: ADR-0026
title: Secret delivery to read-only services — owner's choice between tmpfs filled over `docker exec` (A) and a file-sourced Compose secret (C); A recommended
status: proposed
date: 2026-11-20
verified_by: "For A: OPS-DEPLOY config check (no top-level Compose `secrets`, a tmpfs /run/secrets with the service uid and 0700 on exactly the consumers, `memswap_limit == mem_limit` on every consumer, no `${…}` of a secret variable anywhere in compose.yaml); stage-1 live check (each consumer's file is 0600 and owned by the service uid; for every container, `docker inspect`, `docker export`, `docker events` since `reader up` started, and the Docker Desktop logs contain no value; no path below /run/secrets/ in `docker diff`; after `docker compose restart <svc>` the file is re-delivered atomically before the service reports healthy; `up` succeeds with health dependencies). For C: OPS-DEPLOY config check (file-sourced secrets only; source directory 0700 and files 0600 under a per-run directory); stage-1 live check (the same value-absent checks, except that inspect shows the source path; the source directory is removed by `reader down` and by the next `reader up` after a crash)"
revisit_when: "Compose supports environment-sourced secrets on read-only services; or a host RAM-disk source is measured; or the stack must keep running across Docker restarts without `reader up` (favours C); or agents stop running as the owner's user (weakens the case against C)"
supersedes: []
---

## Context

ADR-0011 delivers resolved secrets as Compose secrets sourced from the `docker compose` child process's environment (`secrets: <name>: environment: <VAR>`), "nothing written to disk". ADR-0023 (accepted by the owner on 2026-11-20, PR #19) gives the token only to its holder, "through a Compose secret under `/run/secrets`". ADR-0022 and ADR-0025 (accepted by the owner on 2026-11-20, PR #19) require read-only roots for kernel services, `agent-proxy` and `worker`.

Measured in `spikes/compose-secrets-ro`, run `results/20261120T214345/`, canary values only. Every service was `user 1000`, `cap_drop ALL`, `no-new-privileges`.

| Variant | Result |
| --- | --- |
| (a) env-sourced secret, read-only root | **refused** at `up`: "`file` is the sole supported option" |
| (b) env-sourced secret, writable root | works, but the value is in the container layer (`docker diff`, `docker export`, also after `stop`) |
| (c) read-only root, tmpfs `/run/secrets` (uid, 0700), `memswap_limit == mem_limit`, value written over `docker exec -i` stdin to a temporary name and renamed | works: 0600 for the service uid; 0 value matches in inspect, config, export, events and Desktop logs; nothing in the layer; `memory.swap.max = 0`; **lost on restart** |
| (d) read-only root, **file-sourced** Compose secret, host source 0600 in a 0700 directory | works: 0600 for uid 1000; 0 value matches; only the bind mount point in the layer; **inspect shows the source path**; **survives restart** |
| (e) health dependency (`depends_on: service_healthy`) | (c) with delivery after `up`: `up` exits 1 after 4 s; (c) with delivery concurrent with `up`: exit 0; (d): exit 0 |
| naive vs atomic delivery, 20 trials with a reader waiting for the file | naive `cat >`: full value in 3 of 20; temporary name then `mv`: 20 of 20 |

Also measured:
- The Docker Desktop VM swaps to `/var/lib/swap` on its disk (890 MB in use during the run).
- Docker rejects a `noswap` tmpfs option.
- Root in a consumer cannot read the file under `cap_drop: [ALL]`.
- A 64 KiB tmpfs holds 16 small files.

ADR-0011's mechanism therefore cannot serve the required read-only services, and its writable-root form puts the value on the VM disk.

## Options

| Option | Gives | Costs (numbers from the spike) |
| --- | --- | --- |
| **A. tmpfs `/run/secrets` filled by `reader up` over `docker exec` stdin**, measured as (c) | Read-only roots; the value in no record checked and not in the container layer; not swappable inside the container with `memswap_limit == mem_limit` (`memory.swap.max = 0`); no file on the host | Lost on every container restart. `reader up` must stay running as a watcher: it delivers concurrently with `up` (delivering after `up` failed `up` in 4 s), re-delivers on every start, and holds every resolved secret in host memory for the stack's lifetime. The stack is down after a Docker restart or a reboot until the next `reader up`. Every consumer image needs `sh`, `cat` and `mv`. Delivery must be atomic (naive: 3 of 20 full reads) |
| **C. file-sourced Compose secret**, measured as (d) | Read-only roots; survives restart; no watcher, no wrapper; works with `up` and health dependencies as is (exit 0) | The value sits in a host file for the stack's lifetime (0600, 0700 directory; this Mac has FileVault on), readable by **any process running as the owner's user**, which includes coding agents. `docker inspect` shows the source path. A crash leaves the file until the next `reader up`/`down` removes it. ADR-0011's "nothing written to disk" no longer holds on the host. Per-secret `uid`/`mode` are not applied on Docker Desktop. A RAM-disk source is macOS-only and unmeasured |
| B. env-sourced secret, writable root, measured as (b) | Compose-native, survives restart | The value is in the container layer on the VM disk, also after `stop`; gives up the read-only roots ADR-0022/0025 require |
| Swarm secrets | Native tmpfs secrets on read-only services | A raft store on the VM disk; replaces Compose (ADR-0012); not measured |
| Named tmpfs volume | No host file | Lost on restart, mounted without `noexec` (reviewer's measurement); no gain over A |
| Init container or sidecar holding the secret | Restart-proof delivery inside the stack | An extra long-lived holder of every secret, or a Docker socket in a service (ADR-0022/0025 forbid it) |

## Decision

**The owner chooses between A and C. The recommendation is A.** The reason: under C, the provider token and the database password sit in a host file readable by every process of the owner's user. That includes the coding agents, which AGENTS.md forbids from reading secrets; the rule would then rest on the agent's restraint instead of on the file being absent. A keeps every value out of every file. Its costs are operational: a long-running `reader up` and a stack that needs `reader up` after a restart. On the owner's single Mac, `reader up` already drives the stack.

If A is chosen:
- Each consumer mounts a tmpfs `/run/secrets` (owner = the service uid, mode 0700, size ≥ 256 KiB) and sets `memswap_limit == mem_limit`. It has no Compose `secrets:` entries, and no secret in `environment:`, `env_file:`, labels, command or healthcheck.
- `reader up` stays running.
  - It delivers each value concurrently with `docker compose up` and again on every container start it observes: `docker events` start events, or polling.
  - Delivery runs as the service uid (`docker exec -u <uid> -i`): `umask 077; cat > /run/secrets/.<name>.tmp && mv /run/secrets/.<name>.tmp /run/secrets/<name>`, with the exit code checked.
  - The value travels only on stdin, never in argv.
- File names follow SRV-CONFIG's `secret_file_name(ref)`; the database password uses `db-password`.
- A consumer waits for the final name before reporting healthy.
- postgres runs as a non-root `user:`. Its entrypoint waits for `/run/secrets/db-password` only when PGDATA is empty, because the official image reads `POSTGRES_PASSWORD_FILE` at initdb (hypothesis, checked in the stage-1 live check).
- Kernel services stay secret-free (ADR-0022), so their restart-based reset needs no delivery.

If C is chosen:
- `reader up` writes each value to a per-run directory: 0700, outside every git work tree, removed by `reader down` and on the next `reader up` after a crash.
- Services declare file-sourced Compose secrets.
- AGENTS.md records that agents must not read that directory.

Either choice replaces ADR-0011's delivery mechanism and ADR-0023's "through a Compose secret under `/run/secrets`" wording. Both keep their store and holder decisions: the Keychain is the store, and only the credential holder receives a token. Under A, ADR-0011's "secrets stay in the OS store until start" and ADR-0023's residual-exposure line ("while it starts the stack") extend to the lifetime of the running `reader up`.

## Rejected options — why (numbers)

- **B:** the value was found in the container layer by `docker export`, also after `stop`, and it gives up read-only roots.
- **Swarm:** a raft store on disk and a platform change away from Compose (ADR-0012).
- **Named tmpfs volume:** the same restart loss as A, with no `noexec`.
- **Init container or sidecar:** a permanent extra holder, or a Docker socket in a service.

## Consequences

- **A:** OPS-CLI implements the watcher and atomic delivery; OPS-DEPLOY adds the tmpfs mounts and `memswap_limit` on consumers; consumer images keep `sh`, `cat` and `mv`; the stage-1 live check covers re-delivery after a restart and `up` with health dependencies.
- **C:** OPS-CLI manages the per-run directory and its cleanup; OPS-DEPLOY declares file-sourced secrets; the source path in `docker inspect` is accepted as metadata.
- Either way: the amendment notes in ADR-0011 and ADR-0023 point here, and the SRV-CONFIG spec's FR-CONFIG-14 sentence about `{ environment: <VAR> }` declarations and `secret_env_var` becomes obsolete (a paired spec and test change).

## Hypotheses (unmeasured)

- **A:** event-driven re-delivery keeps restarted consumers from flapping unhealthy. Checked in the stage-1 live check (the spike polled).
- **A:** the postgres entrypoint wait works with the official image's init, under a non-root user. Checked in the stage-1 live check.
- **C:** a RAM-disk source would remove the host-disk exposure. Not planned for v1.

## Open questions

- The owner chooses A or C.
