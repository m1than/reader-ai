#!/usr/bin/env bash
# Spike kernel-roundtrip (docs/roadmap.md 0.3). Builds the image, runs it with the ADR-0014 hardening,
# then times every kernel through client.py. Writes results.json next to this script.
#   bash spikes/kernel-roundtrip/run.sh            # build + measure
#   SKIP_BUILD=1 bash spikes/kernel-roundtrip/run.sh
set -euo pipefail
cd "$(dirname "$0")"
IMAGE=reader-spike-kernel
NAME=reader-spike-kernel

if [[ -z "${SKIP_BUILD:-}" ]]; then
  start=$(date +%s)
  docker build --platform linux/arm64 -t "$IMAGE" .
  echo "build_seconds=$(( $(date +%s) - start ))"
fi
docker image inspect "$IMAGE" --format 'image_bytes={{.Size}}'

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --platform linux/arm64 \
  --read-only --tmpfs /home/lab:uid=1000,size=512m --tmpfs /tmp:uid=1000,size=1g \
  --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /run/evcxr:uid=1000,exec,size=1g -e EVCXR_TMPDIR=/run/evcxr \
  --memory 2g --cpus 2 --pids-limit 256 -e CARGO_NET_OFFLINE=true \
  -p 127.0.0.1:8888:8888 "$IMAGE" >/dev/null
trap 'docker logs "$NAME" > gateway.log 2>&1; docker rm -f "$NAME" >/dev/null' EXIT

for _ in $(seq 60); do curl -fsS http://127.0.0.1:8888/api >/dev/null 2>&1 && break; sleep 1; done
docker stats --no-stream --format 'idle_mem={{.MemUsage}}' "$NAME"

# Peak memory sampled every second while the client runs.
( while docker stats --no-stream --format '{{.MemUsage}}' "$NAME" 2>/dev/null; do sleep 1; done ) > mem.samples &
sampler=$!
uv run --script client.py http://127.0.0.1:8888 > results.json
kill "$sampler" 2>/dev/null || true
echo "peak_mem=$(sort -h mem.samples | tail -1)"
rm -f mem.samples
jq -r '.results[] | .kernel as $k | "\($k) start=\(.start_s)s \(.error // "") " +([.cells[] | "\(.cell)=\(.total_s)s ok=\(.ok)"] | join(" "))' results.json
