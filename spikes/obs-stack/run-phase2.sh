#!/usr/bin/env bash
# Spike obs-stack, round 6: SRV-OBS phase 2 (V.obs-mlflow-phase2). One fresh spike stack with the zero-price catalog:
# `before` = the implemented V.obs-analytics data (analytics_tabs.py VARIANT=after), screenshotted; then phase2.py adds
# Runs, datasets, evaluation runs, agent versions, labels and a review queue, screenshotted as `after`. Zero credit;
# own project; no published port; internal networks only; the stack is stopped at exit.
#   bash spikes/obs-stack/run-phase2.sh
set -euo pipefail
cd "$(dirname "$0")"
OUT="$PWD/results/$(date +%Y%m%dT%H%M%S)-round6-phase2"
mkdir -p "$OUT"
export OUT BACKEND=sqlite
export CSP_CANDIDATE="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; worker-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'"
DC=(docker compose -p reader-spike-obs -f compose.yaml -f compose.catalog.yaml -f compose.phase2.yaml)
NM=${PLAYWRIGHT_NM:-/Users/m1than/Developer/reader-ai-int/web/node_modules}
PW_IMAGE=mcr.microsoft.com/playwright:v1.65.1-noble
down() { "${DC[@]}" --profile sqlite --profile driver down -v --remove-orphans >/dev/null 2>&1 || true; }
trap down EXIT
shot() {
  docker run --rm --network container:reader-spike-obs-caddy-1 --read-only --tmpfs /tmp --cap-drop ALL \
    --security-opt no-new-privileges --user pwuser --ipc private -e VARIANT="$1" \
    -v "$NM:/nm:ro" -v "$PWD/phase2-tabs.js:/spike/phase2-tabs.js:ro" -v "$OUT:/out" \
    "$PW_IMAGE" node /spike/phase2-tabs.js > "$OUT/tabs-$1.stdout" 2> "$OUT/tabs-$1.stderr" || true
}

"${DC[@]}" --profile sqlite --profile driver build -q
down
"${DC[@]}" --profile sqlite up -d
"${DC[@]}" --profile sqlite --profile driver run --rm -e VARIANT=after --entrypoint python driver2 \
  /spike/analytics_tabs.py > "$OUT/data-before.stdout" 2> "$OUT/data-before.stderr"
shot before
# Run 1 (20261124T141648) measured evaluate's default parallel trace export exhausting MLflow's SQLite pool
# (QueuePool limit 5 + 10); one worker here.
"${DC[@]}" --profile sqlite --profile driver run --rm -e MLFLOW_GENAI_EVAL_MAX_WORKERS=1 --entrypoint python driver2 \
  /spike/phase2.py > "$OUT/data-after.stdout" 2> "$OUT/data-after.stderr"
shot after
docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' | grep reader-spike-obs > "$OUT/stats.txt" || true
echo "results in $OUT"
