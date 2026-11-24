#!/usr/bin/env bash
# Spike obs-stack, round 5 (V.obs-analytics design review round 1: B1 catalog fallback, B2 sessions, B8 Jaeger).
# Zero credit; own project (reader-spike-obs); no published port; every stack is taken down on exit.
#   bash spikes/obs-stack/run-round5.sh
set -euo pipefail
cd "$(dirname "$0")"
OUT="$PWD/results/$(date +%Y%m%dT%H%M%S)-round5"
mkdir -p "$OUT/catalog" "$OUT/empty"
export OUT BACKEND=sqlite JAEGER_MAX_TRACES=100000 CATALOG_DIR="$OUT/catalog"
export CSP_CANDIDATE="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; worker-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'"
export CSP_JAEGER="default-src 'self'; script-src 'self' 'sha256-eoBUh2b2awnlKCd0fkQUIXAAxPdxWV4aIas48E/3hLU=' 'sha256-07X4W69Q7ST165IvjVZwZU+G34LWjkh2Xj2s9fMwqYQ='; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; worker-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'"
MLFLOW_IMG=ghcr.io/mlflow/mlflow:v3.18.0@sha256:257204c77980b0f3450a26eb6089e487603fcdd4509529eadaf88f90669f5c13
DC=(docker compose -p reader-spike-obs -f compose.yaml -f compose.jaeger.yaml -f compose.round5.yaml)
P=(--profile sqlite --profile jaeger)
NM=${PLAYWRIGHT_NM:-/Users/m1than/Developer/reader-ai-int/web/node_modules}
PW_IMAGE=mcr.microsoft.com/playwright:v1.65.1-noble
down() { "${DC[@]}" "${P[@]}" --profile driver down -v --remove-orphans >/dev/null 2>&1 || true; }
trap down EXIT
stats() { { echo "-- $1"; docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' | grep reader-spike-obs; } >> "$OUT/stats.txt"; }
drive() { # PHASE [extra env...]
  local phase=$1; shift
  "${DC[@]}" "${P[@]}" --profile driver run --rm -e PHASE="$phase" "$@" \
    -v "$PWD/analytics.py:/spike/analytics.py:ro" -v "$PWD/round5.py:/spike/round5.py:ro" \
    --entrypoint python driver /spike/round5.py >> "$OUT/driver.stdout" 2>> "$OUT/driver.stderr"
}
up() { down; "${DC[@]}" "${P[@]}" up -d >> "$OUT/compose.log" 2>&1 || true; sleep 8; }

# The catalog: every bundled Anthropic model of the pinned image plus pricing.yaml's ids, all at 0.
docker run --rm --network none --user "$(id -u):$(id -g)" -v "$OUT/catalog:/o" -v "$PWD/gen_catalog.py:/g.py:ro" \
  --entrypoint python "$MLFLOW_IMG" -I /g.py /o claude-opus-5-5 claude-sonnet-5-5 > "$OUT/bundled-anthropic.txt"
"${DC[@]}" "${P[@]}" --profile driver build -q

# A: generated catalog, guarded start, per-site gate, Jaeger beside MLflow.
export CATALOG_DIR="$OUT/catalog" GUARD='test -s /catalog/anthropic.json || exit 78; ' JAEGER_MEM=256m
up; stats A-idle
"${DC[@]}" "${P[@]}" --profile driver run --rm -v "$PWD/analytics.py:/spike/analytics.py:ro" \
  --entrypoint python driver /spike/analytics.py > "$OUT/analytics.stdout" 2> "$OUT/analytics.stderr"
drive cost -e TAG=-generated -e CATALOG_NOTE=generated
drive session
drive gate
"${DC[@]}" logs --no-color gate > "$OUT/gate.log" 2>&1 || true
stats A-before-bulk
drive bulk -e BULK=200 -e TAG=-200; sleep 15; stats A-after-bulk-200
drive bulk -e BULK=400 -e TAG=-600; sleep 15; stats A-after-bulk-600
docker run --rm --network container:reader-spike-obs-caddy-1 --read-only --tmpfs /tmp --cap-drop ALL \
  --security-opt no-new-privileges --user pwuser --ipc private \
  -v "$NM:/nm:ro" -v "$PWD/jaeger-pages.js:/spike/jaeger-pages.js:ro" -v "$OUT:/out" \
  "$PW_IMAGE" node /spike/jaeger-pages.js > "$OUT/jaeger-pages.stdout" 2> "$OUT/jaeger-pages.stderr" || true
"${DC[@]}" stop jaeger >> "$OUT/compose.log" 2>&1
drive probe -e TAG=-jaeger-stopped
"${DC[@]}" logs --no-color rotel > "$OUT/rotel-A.log" 2>&1 || true

# B: catalog URI set, mount empty, no guard: does MLflow fall back to its bundled prices?
export CATALOG_DIR="$OUT/empty" GUARD=''
up
drive cost -e TAG=-mount-empty -e CATALOG_NOTE=empty-mount-unguarded

# C: mount empty, guarded: MLflow must not start.
export GUARD='test -s /catalog/anthropic.json || exit 78; '
up; sleep 5
docker inspect reader-spike-obs-mlflow-sqlite-1 --format '{{.State.Status}} exit={{.State.ExitCode}}' > "$OUT/guard.txt" 2>&1 || true

# D: Jaeger OOM-killed under load (48m), then MLflow delivery.
export CATALOG_DIR="$OUT/catalog" JAEGER_MEM=48m
up
drive bulk -e BULK=600 -e TAG=-oom || true
sleep 10
docker inspect reader-spike-obs-jaeger-1 --format '{{.State.Status}} oom={{.State.OOMKilled}} exit={{.State.ExitCode}}' \
  > "$OUT/jaeger-oom.txt" 2>&1 || true
drive probe -e TAG=-jaeger-oom
"${DC[@]}" logs --no-color rotel > "$OUT/rotel-D.log" 2>&1 || true
echo "results in $OUT"
