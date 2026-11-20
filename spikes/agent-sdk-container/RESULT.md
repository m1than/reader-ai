# Spike: agent-sdk-container — RESULT

**Question (docs/roadmap.md 0.3):** Does the Python Agent SDK run in a linux/arm64 container with `CLAUDE_CODE_OAUTH_TOKEN`, stream text and report cost?
**Pass criterion:** streams; cost reported.
**Verdict: PASS.** The worker can run agent stages in a container; the host-process fallback is not needed. Token delivery and sandboxing of agent tools still have open design work (see "Findings").

Date: 2026-11-20. Command: `bash spikes/agent-sdk-container/run.sh`. Each invocation writes a new `results/<run-id>/`.

| Results dir | Image | What |
| --- | --- | --- |
| `results/20261120T-r2-py313-bookworm/` | `python:3.13-slim-bookworm` | 3 fresh containers per model (haiku, sonnet) + 1 sandboxed tools run. All numbers in the tables below come from here |
| `results/20261120T180638/` | `python:3.14-slim-trixie`: the owner asked for the latest LTS/stable toolchain; proposed in ADR-0020 on branch `0/toolchain-baseline` | Verification on the new base: 1 haiku text run + 1 tools run, both pass ("works"; too few runs for timings) |
| `results/20261120T180949/` | same | 1 haiku text run under the `timeout` entrypoint |

## Environment

| Item | Value |
| --- | --- |
| Host | Apple M5 Max, 36 GiB, macOS 27.2; Docker Desktop 29.9.2, VM 4 CPUs, 11.6 GiB, about ten unrelated containers running |
| Image | Python + `claude-agent-sdk==0.2.186`, which bundles the Claude Code CLI (`_bundled/claude`, 376,366,520 bytes). Image 832,079,498 bytes (bookworm) / 832,809,987 bytes (trixie) |
| User | non-root, uid 1000 |
| Credential | `CLAUDE_CODE_OAUTH_TOKEN` from the Keychain item `reader-ai/claude-oauth-token`, passed with `docker run -e` and never printed. The owner allowed this read **for this spike only** (2026-11-20). The token was rejected with 401 until the owner re-stored it; those runs made no model call |
| Prompt | Count 1–20 in words, all tools disallowed, `max_turns=1`, `setting_sources=[]`, `include_partial_messages=True` |

## Numbers (round 2: 3 fresh containers per model)

"Fresh" means a new container from an image already in the page cache, not a cold image pull. Marks are seconds from `t0` in `probe.py` `main()`, which is set after the interpreter has started and `claude_agent_sdk` is imported. "Host wall" is measured by `run.sh` on the host around the whole probe and in round 2 includes the Keychain read and two host `python3` launches. The current `run.sh` starts the clock after the secret is resolved (`docker_run_wall_s`).

| Measure | claude-haiku-5-5 | claude-sonnet-5-5 |
| --- | --- | --- |
| Host wall, including Keychain read | 3.28–3.63 s | 5.41–5.96 s |
| CLI ready (`SystemMessage` init) | 0.89–1.20 s | 1.01–1.14 s |
| First text token (`StreamEvent` text delta) | 1.58–1.88 s | 3.52–3.88 s |
| Result | 1.82–2.10 s | 3.94–4.34 s |
| Unattributed overhead (host wall − result) | 1.46–1.58 s | 1.47–1.75 s |
| Streaming | 10–11 text deltas | 9–10 text deltas |
| `total_cost_usd` per call (prompt cache hit) | $0.00026 | $0.0018 |
| Answer correct | 3 of 3 | 3 of 3 |

The unattributed overhead covers the Keychain read, host Python launches, container create, interpreter start, SDK import, CLI shutdown and `--rm` teardown. None of them is measured separately. The Keychain read is not the main part. The later haiku runs measure `docker_run_wall_s` after the secret is resolved, and still show 1.89 s (`20261120T180638`, text), 1.46 s (same, tools) and 2.33 s (`20261120T180949`) of overhead beyond the result mark.

- **Cost is reported** on every `ResultMessage` (7 of 7 in round 2, 2 of 2 on trixie): `total_cost_usd` plus `usage` with cache fields. This is the CLI's own figure. It was not reconciled against the Agent SDK credit actually drawn, and `budget.spent_usd` inherits it. The run used the OAuth token by construction (the only credential passed, `setting_sources=[]`); `apiKeySource` from the init message was not recorded.
- **Fixed overhead per call.** The CLI sends about 9.9k tokens of its own system prompt (`cache_read_input_tokens` 9852–9873; 11512 with Bash enabled). Fresh containers still hit the server-side prompt cache. The first call on a model writes the cache:
  - The tools run in round 2 wrote 11,705 tokens (`cache_creation_input_tokens`) for $0.0027. The same run on trixie read them from cache for $0.00048. So $0.0027 is a first-call cost, not a steady-state one.
  - The first Sonnet call of round 1 cost about $0.04. Its cause was not recorded: the files were overwritten, which the current `run.sh` prevents. It is left to `compile-cost`.

## Sandboxed tools run (owner, 2026-11-20: agents may use a Docker sandbox)

One `claude-haiku-5-5` run must run Bash in the container. Applied: ADR-0014's process, capability and resource limits (`--read-only`; tmpfs `/home/worker` and `/tmp`, 256 MiB each; `--cap-drop ALL`; `no-new-privileges`; 2 CPUs; `--memory 2g --memory-swap 2g`; 256 PIDs). **Not applied:**
- Network and egress: Docker's default bridge network, with internet.
- Secrets: the provider token is in this container's environment.
- Per-execution timeout: none in the recorded tools runs. A first cap (host-side `perl alarm`) had no effect: the docker client forwards the signal into the container, where nothing handles it (found by the review). `run.sh` now runs `timeout -s KILL 120` as the container's entrypoint. Verified token-free: a 20 s `sleep` was killed at about 4 s, rc=137. The probe works under it (`results/20261120T180949/`).

The flags were set but not verified from inside the container.

| Measure | Round 2 (bookworm) | Trixie |
| --- | --- | --- |
| Agent's reply | `aarch64`, `1000`, `touch: cannot touch '/probe-root': Read-only file system`, `sandbox-ok` | same checks pass |
| Turns, result mark, cost | 2 turns; 3.12 s; $0.0027 (cache write) | 2.99 s; $0.00048 (cache read) |

`answer_ok` is judged from the model's text reply; tool-use blocks are not recorded. `num_turns` = 2 and the "Read-only file system" error (uid 1000 would otherwise get "Permission denied") make it credible.

## Findings for SRV-AGENT, OPS-SANDBOX and OPS-CLI

1. **The provider token is inside the tool sandbox.** The agent's Bash runs in the container whose environment holds `CLAUDE_CODE_OAUTH_TOKEN`. Whether the CLI strips it from the Bash tool's environment was not tested. With egress open, an injected `env | curl` would have both the token and a route out. The agent sandbox must keep the credential out of reach of tool execution, for example by running tools in a separate container from the one that holds the token. This is a design task for SRV-AGENT and OPS-SANDBOX; it is not solved here.
2. **`docker run -e` exposes the token to `docker inspect`.** While a container runs, its `Config.Env` holds the token in plain text. Anything with the Docker socket can read it, and Docker's container config in the VM stores it. It is also in `/proc/<pid>/environ` inside the container and in the host `docker` process's environment (same-uid `ps eww`). ADR-0011 delivers secrets as `/run/secrets` files, and an OPS-CLI invariant scans `docker inspect`, which this method would fail. "Token via `/run/secrets` into the CLI's environment through an entrypoint" is **untested**. Verified clean: `results/*.json`, the image's `Config.Env`, `docker history`, argv, and no leftover containers.
3. **The CLI writes `~/.claude`.** A tmpfs home on a read-only root is enough. A read-only home was not tried.
4. **An agent container needs egress to the Anthropic API**, unlike the kernel sandbox (ADR-0014). It cannot share the kernel's internal network. It needs an allowlist egress proxy for the API host only. Not built or tested here.
5. **Tool permissions are code.** `allowed_tools` / `disallowed_tools` decide what the agent may call. A granted Bash can do anything the container allows, so the container, its secrets and its network are the boundary.

## Spend

| Run | USD | Evidence |
| --- | --- | --- |
| First probe (haiku), round 0 | 0.0021 | printed, not kept |
| 6 text runs, round 1 | 0.0447 | printed, files overwritten |
| 6 text runs + tools run, round 2 | 0.0090 | `results/20261120T-r2-py313-bookworm/` (sum 0.00897) |
| Trixie verification, 2 runs | 0.00075 | `results/20261120T180638/` |
| Timeout entrypoint verification, 1 run | 0.00026 | `results/20261120T180949/` |
| Runs rejected with 401 | 0 | no model call |
| **Total** | **0.0568** (unrounded rows: 0.05678) | 82% of it rests on printed output that was not kept |

## Hands to stage 1 (OPS-DEPLOY, OPS-CLI) and stage 5 (SRV-AGENT)

- Worker placement: **container** (ADR-0005 fallback not needed). Base: `python:3.14-slim-trixie` works.
- Per-call floor: about 1 s from SDK import to a ready CLI, plus about 9.9k cached system tokens. First token in 1.6–1.9 s on Haiku and 3.5–3.9 s on Sonnet.
- Open, untested: token via `/run/secrets`; tools separated from the credential; API-only egress.
