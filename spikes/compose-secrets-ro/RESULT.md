# Spike: compose-secrets-ro — RESULT

**Question (OPS-DEPLOY spec review, finding B7; ADR-0011, ADR-0023):** can a resolved secret reach a **read-only, non-root, cap-drop** service as a readable file under `/run/secrets`, without appearing in `docker inspect` and without being written into the container layer? ADR-0011's mechanism is `secrets: <name>: environment: <VAR>`.

**Verdict:**
- ADR-0011's mechanism **does not work**: Compose refuses an environment-sourced secret on a read-only service.
- Two mechanisms do work on read-only services:
  - **(c)** a tmpfs `/run/secrets`, filled by the host over `docker exec` stdin, atomically;
  - **(d)** a file-sourced Compose secret.
- Their costs differ:
  - (c) loses the value on every restart.
  - (d) keeps the value in a host file for the stack's lifetime.

The choice between them is the owner's (proposed ADR-0026).

Date: 2026-11-20. Run `results/20261120T214345/` (`bash spikes/compose-secrets-ro/run.sh`).
- **Environment:** Docker Desktop engine 29.9.2, Compose 5.6.1, VM kernel 7.0.14-linuxkit, image `python:3.14-slim-trixie`, linux/arm64.
- **Service under test:** `user: "1000:1000"`, `cap_drop: [ALL]`, `no-new-privileges`, `network_mode: none` in every variant.
- **Values:** a fresh canary per variant (`CANARY-` plus 32 hex digits, 39 characters). No real secret was used.
- **Results:** the result files hold numbers and booleans only.
- **Teardown:** 0 containers and 0 volumes left afterwards.
- **Task id:** this is task `S1.spike.compose-secrets-ro` in `docs/progress.yaml`. The branch is named `0/S0.spike.compose-secrets-ro` for historical reasons.

## Results

| Variant | Setup | Outcome | Owner and mode inside | `inspect` / `config` / `export` / `events` / Desktop logs (value matches) | Files below `/run/secrets` in the layer (`docker diff`) | After restart |
| --- | --- | --- | --- | --- | --- | --- |
| **(a)** | environment-sourced secret, `read_only: true` | **Refused** at `up` (exit 1): "`file` is the sole supported option" | — | — | — | — |
| **(b)** | environment-sourced secret, writable root | works | root, `0444` | 0 / 0 / **1** / 0 / 0 | **1** (`A /run/secrets/tok` with content; `export` finds the value, also after `stop`) | kept |
| **(c)** | `read_only: true`, tmpfs `/run/secrets` (uid 1000, 0700, 64k), `memswap_limit == mem_limit` (256m); host writes `umask 077; cat > .tok.tmp && mv .tok.tmp tok` over `docker exec -i` stdin | works, delivery exit 0 | uid 1000, `0600` | 0 / 0 / 0 / 0 / 0 | 0 | **lost** |
| **(d)** | `read_only: true`, **file-sourced** Compose secret; host source `0600` in a `0700` temp directory | works | uid 1000, `0600` (the requested `uid`/`mode: 0400` are not applied: Docker Desktop bind-mounts the host file) | 0 / 0 / 0 / 0 / 0; **inspect shows the source path** | 1, but only the bind mount point: `export` has 0 matches | kept |

Further measurements in (c):

| Measure | Value |
| --- | --- |
| `memory.swap.max` in the container with `memswap_limit == mem_limit` | `0` |
| Root in the container (`exec -u 0`) under `cap_drop: [ALL]` reads the file | no |
| Small files that fit in the 64k tmpfs | 15 besides `tok` (16 in total; each file takes a 4 KiB page) |
| A reader that waits for the file and reads as soon as it exists: full value read, out of 20 trials | **naive** `cat > tok`: **3**; **atomic** temp name then `mv`: **20** |

Dependency on health (e), consumer healthy only when its file exists, dependent with `depends_on: condition: service_healthy`:

| Case | `docker compose up -d` |
| --- | --- |
| (c), delivery **after** `up` returns | **exit 1** after 4 s: the consumer turns unhealthy before the value arrives |
| (c), delivery **concurrent** with `up` (the host polls until the consumer runs, then delivers) | exit 0, 3 s |
| (d) | exit 0, 2 s |

VM facts (`vm.json`):
- The Docker Desktop VM swaps to `/var/lib/swap` on its disk: 1,780,732 kB in size, 890,364 kB in use during the run.
- Docker rejects a `noswap` tmpfs option (`invalid tmpfs option`).
- Without `memswap_limit == mem_limit`, a container's pages, tmpfs included, can be swapped to the VM disk. The reviewer observed `memory.swap.max = max` for (c) without the limit; that case was not re-run here.

The reviewer of this spike also observed, separately:
- `docker events` records the exec command line (`exec_create: sh -c umask 077; cat > /run/secrets/tok`): the secret's **name**, never its value. The value must therefore never reach argv.
- A second container joined to the consumer's PID namespace as the same uid can read the file through `/proc/1/root`.
- A file-sourced secret also survives restart when its source is on ordinary APFS.

## Findings

1. **ADR-0011's mechanism is unusable for the read-only services ADR-0022 and ADR-0025 require** (a).
2. **A writable root (b) puts the value in the container layer on the VM disk.** It is still there after `stop`, and is removed only with the container.
3. **(c)** keeps the value out of every record checked and out of the container layer. With `memswap_limit == mem_limit` its pages cannot be swapped. It needs:
   - **atomic delivery**: a naive `cat >` let a waiting reader see a partial or empty file in 17 of 20 trials;
   - **delivery concurrent with `up`** whenever anything depends on a consumer's health;
   - **re-delivery after every restart.**
4. **(d)** survives restart and works with `up` as is. Its costs:
   - the value lives in a host file for the stack's lifetime, readable by any process of the owner's user (this Mac has FileVault on);
   - `docker inspect` shows the source path;
   - the per-secret `uid`/`mode` options are not applied on Docker Desktop.
5. **Root inside a consumer cannot read the file without `CAP_DAC_OVERRIDE`**, which `cap_drop: [ALL]` removes.
6. **Consumer image requirement:** (c) needs `sh`, `cat` and `mv` in every consumer image. `docker cp` into the read-only root is refused, per the reviewer.

## Not tested

- (d) with a RAM-disk source (`hdiutil`), and its swap behaviour.
- Many consumers and secrets at once; a crash-restart loop under (c); event-driven re-delivery (the spike polled).
- `docker compose up --wait`.
- The postgres image specifically: its user, and when it reads `POSTGRES_PASSWORD_FILE`.
- Windows and Linux hosts (ADR-0011 names three platforms).
