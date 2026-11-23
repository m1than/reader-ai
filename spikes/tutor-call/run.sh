#!/usr/bin/env bash
# Spike tutor-call (SRV-TUTOR review round 1, blocker 1): 5 tutor-shaped single-turn Sonnet calls, each once with
# extended thinking off (MAX_THINKING_TOKENS=0) and once with the CLI default; results/<run-id>/ holds numbers only.
#   bash spikes/tutor-call/run.sh
# Source spans: synthetic (synthetic.json), no book text.
#
# Spend cap: CAP_USD (default 0.60, from the owner's $30 test budget, 2026-11-22) minus everything already recorded in
# results/*/q*.json. Record the spend in docs/progress.yaml budget.ledger after the run.
#
# Token: like spikes/compile-cost, this runner reads the Keychain item reader-ai/claude-oauth-token and pipes it to the
# container on stdin, never printed. The owner allowed that only for agent-sdk-container, compile-cost and
# trace-backend (2026-11-20), so THIS RUNNER IS RUN BY THE OWNER, or by the agent once the owner extends that
# permission to tutor-call.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE=reader-spike-compile          # the compile-cost image: python 3.14 + claude-agent-sdk, non-root
CAP_USD="${CAP_USD:-0.60}"
RUN_ID="$(date +%Y%m%dT%H%M%S)"
NUM="$HERE/results/$RUN_ID"

shopt -s nullglob
calls=("$HERE"/results/*/q*.json)
spent=0
((${#calls[@]})) && spent=$(jq -s 'map(.total_cost_usd // 0) | add // 0' "${calls[@]}")
left=$(jq -n --argjson cap "$CAP_USD" --argjson s "$spent" '$cap - $s')
echo "cap=$CAP_USD already_spent=$spent left=$left"
jq -e -n --argjson l "$left" '$l >= 0.05' >/dev/null || { echo "cap reached"; exit 1; }

docker build -q --platform linux/arm64 -t "$IMAGE" "$HERE/../compile-cost" >/dev/null
mkdir -p "$NUM"
security find-generic-password -s reader-ai -a claude-oauth-token -w | docker run --rm -i --platform linux/arm64 \
  -e CAP_REMAINING="$left" \
  --read-only --tmpfs /home/worker:uid=1000,size=256m --tmpfs /tmp:uid=1000,size=256m \
  --cap-drop ALL --security-opt no-new-privileges --memory 2g --memory-swap 2g --pids-limit 256 \
  -v "$HERE/call.py:/app/call.py:ro" -v "$HERE/system.md:/app/system.md:ro" \
  -v "$HERE/synthetic.json:/in/sources.json:ro" -v "$NUM:/out/numbers" \
  --entrypoint sh "$IMAGE" -c 'IFS= read -r CLAUDE_CODE_OAUTH_TOKEN && export CLAUDE_CODE_OAUTH_TOKEN && exec timeout -s KILL 900 python /app/call.py'
jq -c . "$NUM/summary.json"
for f in "$NUM"/q*.json; do
  jq -c '{q: .question, thinking, wall_s, usd: .total_cost_usd, out: .usage.output_tokens,
          out_details: .usage.output_tokens_details, chars: .output_chars, json_ok}' "$f"
done
