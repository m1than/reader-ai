#!/usr/bin/env bash
# Spike embed-service: run measure.py in the hardened container (OPS-DEPLOY flags) at several CPU counts and dtypes.
# Writes spikes/embed-service/results/<run-id>/<case>.json plus <case>.log (stderr), numbers only; text is synthetic.
#   bash spikes/embed-service/run.sh                     # cap 1,024, dense worst texts (the SRV-SEARCH limit)
#   CAP=512 POOL=cyrillic bash spikes/embed-service/run.sh   # the first, 512-cap comparison run
# No other CPU-bound work should run on the Docker VM meanwhile.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${IMAGE:-reader-spike-embed-service}"
CAP="${CAP:-1024}"
POOL="${POOL:-dense}"
CPUS="${CPUS:-2 4 8}"
OUT="$HERE/results/$(date +%Y%m%dT%H%M%S)-cap$CAP"
MEM="${MEM:-8g}"   # generous ceiling for measuring; the recommendation is derived from the measured peaks
mkdir -p "$OUT"
echo "docker_vm_cpus=$(docker info --format '{{.NCPU}}') image_bytes=$(docker image inspect "$IMAGE" --format '{{.Size}}')"

hardened=(--rm --platform linux/arm64 --network none --read-only --cap-drop ALL
  --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,size=64m --memory "$MEM" --memory-swap "$MEM")

run() {  # name cpus dtype
  docker run "${hardened[@]}" --cpus "$2" -e THREADS="$2" "$IMAGE" --dtype "$3" --max-seq "$CAP" \
    --worst-pool "$POOL" > "$OUT/$1.json" 2> "$OUT/$1.log"
  jq -c --arg case "$1" '{case: $case, worst_text_tokens_untruncated, load_s, query_idle_s, batch8_prose_s,
    batch8_worst_s, batch32_worst_s, chunks_per_s, rss_mib_after_load, rss_mib_after_batch8_worst,
    rss_mib_after_batch32_worst, cgroup_peak_mib_after_batch8_worst, cgroup_memory_peak_mib}' "$OUT/$1.json"
}

docker run "${hardened[@]}" --cpus 2 "$IMAGE" --weights-only-check > "$OUT/weights-only.json"
cat "$OUT/weights-only.json"
if [[ -n "${ONLY_BATCH8:-}" ]]; then  # isolated worst batch of 8 (its peak is otherwise masked by earlier phases)
  docker run "${hardened[@]}" --cpus 8 -e THREADS=8 "$IMAGE" --max-seq "$CAP" --worst-pool "$POOL" \
    --only-batch8-worst > "$OUT/fp32-cpus8-batch8-only.json" 2> "$OUT/fp32-cpus8-batch8-only.log"
  cat "$OUT/fp32-cpus8-batch8-only.json"; echo "$OUT"; exit 0
fi
for c in $CPUS; do run "fp32-cpus$c" "$c" float32; done
run "bf16-cpus8" 8 bfloat16
echo "$OUT"
