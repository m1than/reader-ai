#!/usr/bin/env bash
# Spike obs-stack, round 4, every tab before and after (V.obs-analytics). Two fresh spike stacks: `before` replays
# today's span shapes, `after` the proposed conventions, child spans, assessments and prompts (analytics_tabs.py);
# each is screenshotted tab by tab under SRV-OBS's CSP (analytics-tabs.js). Zero credit; own project; no published
# port; the stack is stopped after each variant.
#   bash spikes/obs-stack/run-tabs.sh
set -euo pipefail
cd "$(dirname "$0")"
OUT="$PWD/results/$(date +%Y%m%dT%H%M%S)-round4-tabs"
mkdir -p "$OUT"
export OUT BACKEND=sqlite
export CSP_CANDIDATE="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; worker-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'"
DC=(docker compose -p reader-spike-obs -f compose.yaml)
NM=${PLAYWRIGHT_NM:-/Users/m1than/Developer/reader-ai-int/web/node_modules}
PW_IMAGE=mcr.microsoft.com/playwright:v1.65.1-noble
down() { "${DC[@]}" --profile sqlite --profile driver down -v --remove-orphans >/dev/null 2>&1 || true; }
trap down EXIT

"${DC[@]}" --profile sqlite --profile driver build -q
for VARIANT in before after; do
  down
  # `after`: the ledger is MLflow's cost source; a local zero-price catalog keeps MLflow from pricing the CLI spans.
  [ "$VARIANT" = after ] && DC=(docker compose -p reader-spike-obs -f compose.yaml -f compose.catalog.yaml)
  "${DC[@]}" --profile sqlite up -d
  "${DC[@]}" --profile sqlite --profile driver run --rm -e VARIANT="$VARIANT" \
    -v "$PWD/analytics.py:/spike/analytics.py:ro" -v "$PWD/analytics_tabs.py:/spike/analytics_tabs.py:ro" \
    --entrypoint python driver /spike/analytics_tabs.py > "$OUT/data-$VARIANT.stdout" 2> "$OUT/data-$VARIANT.stderr"
  docker run --rm --network container:reader-spike-obs-caddy-1 --read-only --tmpfs /tmp --cap-drop ALL \
    --security-opt no-new-privileges --user pwuser --ipc private -e VARIANT="$VARIANT" \
    -v "$NM:/nm:ro" -v "$PWD/analytics-tabs.js:/spike/analytics-tabs.js:ro" -v "$OUT:/out" \
    "$PW_IMAGE" node /spike/analytics-tabs.js > "$OUT/tabs-$VARIANT.stdout" 2> "$OUT/tabs-$VARIANT.stderr" || true
  docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' | grep reader-spike-obs > "$OUT/stats-$VARIANT.txt" || true
done
echo "results in $OUT"
