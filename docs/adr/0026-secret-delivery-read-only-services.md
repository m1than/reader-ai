---
id: ADR-0026
title: Secret delivery to read-only services — tmpfs /run/secrets filled by a watching `reader up` over `docker exec` stdin (A, chosen by the owner); file-sourced Compose secret (C) rejected
status: proposed
date: 2026-11-20
verified_by: "OPS-DEPLOY config check: no top-level Compose `secrets`; a tmpfs /run/secrets with the service uid and 0700 on exactly the consumers; a `mem_limit` and `memswap_limit == mem_limit` on every consumer; no `${…}` of a secret variable anywhere in compose.yaml. Stage-1 live check: each consumer's file is 0600 and owned by the service uid; for every container, `docker inspect`, `docker export`, `docker events` since `reader up` started, and the Docker Desktop logs contain no value; no path below /run/secrets/ in `docker diff`; after `docker compose restart <svc>` the file is re-delivered atomically before the service reports healthy; `up` succeeds with health dependencies; after the watcher stops, a restarted consumer stays unhealthy"
revisit_when: "Compose supports environment-sourced secrets on read-only services; or a host RAM-disk source is measured; or the stack must keep running across Docker restarts without `reader up`"
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
| (e) health dependency (`depends_on: service_healthy`) | (c) with delivery after `up`: `up` exits 1 after 4 s (with the spike's healthcheck: interval 1 s, 3 retries; the time depends on these settings); (c) with delivery concurrent with `up`: exit 0; (d): exit 0 |
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
| **A. tmpfs `/run/secrets` filled by `reader up` over `docker exec` stdin**, measured as (c) | Read-only roots; the value in no record checked and not in the container layer; not swappable inside the container with `memswap_limit == mem_limit` (`memory.swap.max = 0`); no file on the host | Lost on every container restart. `reader up` must stay running as a watcher: it delivers concurrently with `up` (delivering after `up` failed `up` in 4 s), re-delivers on every start, and holds every resolved secret in host memory for the stack's lifetime. The stack is down after a Docker restart or a reboot until the next `reader up`. Every consumer image needs `sh`, `cat` and `mv`, and a `mem_limit`. Delivery must be atomic (naive: 3 of 20 full reads). **Any process with the owner's Docker socket** (`~/.docker/run/docker.sock`, owned by the owner's user), coding agents included, can read a consumer's file with one `docker exec <consumer> cat /run/secrets/<name>`, or through a second container with `--pid container:<id>` (measured by the reviewer; RESULT.md) |
| **C. file-sourced Compose secret**, measured as (d) | Read-only roots; survives restart; no watcher, no wrapper; works with `up` and health dependencies as is (exit 0) | The value sits in a host file for the stack's lifetime (0600, 0700 directory; this Mac has FileVault on), readable by **any process running as the owner's user**, which includes coding agents. `docker inspect` shows the source path. A crash leaves the file until the next `reader up`/`down` removes it. ADR-0011's "nothing written to disk" no longer holds on the host, and the per-run directory would have to be excluded from backups and snapshots. Per-secret `uid`/`mode` are not applied on Docker Desktop. On Linux hosts a 0600 host file would be unreadable for a consumer with a different uid (untested). A RAM-disk source is macOS-only and unmeasured |
| B. env-sourced secret, writable root, measured as (b) | Compose-native, survives restart | The value is in the container layer on the VM disk, also after `stop`; gives up the read-only roots ADR-0022/0025 require |
| Swarm secrets | Native tmpfs secrets on read-only services | A raft store on the VM disk; replaces Compose (ADR-0012); not measured |
| Named tmpfs volume | No host file | Lost on restart, mounted without `noexec` (reviewer's measurement); no gain over A |
| Init container or sidecar holding the secret | Restart-proof delivery inside the stack | An extra long-lived holder of every secret, or a Docker socket in a service (ADR-0022/0025 forbid it) |

## Decision

**Decision: A (chosen by the owner, 2026-11-20).** The status stays `proposed` until the owner sets it.

Why A over C. Neither option stops a process running as the owner's user from reaching a value; the difference is how much effort that takes and where the value rests:
- **Under C,** the value is a plain host file at rest for the stack's lifetime. Any file read, grep, backup or snapshot tool reaches it, and it persists after a crash.
- **Under A,** the value is never at rest on the host. Reading it takes a deliberate `docker exec` into a consumer, or a container joined to its PID namespace, through the owner's Docker socket.

A's other costs are operational: a long-running `reader up`, and a stack that needs `reader up` after a restart. On the owner's single Mac, `reader up` already drives the stack.

**Proposed enforced control, which the owner adds because settings are the owner's:** deny rules in `.claude/settings.json` covering every read path listed above that the agent could run directly: `docker exec`, `docker compose exec`, and `docker run` with `--pid container:`. Claude Code's permission rules match the agent's own Bash command, not the `docker exec` that `reader up` runs internally, so `reader up` keeps working. Without that rule, agents are kept from reading consumer files only by AGENTS.md.

Under A:
- Each consumer mounts a tmpfs `/run/secrets` (owner = the service uid, mode 0700, size ≥ 256 KiB) and sets a `mem_limit` with `memswap_limit == mem_limit`. OPS-DEPLOY must source the `mem_limit` numbers. It has no Compose `secrets:` entries, and no secret in `environment:`, `env_file:`, labels, command or healthcheck.
- `reader up` stays running as a watcher.
  - It delivers each value concurrently with `docker compose up` and again on every container start it observes: `docker events` start events, or polling.
  - OPS-CLI requirements: whether `reader up` daemonises after the stack is healthy (the live-check scripts call `reader up` and then continue); how `reader down` stops the watcher; and that if the watcher dies, consumers that restart stay unhealthy (fail closed) until the next `reader up`.
  - Delivery runs as the service uid (`docker exec -u <uid> -i`): `umask 077; cat > /run/secrets/.<name>.tmp && mv /run/secrets/.<name>.tmp /run/secrets/<name>`, with the exit code checked.
  - The value travels only on stdin, never in argv.
- File names follow SRV-CONFIG's `secret_file_name(ref)`; the database password uses `db-password`.
- A consumer waits for the final name before reporting healthy.
- postgres runs as a non-root `user:`. Its entrypoint waits for `/run/secrets/db-password` only when PGDATA is empty, because the official image reads `POSTGRES_PASSWORD_FILE` at initdb (hypothesis, checked in the stage-1 live check).
- Kernel services stay secret-free (ADR-0022), so their restart-based reset needs no delivery.

C (rejected) would have needed:
- a per-run directory, 0700, outside every git work tree, excluded from backups and snapshots, and removed by `reader down` and on the next `reader up` after a crash;
- file-sourced Compose secrets;
- an AGENTS.md rule that agents must not read that directory.

Either choice replaces ADR-0011's delivery mechanism and ADR-0023's "through a Compose secret under `/run/secrets`" wording. Both keep their store and holder decisions: the Keychain is the store, and only the credential holder receives a token. Under A, ADR-0011's "secrets stay in the OS store until start" and ADR-0023's residual-exposure line ("while it starts the stack") extend to the lifetime of the running `reader up`.

## Rejected options — why (numbers)

- **B:** the value was found in the container layer by `docker export`, also after `stop`, and it gives up read-only roots.
- **Swarm:** a raft store on disk and a platform change away from Compose (ADR-0012).
- **Named tmpfs volume:** the same restart loss as A, with no `noexec`.
- **Init container or sidecar:** a permanent extra holder, or a Docker socket in a service.

## Consequences

- OPS-CLI implements the watcher, atomic delivery, daemonising or not, and stopping on `reader down`.
- OPS-DEPLOY adds the tmpfs mounts, `mem_limit` and `memswap_limit` on consumers; consumer images keep `sh`, `cat` and `mv`.
- The stage-1 live check covers re-delivery after a restart, `up` with health dependencies, and fail-closed behaviour when the watcher is gone.
- The owner decides on the proposed `.claude/settings.json` deny rule for direct `docker exec` by the agent.
- The amendment notes in ADR-0011 and ADR-0023 point here.
- The SRV-CONFIG spec's FR-CONFIG-14 sentence about `{ environment: <VAR> }` declarations and `secret_env_var` becomes obsolete. It lives on branch 1/S1.SRV-CONFIG (PR #21) and needs a paired spec and test change.

## Hypotheses (unmeasured)

- Event-driven re-delivery keeps restarted consumers from flapping unhealthy. Checked in the stage-1 live check (the spike polled).
- The postgres entrypoint wait works with the official image's init, under a non-root user. Checked in the stage-1 live check.
- tmpfs pages are charged to the container's cgroup, so `memswap_limit == mem_limit` keeps them out of the VM's swap. This is inferred from `memory.swap.max = 0`; the tmpfs pages themselves were not observed.
- The host process `reader up` holds the values in memory that falls under macOS's own swap (encrypted on this Mac). Not measured.

## Open questions

- None. The owner chose A on 2026-11-20 and sets `status: accepted`; the owner also decides on the proposed `docker exec` deny rule.
