#!/usr/bin/env bash
# Spike obs-stack, round 2 (SRV-OBS review B2–B4, B6): the gate's Origin rules, header stripping, text/plain bodies,
# retention with a real cutoff, then a headless-browser pass for the UI's CSP needs and the trace deep link.
# Zero credit; own project; no published port: the browser shares the caddy container's network namespace.
#   bash spikes/obs-stack/run-browser.sh            CSP_CANDIDATE="<policy>" to test a candidate on :8446
set -euo pipefail
cd "$(dirname "$0")"
OUT="$PWD/results/$(date +%Y%m%dT%H%M%S)-round2"
mkdir -p "$OUT"
export OUT BACKEND=sqlite DURATION_S=${DURATION_S:-7} BULK_LESSONS=${BULK_LESSONS:-1} KEEP_TRACES=1
DC=(docker compose -p reader-spike-obs -f compose.yaml)
NM=${PLAYWRIGHT_NM:-/Users/m1than/Developer/reader-ai-int/web/node_modules}
PW_IMAGE=mcr.microsoft.com/playwright:v1.65.1-noble
cleanup() { "${DC[@]}" --profile sqlite --profile postgres --profile driver down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

"${DC[@]}" --profile sqlite --profile driver build -q
"${DC[@]}" --profile sqlite up -d
"${DC[@]}" --profile sqlite --profile driver run --rm driver > "$OUT/driver.stdout" 2> "$OUT/driver.stderr"
docker run --rm --network container:reader-spike-obs-caddy-1 --read-only --tmpfs /tmp --cap-drop ALL \
  --security-opt no-new-privileges --user pwuser --ipc private \
  -v "$NM:/nm:ro" -v "$PWD/browser.js:/spike/browser.js:ro" -v "$OUT:/out" \
  "$PW_IMAGE" node /spike/browser.js > "$OUT/browser.stdout" 2> "$OUT/browser.stderr" || true
"${DC[@]}" logs --no-color gate > "$OUT/gate.log" 2>&1 || true
# The Caddyfile as Caddy reads it (the `caddy adapt` oracle of the paired OPS-DEPLOY test).
docker run --rm --network none -e CSP_CANDIDATE=x -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2.11.9-alpine \
  caddy adapt --config /etc/caddy/Caddyfile > "$OUT/caddy-adapt.json" 2> "$OUT/caddy-adapt.stderr" || true
echo "results in $OUT"
