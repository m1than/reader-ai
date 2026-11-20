#!/usr/bin/env bash
# Spike kernel-roundtrip (docs/roadmap.md 0.3). Builds the image, runs the gateway with the ADR-0014
# hardening on an internal network (no internet), runs client.py from a second container on that
# network, and writes everything measured to results/<mode>-<run>.json.
#   bash spikes/kernel-roundtrip/run.sh               # build, then 3 warm runs and 1 cold run
#   SKIP_BUILD=1 RUNS=1 bash spikes/kernel-roundtrip/run.sh
# Cold = the pre-built evcxr cache is not copied in, so the pilot crates compile from source (offline).
set -euo pipefail
cd "$(dirname "$0")"
IMAGE=reader-spike-kernel
GW=reader-spike-kernel
CLIENT=reader-spike-client
NET=reader-spike-sandbox
RUNS="${RUNS:-3}"
mkdir -p results

build_s=null
if [[ -z "${SKIP_BUILD:-}" ]]; then
  start=$(date +%s)
  docker build --platform linux/arm64 -t "$IMAGE" .
  build_s=$(( $(date +%s) - start ))
fi
image_bytes=$(docker image inspect "$IMAGE" --format '{{.Size}}')

cleanup() { docker rm -f "$GW" "$CLIENT" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup
docker network create --internal "$NET" >/dev/null

# The client container starts on the default network to install its dependencies, then joins the
# internal network; the gateway container is only ever on the internal network.
docker run -d --name "$CLIENT" python:3.13-slim-bookworm sleep infinity >/dev/null
docker exec "$CLIENT" pip install -q --no-cache-dir httpx==0.28.1 websockets==17.4 2>/dev/null
docker network connect "$NET" "$CLIENT"
docker cp client.py "$CLIENT":/client.py

mem_mib() { docker stats --no-stream --format '{{.MemUsage}}' "$GW" | awk '{ v = $1
  if (v ~ /GiB/) { sub(/GiB/, "", v); v *= 1024 } else if (v ~ /KiB/) { sub(/KiB/, "", v); v /= 1024 } else { sub(/MiB/, "", v) }
  print v + 0 }'; }

measure() { # mode run
  local mode="$1" run="$2"
  docker rm -f "$GW" >/dev/null 2>&1 || true
  # ADR-0014: non-root (image USER lab), read-only root, cap-drop ALL, no-new-privileges, default seccomp,
  # 2 CPUs, 2 GiB without extra swap, 256 PIDs. tmpfs sizes sum to 1.5 GiB, under the memory limit.
  docker run -d --name "$GW" --platform linux/arm64 --network "$NET" \
    --read-only --tmpfs /home/lab:uid=1000,size=256m --tmpfs /tmp:uid=1000,size=256m \
    --tmpfs /run/evcxr:uid=1000,exec,size=1g -e "COLD=$([[ $mode == cold ]] && echo 1)" \
    --cap-drop ALL --security-opt no-new-privileges \
    --memory 2g --memory-swap 2g --cpus 2 --pids-limit 256 -e CARGO_NET_OFFLINE=true \
    "$IMAGE" >/dev/null
  for _ in $(seq 60); do
    docker exec "$CLIENT" python -c "import httpx; httpx.get('http://$GW:8888/api').raise_for_status()" \
      >/dev/null 2>&1 && break
    sleep 1
  done
  local idle; idle=$(mem_mib)
  ( while mem_mib 2>/dev/null; do sleep 1; done ) > mem.samples &
  local sampler=$!
  docker exec "$CLIENT" python /client.py "http://$GW:8888" > client.json
  kill "$sampler" 2>/dev/null || true
  local peak; peak=$(sort -g mem.samples | tail -1)
  # Per-kernel work dirs stay in the tmpfs after their kernels are deleted (the launcher execs evcxr).
  local evcxr_mib; evcxr_mib=$(docker exec "$GW" du -sm /run/evcxr | awk '{print $1}')
  docker logs "$GW" > "results/gateway-$mode-$run.log" 2>&1
  jq --arg mode "$mode" --argjson run "$run" --argjson build_s "$build_s" --argjson image_bytes "$image_bytes" \
     --argjson idle "${idle:-null}" --argjson peak "${peak:-null}" --argjson evcxr "${evcxr_mib:-null}" \
     '{mode: $mode, run: $run, build_s: $build_s, image_bytes: $image_bytes,
       idle_mem_mib: $idle, peak_mem_mib: $peak, evcxr_tmpfs_after_mib: $evcxr} + .' client.json > "results/$mode-$run.json"
  rm -f client.json mem.samples
  jq -r '"\(.mode)-\(.run) idle=\(.idle_mem_mib)MiB peak=\(.peak_mem_mib)MiB",
    (.results[], .concurrent_rust[] | "  \(.kernel) start=\(.start_s)s \(.error // "")"
      + ([.cells[] | " \(.cell)=\(.total_s)s ok=\(.ok)"] | join("")))' "results/$mode-$run.json"
}

for run in $(seq "$RUNS"); do measure warm "$run"; done
measure cold 1
