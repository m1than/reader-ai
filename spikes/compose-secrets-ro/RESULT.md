# Spike: compose-secrets-ro — RESULT

**Question (OPS-DEPLOY spec review, finding B7; ADR-0011, ADR-0023):** Can a Compose secret sourced from the host process's environment (ADR-0011: "`secrets: <name>: environment: <VAR>` … appear as files under `/run/secrets/`. Nothing is written to disk") reach a **read-only, non-root, cap-drop** service as a readable file, while staying out of `docker inspect`?

**Verdict: no, as ADR-0011 describes it.** Compose refuses an environment-sourced secret in a read-only service. With a writable root it works, but Compose copies the value into the container's writable layer, which sits on the Docker VM's disk. A third variant meets every constraint: read-only root, a tmpfs at `/run/secrets` owned by the service user, and the value written after start through `docker exec -i` over stdin. Its cost is that the value is lost on every container restart and must be delivered again.

Date: 2026-11-20. Docker Desktop, engine 29.9.2, Compose 5.6.1, `python:3.14-slim-trixie`, linux/arm64. The secret was a random canary (`CANARY-<uuid>`, 39 characters); no real secret was used. All runs used `network_mode: none`, `user: "1000:1000"`, `cap_drop: [ALL]` and `no-new-privileges`, and were torn down afterwards.

## Results

| Variant | File | Outcome | Readable by uid 1000 | In `docker inspect` | Written to the container layer (`docker diff`) | After `restart` |
| --- | --- | --- | --- | --- | --- | --- |
| (a) env-sourced secret, `read_only: true` | `compose.yaml` | **Refused** by Compose at `up`: `cannot create secret "reader-spike-csro_tok" in read-only service svc: \`file\` is the sole supported option` | — | — | — | — |
| (b) env-sourced secret, writable root | `compose.b-writable.yaml` | Works | yes (`file_uid 0`, mode `0444`) | no (0 matches) | **yes**: `A /run/secrets/tok`, so the value is on the VM disk | kept |
| (c) `read_only: true`, tmpfs `/run/secrets` (uid 1000, mode 0700, 64k), value written by `docker exec -i svc sh -c 'umask 077; cat > /run/secrets/tok'` from stdin | `compose.c-exec.yaml` | Works | yes (`file_uid 1000`, mode `0600`) | no (0 matches) | no: only the mount point (`C /run`, `A /run/secrets`) | **lost** (0 files after `docker compose restart`) |

`docker compose config` output contained the canary 0 times in (a) and (b).

The commands are in the commit message of this spike. Each variant printed one JSON-like line from inside the container (`uid`, `file_uid`, `mode`, `length`, whether the value starts with `CANARY-`); the value itself was never printed.

## Findings

1. **ADR-0011's mechanism and ADR-0022/0025's hardening are incompatible.** Read-only roots are required for the kernel services (ADR-0022) and for `agent-proxy` and `worker` (ADR-0025). Compose's environment-sourced secrets cannot be used there.
2. **A writable root (b) breaks "nothing is written to disk".** The value lands in the container's writable layer on the Docker VM's disk until the container is removed. `docker inspect` stays clean.
3. **Exec over stdin (c) keeps every constraint**: read-only root, the value only in memory (tmpfs), file mode 0600 for the service user, clean `docker inspect`, nothing in argv, environment or the compose file.
   - **Restart loses the value.** It must be re-delivered after every container start. Under (c) the delivery belongs to `reader up` (OPS-CLI), and a service that restarts on its own (restart policy, crash) waits for the file.
   - **The root user in the container could also read it.** Here the service runs as uid 1000 with no capabilities; no root process runs in the container.
   - **`docker exec` needs the Docker socket on the host side**, which `reader up` already uses. No service gets the socket (ADR-0022/0025 hold).
4. **Not tested:**
   - the waiting behaviour for many services at once;
   - how a crash-restart loop interacts with re-delivery;
   - `docker compose up --wait` with services that are unhealthy until their secret arrives;
   - the file-sourced Compose secret from a host RAM disk (another way to avoid the VM disk; unmeasured).

## Consequence

ADR-0011's delivery text, and ADR-0023's "`/run/secrets` via Compose secrets", need a proposed amendment: **secrets reach read-only services by exec-over-stdin into a tmpfs `/run/secrets` after start, re-delivered by `reader up` on every start**. OPS-DEPLOY's FR-DEPLOY-9 and OPS-CLI depend on it. This is the owner's decision (an ADR amendment).
