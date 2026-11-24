#!/usr/bin/env bash
# Spike obs-stack, round 4, Jaeger option (the owner's "Jaeger-like view" question): Jaeger v2 all-in-one as Rotel's
# second exporter beside MLflow; the same synthetic traces plus BULK compile-like traces; Jaeger's and MLflow's
# memory; the UI through the spike gate under SRV-OBS's CSP. Zero credit; own project; no published port.
#   bash spikes/obs-stack/run-jaeger.sh            BULK=300 JAEGER_MAX_TRACES=10000 by default
set -euo pipefail
cd "$(dirname "$0")"
OUT="$PWD/results/$(date +%Y%m%dT%H%M%S)-round4-jaeger"
mkdir -p "$OUT"
export OUT BACKEND=sqlite BULK="${BULK:-300}" JAEGER_MAX_TRACES="${JAEGER_MAX_TRACES:-10000}"
export CSP_CANDIDATE="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; worker-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'"
# Jaeger UI 2.23.0: its two inline scripts by hash, and `base-uri 'self'` for its <base href> (measured in the first
# two Jaeger passes: with `base-uri 'none'` its relative module URLs resolved under /trace/ and failed).
export CSP_JAEGER="default-src 'self'; script-src 'self' 'sha256-eoBUh2b2awnlKCd0fkQUIXAAxPdxWV4aIas48E/3hLU=' 'sha256-07X4W69Q7ST165IvjVZwZU+G34LWjkh2Xj2s9fMwqYQ='; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; worker-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'"
DC=(docker compose -p reader-spike-obs -f compose.yaml -f compose.jaeger.yaml)
NM=${PLAYWRIGHT_NM:-/Users/m1than/Developer/reader-ai-int/web/node_modules}
PW_IMAGE=mcr.microsoft.com/playwright:v1.65.1-noble
cleanup() { "${DC[@]}" --profile sqlite --profile jaeger --profile driver down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT
stats() { docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' | grep reader-spike-obs >> "$OUT/stats.txt"; echo "-- $1" >> "$OUT/stats.txt"; }

"${DC[@]}" --profile sqlite --profile jaeger --profile driver build -q
"${DC[@]}" --profile sqlite --profile jaeger up -d
sleep 8; stats idle
RUN=("${DC[@]}" --profile sqlite --profile jaeger --profile driver run --rm -e BULK
  -v "$PWD/analytics.py:/spike/analytics.py:ro" -v "$PWD/jaeger_check.py:/spike/jaeger_check.py:ro" --entrypoint python driver)
"${RUN[@]}" /spike/analytics.py > "$OUT/analytics.stdout" 2> "$OUT/analytics.stderr"
stats after-analytics
"${RUN[@]}" /spike/jaeger_check.py > "$OUT/jaeger.stdout" 2> "$OUT/jaeger.stderr"
stats "after-bulk-$BULK"
"${DC[@]}" logs --no-color rotel > "$OUT/rotel.log" 2>&1 || true
"${DC[@]}" logs --no-color jaeger > "$OUT/jaeger.log" 2>&1 || true
docker run --rm --network container:reader-spike-obs-caddy-1 --read-only --tmpfs /tmp --cap-drop ALL \
  --security-opt no-new-privileges --user pwuser --ipc private \
  -v "$NM:/nm:ro" -v "$PWD/jaeger-browser.js:/spike/jaeger-browser.js:ro" -v "$OUT:/out" \
  "$PW_IMAGE" node /spike/jaeger-browser.js > "$OUT/jaeger-browser.stdout" 2> "$OUT/jaeger-browser.stderr" || true
stats after-browser
echo "results in $OUT"
