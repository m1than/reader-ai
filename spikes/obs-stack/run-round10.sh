#!/usr/bin/env bash
# Spike obs-stack, round 10: the phase 2 writes round 8 did not replay (setup rename/tags/finalize and its drift check,
# dataset search, retention deletes) and three ops checks, on round 9's production flags. Zero credit; own project;
# no published port; internal networks only; the stack is stopped at exit.
#   bash spikes/obs-stack/run-round10.sh
set -euo pipefail
cd "$(dirname "$0")"
OUT="$PWD/results/$(date +%Y%m%dT%H%M%S)-round10-setup"
mkdir -p "$OUT"
export OUT BACKEND=sqlite CSP_CANDIDATE="default-src 'self'"
DC=(docker compose -p reader-spike-obs -f compose.yaml -f compose.catalog.yaml -f compose.phase2.yaml -f compose.round9.yaml)
down() {
  docker logs reader-spike-obs-mlflow-sqlite-1 >"$OUT/mlflow.log" 2>&1 || true
  "${DC[@]}" --profile sqlite --profile driver down -v --remove-orphans >/dev/null 2>&1 || true
}
trap down EXIT
"${DC[@]}" --profile sqlite --profile driver build -q
down
"${DC[@]}" --profile sqlite up -d >/dev/null 2>&1
"${DC[@]}" --profile sqlite --profile driver run --rm -v "$PWD/round10.py:/spike/round10.py:ro" --entrypoint python \
  driver2 /spike/round10.py > "$OUT/data.stdout" 2> "$OUT/data.stderr"
echo "results in $OUT"
