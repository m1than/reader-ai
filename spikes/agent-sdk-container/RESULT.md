# Spike: agent-sdk-container — RESULT

**Question (docs/roadmap.md 0.3):** Does the Python Agent SDK run in a linux/arm64 container with `CLAUDE_CODE_OAUTH_TOKEN`, stream text and report cost?
**Pass criterion:** streams; cost reported.
**Verdict: PASS.** The worker can run agent stages in a container; the host-process fallback is not needed.

Date: 2026-11-20. Command: `bash spikes/agent-sdk-container/run.sh` (3 cold containers per model, then one sandboxed tools run). Raw data: `results/*.json`. Spend: $0.056 in total for this spike, including runs not kept in `results/` (see "Spend").

## Environment

| Item | Value |
| --- | --- |
| Host | Apple M5 Max, 36 GiB, macOS 27.2; Docker Desktop 29.9.2, VM 4 CPUs, 11.6 GiB |
| Image | `python:3.13-slim-bookworm` linux/arm64 + `claude-agent-sdk==0.2.186`, which bundles the Claude Code CLI (`_bundled/claude`). Image 832 MB; the SDK package with the bundled CLI is 361 MB of it |
| User | non-root, uid 1000 |
| Credential | `CLAUDE_CODE_OAUTH_TOKEN` from the Keychain item `reader-ai/claude-oauth-token` (ADR-0011 `secret://keychain/…`). `run.sh` passes it straight into `docker run -e` and never prints it. The owner allowed this read for the spike on 2026-11-20 |
| Prompt | Count 1–20 in words, all tools disallowed, `max_turns=1`, `setting_sources=[]`, `include_partial_messages=True` |

## Numbers (3 fresh containers per model)

Times are seconds from the start of `probe.py` inside the container. "Container wall" is `docker run` on the host, from start to exit.

| Measure | claude-haiku-5-5 | claude-sonnet-5-5 |
| --- | --- | --- |
| Container wall (cold start → exit) | 3.28–3.63 s | 5.41–5.96 s |
| CLI ready (`SystemMessage` init) | 0.89–1.20 s | 1.01–1.14 s |
| First text token (`StreamEvent` text delta) | 1.58–1.88 s | 3.52–3.88 s |
| Result | 1.82–2.10 s | 3.94–4.34 s |
| Streaming | 10–11 text deltas | 9–10 text deltas |
| `total_cost_usd` per call | $0.00026 | $0.0018 |
| Answer correct | 3 of 3 | 3 of 3 |

- **Cost is reported** on every `ResultMessage` (`total_cost_usd` and `usage` with cache fields). It is present on the OAuth subscription token, as the ADR-0005 ledger needs.
- **Fixed overhead per call.** The CLI sends about 9.9k tokens of its own system prompt (`cache_read_input_tokens` 9852–9873; 11512 with Bash enabled). Fresh containers still hit the server-side prompt cache. The first call on a model writes the cache. In a run whose files were overwritten (*observed, not recorded*: printed, then replaced by a re-run), that first Sonnet call cost $0.040 against $0.0018 for the next ones (`cache_creation_input_tokens` 9852). Pipeline stages should keep the system prompt stable to stay in cache.
- **Cold start.** About 1 s from process start to a ready CLI, inside a container that is already running. `docker run` adds about 1.5 s around it.

## Sandboxed tools run (owner, 2026-11-20: agents may use a Docker sandbox)

One run of `claude-haiku-5-5` that must use Bash. ADR-0014 limits applied: `--read-only`, tmpfs `/home/worker` and `/tmp` (256 MiB each), `--cap-drop ALL`, `no-new-privileges`, 2 CPUs, `--memory 2g --memory-swap 2g`, 256 PIDs.

| Measure | Value |
| --- | --- |
| Result | PASS: the agent ran Bash in the container and got `aarch64`, uid `1000`, `touch /probe-root: Read-only file system`, `sandbox-ok` |
| Turns, time, cost | 2 turns; result at 3.12 s; $0.0027 |

Findings for SRV-AGENT and OPS-SANDBOX:

1. **The CLI needs a writable home.** It writes `~/.claude`, and with a read-only root a tmpfs home is enough.
2. **An agent container needs egress to the Anthropic API.** The kernel sandbox has none (ADR-0014). An agent sandbox therefore cannot share the kernel's internal network. It needs an allowlist egress proxy that permits the API host only. This is not tested here: the run used Docker's default bridge network.
3. **Tool permissions are code, not prompts.** `allowed_tools` / `disallowed_tools` decide what the agent can call. Inside the sandbox, a granted Bash can do anything the container allows, so the container is the boundary.

## Spend

| Run | USD |
| --- | --- |
| First probe (haiku) | 0.0021 |
| 6 text runs (round 1; files overwritten) | 0.0447 |
| 6 text runs (round 2, kept) + tools run | 0.0090 |
| **Total** | **0.0558** |

## Hands to stage 1 (OPS-DEPLOY) and stage 5 (SRV-AGENT)

- Worker placement: **container** (ADR-0005 fallback not needed).
- Per-call floor: about 1 s CLI start plus about 9.9k cached system tokens. First-token latency is 1.6–1.9 s on Haiku and 3.5–3.9 s on Sonnet.
- The agent sandbox needs: non-root user, read-only root, tmpfs home, and API-only egress (to be built and tested in OPS-SANDBOX).
