#!/usr/bin/env bash
# Spike obs-stack, round 4 (V.obs-analytics): replay synthetic spans carrying MLflow's conventions, read MLflow's REST
# view, searches and trace metrics (analytics.py), then a headless-browser pass over the UI under SRV-OBS's CSP
# (analytics-browser.js). Zero credit; own project; no published port (the browser shares caddy's network namespace).
#   bash spikes/obs-stack/run-analytics.sh [--no-browser]
set -euo pipefail
cd "$(dirname "$0")"
OUT="$PWD/results/$(date +%Y%m%dT%H%M%S)-round4"
mkdir -p "$OUT"
export OUT BACKEND=sqlite
export CSP_CANDIDATE="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; worker-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'"
DC=(docker compose -p reader-spike-obs -f compose.yaml)
NM=${PLAYWRIGHT_NM:-/Users/m1than/Developer/reader-ai-int/web/node_modules}
PW_IMAGE=mcr.microsoft.com/playwright:v1.65.1-noble
cleanup() { "${DC[@]}" --profile sqlite --profile driver down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

"${DC[@]}" --profile sqlite --profile driver build -q
"${DC[@]}" --profile sqlite up -d
"${DC[@]}" --profile sqlite --profile driver run --rm -v "$PWD/analytics.py:/spike/analytics.py:ro" \
  --entrypoint python driver /spike/analytics.py > "$OUT/analytics.stdout" 2> "$OUT/analytics.stderr"
"${DC[@]}" logs --no-color mlflow-sqlite > "$OUT/mlflow.log" 2>&1 || true
if [[ "${1:-}" != "--no-browser" ]]; then
  docker run --rm --network container:reader-spike-obs-caddy-1 --read-only --tmpfs /tmp --cap-drop ALL \
    --security-opt no-new-privileges --user pwuser --ipc private \
    -v "$NM:/nm:ro" -v "$PWD/analytics-browser.js:/spike/analytics-browser.js:ro" -v "$OUT:/out" \
    "$PW_IMAGE" node /spike/analytics-browser.js > "$OUT/browser.stdout" 2> "$OUT/browser.stderr" || true
  docker run --rm --network container:reader-spike-obs-caddy-1 --read-only --tmpfs /tmp --cap-drop ALL \
    --security-opt no-new-privileges --user pwuser --ipc private \
    -v "$NM:/nm:ro" -v "$PWD/analytics-filters.js:/spike/analytics-filters.js:ro" -v "$OUT:/out" \
    "$PW_IMAGE" node /spike/analytics-filters.js > "$OUT/filters.stdout" 2> "$OUT/filters.stderr" || true
  docker run --rm --network container:reader-spike-obs-caddy-1 --read-only --tmpfs /tmp --cap-drop ALL \
    --security-opt no-new-privileges --user pwuser --ipc private \
    -v "$NM:/nm:ro" -v "$PWD/analytics-timeline.js:/spike/analytics-timeline.js:ro" -v "$OUT:/out" \
    "$PW_IMAGE" node /spike/analytics-timeline.js > "$OUT/timeline.stdout" 2> "$OUT/timeline.stderr" || true
fi
echo "results in $OUT"
