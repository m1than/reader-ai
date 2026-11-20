#!/usr/bin/env bash
# Spike agent-sdk-container (docs/roadmap.md 0.3). Builds the image, then runs the probe N times per model.
# Each run is a fresh container (cold start). Writes results/<model>-<n>.json. Spends Agent SDK credit:
# the total is printed and goes into docs/progress.yaml budget.ledger.
#   bash spikes/agent-sdk-container/run.sh
#
# The token comes from the macOS Keychain item reader-ai/claude-oauth-token (ADR-0011
# secret://keychain/reader-ai/claude-oauth-token) straight into the container's environment.
# It is never printed; the owner allowed this read for this spike (2026-11-20).
set -euo pipefail
cd "$(dirname "$0")"
IMAGE=reader-spike-agent-sdk
RUNS="${RUNS:-3}"
MODELS="${MODELS:-claude-haiku-5-5 claude-sonnet-5-5}"
mkdir -p results

docker build -q --platform linux/arm64 -t "$IMAGE" . >/dev/null
docker image inspect "$IMAGE" --format 'image_bytes={{.Size}}'

for model in $MODELS; do
  for n in $(seq "$RUNS"); do
    start=$(python3 -c 'import time; print(time.time())')
    CLAUDE_CODE_OAUTH_TOKEN="$(security find-generic-password -s reader-ai -a claude-oauth-token -w)" \
      docker run --rm --platform linux/arm64 -e CLAUDE_CODE_OAUTH_TOKEN "$IMAGE" "$model" > probe.json
    wall=$(python3 -c "import time; print(round(time.time() - $start, 2))")
    jq --argjson wall "$wall" '{container_wall_s: $wall} + .' probe.json > "results/$model-$n.json"
    rm -f probe.json
    jq -c '{model, container_wall_s, marks_s, text_deltas, answer_ok, total_cost_usd, is_error}' "results/$model-$n.json"
  done
done
jq -s 'map(.total_cost_usd // 0) | add | "total_cost_usd=\(.)"' results/*.json
