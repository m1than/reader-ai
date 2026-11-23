#!/usr/bin/env bash
# Spike web-search § 5b: ONE live WebSearch run (owner-approved 2026-11-23: at most $0.10 from budget.test_budget_usd,
# one search, preferably Haiku 5.5). results/<run-id>/live.json holds numbers, key names and shapes only.
#   bash spikes/web-search/run-live.sh                  (MODEL=claude-sonnet-5-5 to run the main loop on Sonnet)
#
# THE OWNER RUNS THIS SCRIPT, not the agent: it reads the Keychain item reader-ai/claude-oauth-token and pipes it on
# stdin into the container, where the entry shell exports it for the CLI only. It is never printed, written, passed
# with -e (so `docker inspect` cannot show it) or logged; the recording proxy inside forwards the Authorization header
# without reading it (live.py). Same pattern as spikes/tutor-call/run.sh.
#
# Spend cap: CAP_USD (default 0.10) minus every run already recorded in results/*/live.json. Inside, live.py's proxy
# refuses any request whose worst case would pass the remaining cap, allows one search with max_uses rewritten to 1,
# and the SDK gets the same max_budget_usd. Record the spend in docs/progress.yaml budget.ledger after the run.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE=reader-spike-agent-proxy-handshake     # python 3.14 + claude-agent-sdk 0.2.187 (SRV-AGENT's pin), non-root
CAP_USD="${CAP_USD:-0.10}"
MODEL="${MODEL:-claude-haiku-5-5}"
RUN_ID="$(date +%Y%m%dT%H%M%S)"
NUM="$HERE/results/$RUN_ID"

command -v jq >/dev/null || { echo "jq is required"; exit 1; }
shopt -s nullglob
runs=("$HERE"/results/*/live.json)
spent=0
((${#runs[@]})) && spent=$(jq -s 'map([.sdk.result.total_cost_usd // 0, ([.requests[]?.usd // 0] | add // 0)] | max) | add // 0' "${runs[@]}")
left=$(jq -n --argjson cap "$CAP_USD" --argjson s "$spent" '$cap - $s')
echo "cap=$CAP_USD already_spent=$spent left=$left model=$MODEL"
jq -e -n --argjson l "$left" '$l >= 0.03' >/dev/null || { echo "cap reached"; exit 1; }

docker build -q --platform linux/arm64 -t "$IMAGE" "$HERE/../agent-proxy-handshake" >/dev/null
mkdir -p "$NUM"
security find-generic-password -s reader-ai -a claude-oauth-token -w | docker run --rm -i --platform linux/arm64 \
  -e CAP_USD="$left" -e MODEL="$MODEL" -e HOME=/home/worker \
  --read-only --tmpfs /home/worker:uid=1000,size=256m --tmpfs /tmp:uid=1000,size=256m \
  --cap-drop ALL --security-opt no-new-privileges --memory 2g --memory-swap 2g --pids-limit 256 \
  -v "$HERE/live.py:/spike/live.py:ro" -v "$NUM:/out/numbers" \
  --entrypoint sh "$IMAGE" -c 'IFS= read -r CLAUDE_CODE_OAUTH_TOKEN && export CLAUDE_CODE_OAUTH_TOKEN && exec timeout -s KILL 600 python -I /spike/live.py'
jq -c '{model, wall_s, error, refused, sdk_usd: .sdk.result.total_cost_usd, checks}' "$NUM/live.json"
echo "Owner, optional: compare the Agent SDK credit used in your Claude account before and after this run (does the"
echo "\$0.01 search fee draw from the subscription's Agent SDK credit?) and tell the agent the difference."
