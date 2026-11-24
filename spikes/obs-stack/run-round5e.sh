#!/usr/bin/env bash
# Spike obs-stack, round 5 stack E (review B1): MLflow whose bundled model catalog is replaced in the image by
# catalog/anthropic.json (zero prices). Measures the four CLI-span cases of round5.py `cost`, the provider-less one
# included. Zero credit; own project; no published port; taken down on exit.
#   bash spikes/obs-stack/run-round5e.sh RESULT_DIR      (an existing round-5 result directory)
set -euo pipefail
cd "$(dirname "$0")"
OUT="$(cd "${1:?result dir}" && pwd)"
export OUT BACKEND=sqlite
DC=(docker compose -p reader-spike-obs -f compose.yaml -f compose.round5e.yaml)
down() { "${DC[@]}" --profile sqlite --profile driver down -v --remove-orphans >/dev/null 2>&1 || true; }
trap down EXIT
down
"${DC[@]}" --profile sqlite --profile driver build -q
"${DC[@]}" --profile sqlite up -d >> "$OUT/compose-e.log" 2>&1
"${DC[@]}" --profile sqlite --profile driver run --rm -e PHASE=cost -e TAG=-image-replaced \
  -e CATALOG_NOTE=bundled-replaced-in-image -v "$PWD/analytics.py:/spike/analytics.py:ro" \
  -v "$PWD/round5.py:/spike/round5.py:ro" --entrypoint python driver /spike/round5.py \
  >> "$OUT/driver-e.stdout" 2>> "$OUT/driver-e.stderr"
"${DC[@]}" logs --no-color mlflow-sqlite > "$OUT/mlflow-e.log" 2>&1 || true
down

# F (review B8): Jaeger's memory per span with VEC-OBS-2's full CLI key sets (256m), then an OOM under a 24m limit
# and MLflow's delivery afterwards.
export CATALOG_DIR="$OUT/catalog" GUARD='' JAEGER_MAX_TRACES=100000
export CSP_CANDIDATE="default-src 'self'" CSP_JAEGER="default-src 'self'"
DF=(docker compose -p reader-spike-obs -f compose.yaml -f compose.jaeger.yaml -f compose.round5.yaml)
PF=(--profile sqlite --profile jaeger)
fdrive() { local phase=$1; shift
  "${DF[@]}" "${PF[@]}" --profile driver run --rm -e PHASE="$phase" "$@" \
    -v "$PWD/analytics.py:/spike/analytics.py:ro" -v "$PWD/round5.py:/spike/round5.py:ro" \
    --entrypoint python driver /spike/round5.py >> "$OUT/driver-f.stdout" 2>> "$OUT/driver-f.stderr"; }
fstats() { { echo "-- $1"; docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' | grep reader-spike-obs-jaeger; } \
  >> "$OUT/stats-f.txt"; }
fdown() { "${DF[@]}" "${PF[@]}" --profile driver down -v --remove-orphans >/dev/null 2>&1 || true; }
trap 'fdown; down' EXIT
export JAEGER_MEM=256m
fdown; "${DF[@]}" "${PF[@]}" up -d >> "$OUT/compose-f.log" 2>&1; sleep 10; fstats F-idle
fdrive bulkfull -e BULK=2000 -e TAG=-f-2000; sleep 15; fstats F-after-8000-full-spans
fdrive bulkfull -e BULK=4000 -e TAG=-f-6000; sleep 15; fstats F-after-24000-full-spans
export JAEGER_MEM=24m
fdown; "${DF[@]}" "${PF[@]}" up -d >> "$OUT/compose-f.log" 2>&1; sleep 10
fdrive bulkfull -e BULK=6000 -e TAG=-f-oom || true
sleep 10
docker inspect reader-spike-obs-jaeger-1 --format '{{.State.Status}} oom={{.State.OOMKilled}} exit={{.State.ExitCode}}' \
  > "$OUT/jaeger-oom-f.txt" 2>&1 || true
fdrive probe -e TAG=-jaeger-oom-f
"${DF[@]}" logs --no-color rotel > "$OUT/rotel-F.log" 2>&1 || true
fdown

# B8: the source of Jaeger UI's one `script-src eval` violation (static/schemas-*.js line 2): fetch that bundle from
# a Jaeger container with no network, through a Python container sharing its network namespace.
J=reader-spike-obs-jaeger-src
docker rm -f "$J" >/dev/null 2>&1 || true
docker run -d --name "$J" --network none --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  jaegertracing/jaeger:2.23.0@sha256:3823829974ccfc2485d2e48b165579091f32295022ae80ca49704e2f4834116e >/dev/null
sleep 5
docker run --rm --network "container:$J" python:3.14-slim-trixie python -I -c \
  "import urllib.request,sys; sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:16686/static/schemas-hm98w7NF.js').read().decode())" \
  > "$OUT/jaeger-schemas.js" || true   # read, then deleted by hand (third-party code; RESULT.md quotes the probe)
docker rm -f "$J" >/dev/null 2>&1 || true
echo "results in $OUT"
