#!/usr/bin/env bash
# Spike agent-sdk-container (docs/roadmap.md 0.3). Builds the image, runs the probe N times per model
# (a fresh container each time), then one sandboxed tools run. Each invocation writes a new directory
# results/<run-id>/ and never overwrites earlier results. Spends Agent SDK credit: the total is printed
# and goes into docs/progress.yaml budget.ledger.
#   bash spikes/agent-sdk-container/run.sh
#   RUNS=1 MODELS=claude-haiku-5-5 SKIP_TOOLS=1 bash spikes/agent-sdk-container/run.sh
#
# The token comes from the macOS Keychain item reader-ai/claude-oauth-token (ADR-0011
# secret://keychain/reader-ai/claude-oauth-token) into `docker run -e`; it is never printed.
# The owner allowed this read for this spike only (2026-11-20). Known exposure, unlike ADR-0011's
# /run/secrets delivery: while a container runs, its Config.Env (and so the token) is visible to
# `docker inspect`. It also sits in a shell local until `unset`, so `bash -x run.sh` would print it.
# Every run is capped at 120 s inside the container (`timeout -s KILL` as the entrypoint; the container
# stops when it fires). A host-side alarm does not work: the docker client forwards the signal into the
# container, where nothing handles it. Verified token-free:
#   docker run --rm --network none --entrypoint timeout reader-spike-agent-sdk -s KILL 3 sleep 20   → rc=137 after 4 s
set -euo pipefail
cd "$(dirname "$0")"
IMAGE=reader-spike-agent-sdk
RUNS="${RUNS:-3}"
MODELS="${MODELS-claude-haiku-5-5 claude-sonnet-5-5}"   # MODELS= (empty) runs no text probes
OUT="results/$(date +%Y%m%dT%H%M%S)"
mkdir -p "$OUT"

docker build -q --platform linux/arm64 -t "$IMAGE" . >/dev/null
docker image inspect "$IMAGE" --format 'image_bytes={{.Size}}'

now() { perl -MTime::HiRes=time -e 'printf "%.3f", time'; }

probe() { # name, extra docker flags..., -- , probe args...
  local name="$1"; shift
  local flags=(); while [[ "$1" != -- ]]; do flags+=("$1"); shift; done; shift
  local token; token="$(security find-generic-password -s reader-ai -a claude-oauth-token -w)"
  local start; start=$(now)   # after the secret is resolved: the wall time is docker run only
  CLAUDE_CODE_OAUTH_TOKEN="$token" docker run --rm --platform linux/arm64 -e CLAUDE_CODE_OAUTH_TOKEN \
    --entrypoint timeout ${flags[@]+"${flags[@]}"} "$IMAGE" -s KILL 120 python /probe.py "$@" > "$OUT/$name.json"
  unset token
  local wall; wall=$(perl -e "printf '%.2f', $(now) - $start")
  jq --argjson wall "$wall" '{docker_run_wall_s: $wall} + .' "$OUT/$name.json" > "$OUT/$name.tmp" && mv "$OUT/$name.tmp" "$OUT/$name.json"
  jq -c '{model, mode, docker_run_wall_s, marks_s, answer_ok, total_cost_usd, is_error}' "$OUT/$name.json"
}

for model in $MODELS; do
  for n in $(seq "$RUNS"); do probe "$model-$n" -- "$model"; done
done

# Sandbox check (owner, 2026-11-20: agents may use a Docker sandbox): the agent runs Bash inside the
# container with the ADR-0014 process, capability and resource limits. NOT applied: network/egress
# (default bridge), secrets (the token is in this container's environment), per-execution timeout.
if [[ -z "${SKIP_TOOLS:-}" ]]; then
  probe tools-sandbox-1 --read-only --tmpfs /home/worker:uid=1000,size=256m --tmpfs /tmp:uid=1000,size=256m \
    --cap-drop ALL --security-opt no-new-privileges --memory 2g --memory-swap 2g --cpus 2 --pids-limit 256 \
    -- claude-haiku-5-5 tools
fi
jq -s 'map(.total_cost_usd // 0) | add | "total_cost_usd=\(.)"' "$OUT"/*.json
