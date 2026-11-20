#!/usr/bin/env bash
# Spike compile-cost (docs/roadmap.md 0.3). Compiles lesson L02 once through ADR-0009's stages and writes
# results/<run-id>/ (numbers only). Lesson text goes to the gitignored data/spikes/compile-cost/runs/<run-id>/.
#   bash spikes/compile-cost/run.sh
# Source pages: data/spikes/compile-cost/source/pNNNN.md, PDF pages 60-84, produced by
#   CODE=0 uv run --script spikes/ingest-docling/convert_native.py <pdf> 60,...,84 data/spikes/compile-cost/source
#
# Spend cap: CAP_USD (default 8.00, owner 2026-11-20) minus everything already recorded in results/*/*.json.
# Token: the owner allowed this spike to read the Keychain item reader-ai/claude-oauth-token (2026-11-20). It is
# piped to the container on stdin, exported only inside it, never printed, and never in `docker inspect`.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
IMAGE=reader-spike-compile
CAP_USD="${CAP_USD:-8.00}"
RUN_ID="$(date +%Y%m%dT%H%M%S)"
SRC="$REPO/data/spikes/compile-cost/source"
TEXT="$REPO/data/spikes/compile-cost/runs/$RUN_ID"
NUM="$HERE/results/$RUN_ID"
[[ -n "$(ls "$SRC"/p*.md 2>/dev/null)" ]] || { echo "no source pages in $SRC"; exit 1; }

shopt -s nullglob
summaries=("$HERE"/results/*/summary.json)
spent=0
((${#summaries[@]})) && spent=$(jq -s 'map(.total_cost_usd) | add // 0' "${summaries[@]}")
left=$(jq -n --argjson cap "$CAP_USD" --argjson s "$spent" '$cap - $s')
echo "cap=$CAP_USD already_spent=$spent left=$left"
jq -e -n --argjson l "$left" '$l >= 0.5' >/dev/null || { echo "cap reached"; exit 1; }

docker build -q --platform linux/arm64 -t "$IMAGE" "$HERE" >/dev/null
mkdir -p "$TEXT" "$NUM"
security find-generic-password -s reader-ai -a claude-oauth-token -w | docker run --rm -i --platform linux/arm64 \
  -e CAP_REMAINING="$left" -e CAP_S=3000 \
  --read-only --tmpfs /home/worker:uid=1000,size=256m --tmpfs /tmp:uid=1000,size=256m \
  --cap-drop ALL --security-opt no-new-privileges --memory 2g --memory-swap 2g --pids-limit 256 \
  -v "$SRC:/in/source:ro" -v "$HERE/lesson-L02.json:/in/lesson.json:ro" \
  -v "$TEXT:/out/text" -v "$NUM:/out/numbers" \
  "$IMAGE" /in/lesson.json
rm -f "$NUM"/*.tmp
jq -c . "$NUM/summary.json"
