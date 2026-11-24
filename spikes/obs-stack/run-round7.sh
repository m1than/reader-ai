#!/usr/bin/env bash
# Spike obs-stack, round 7: where compile Runs live (SRV-OBS phase 2, FR-OBS-20). One fresh spike stack with the
# zero-price catalog; round7.py links experiment 0's stage traces to a Run in its own experiment `runs`;
# round7-tabs.js screenshots that Run and both experiments' lists. Zero credit; own project; no published port;
# internal networks only; the stack is stopped at exit.
#   bash spikes/obs-stack/run-round7.sh
set -euo pipefail
cd "$(dirname "$0")"
OUT="$PWD/results/$(date +%Y%m%dT%H%M%S)-round7-runs"
mkdir -p "$OUT"
export OUT BACKEND=sqlite
export CSP_CANDIDATE="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; worker-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'"
DC=(docker compose -p reader-spike-obs -f compose.yaml -f compose.catalog.yaml -f compose.phase2.yaml)
NM=${PLAYWRIGHT_NM:-/Users/m1than/Developer/reader-ai-int/web/node_modules}
PW_IMAGE=mcr.microsoft.com/playwright:v1.65.1-noble
down() { "${DC[@]}" --profile sqlite --profile driver down -v --remove-orphans >/dev/null 2>&1 || true; }
trap down EXIT

"${DC[@]}" --profile sqlite --profile driver build -q
down
"${DC[@]}" --profile sqlite up -d
"${DC[@]}" --profile sqlite --profile driver run --rm -v "$PWD/round7.py:/spike/round7.py:ro" --entrypoint python \
  driver2 /spike/round7.py > "$OUT/data.stdout" 2> "$OUT/data.stderr"
docker run --rm --network container:reader-spike-obs-caddy-1 --read-only --tmpfs /tmp --cap-drop ALL \
  --security-opt no-new-privileges --user pwuser --ipc private \
  -v "$NM:/nm:ro" -v "$PWD/round7-tabs.js:/spike/round7-tabs.js:ro" -v "$OUT:/out" \
  "$PW_IMAGE" node /spike/round7-tabs.js > "$OUT/tabs.stdout" 2> "$OUT/tabs.stderr" || true
echo "results in $OUT"
