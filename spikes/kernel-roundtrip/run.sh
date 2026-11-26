#!/usr/bin/env bash
# Spike kernel-roundtrip (docs/roadmap.md 0.3). Builds the image, runs the gateway with the ADR-0014
# hardening on an internal network (no internet), runs client.py from a second container on that
# network, and writes everything measured to $OUT/<mode>-<run>.json (default results/).
#   bash spikes/kernel-roundtrip/run.sh               # build, then 3 warm runs and 1 cold run
#   SKIP_BUILD=1 RUNS=1 bash spikes/kernel-roundtrip/run.sh   # also skips the client image: rebuild after editing client.py
#   OUT=results-314 bash spikes/kernel-roundtrip/run.sh   # ADR-0020 re-run on python:3.14-slim-trixie
# Cold = the pre-built evcxr cache is not copied in, so the pilot crates compile from source (offline).
set -euo pipefail
cd "$(dirname "$0")"
IMAGE=reader-spike-kernel
GW=reader-spike-kernel
CLIENT=reader-spike-client
NET=reader-spike-sandbox
RUNS="${RUNS:-3}"
OUT="${OUT:-results}"
mkdir -p "$OUT"

build_s=null
if [[ -z "${SKIP_BUILD:-}" ]]; then
  start=$(date +%s)
  docker build --platform linux/arm64 -t "$IMAGE" .
  docker build --platform linux/arm64 -t "$CLIENT" -f client.Dockerfile .
  build_s=$(( $(date +%s) - start ))
fi
image_bytes=$(docker image inspect "$IMAGE" --format '{{.Size}}')

cleanup() { docker rm -f "$GW" "$CLIENT" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup
docker network create --internal "$NET" >/dev/null

# The client is its own image (client.Dockerfile, dependencies baked in), run on the internal network;
# the gateway container is only ever on that network. Calls are sequential, so one container name is enough,
# and `cleanup` removes a client left running by an interrupt.
client() { docker run --rm --name "$CLIENT" --network "$NET" "$CLIENT" "$@"; }

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
    client python -c "import httpx; httpx.get('http://$GW:8888/api').raise_for_status()" \
      >/dev/null 2>&1 && break
    sleep 1
  done
  local idle; idle=$(mem_mib)
  ( while mem_mib 2>/dev/null; do sleep 1; done ) > mem.samples &
  local sampler=$!
  client python /client.py "http://$GW:8888" > client.json
  kill "$sampler" 2>/dev/null || true
  local peak; peak=$(sort -g mem.samples | tail -1)
  # The 3.13 run read the evcxr tmpfs size from inside the gateway container; this run does not.
  local evcxr_mib=null
  docker logs "$GW" > "$OUT/gateway-$mode-$run.log" 2>&1
  jq --arg mode "$mode" --argjson run "$run" --argjson build_s "$build_s" --argjson image_bytes "$image_bytes" \
     --argjson idle "${idle:-null}" --argjson peak "${peak:-null}" --argjson evcxr "${evcxr_mib:-null}" \
     '{mode: $mode, run: $run, build_s: $build_s, image_bytes: $image_bytes,
       idle_mem_mib: $idle, peak_mem_mib: $peak, evcxr_tmpfs_after_mib: $evcxr} + .' client.json > "$OUT/$mode-$run.json"
  rm -f client.json mem.samples
  jq -r '"\(.mode)-\(.run) idle=\(.idle_mem_mib)MiB peak=\(.peak_mem_mib)MiB",
    (.results[], .concurrent_rust[] | "  \(.kernel) start=\(.start_s)s \(.error // "")"
      + ([.cells[] | " \(.cell)=\(.total_s)s ok=\(.ok)"] | join("")))' "$OUT/$mode-$run.json"
}

for run in $(seq "$RUNS"); do measure warm "$run"; done
measure cold 1
