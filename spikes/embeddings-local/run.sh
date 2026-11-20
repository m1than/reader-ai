#!/usr/bin/env bash
# Spike embeddings-local (roadmap 0.3). Runs bench.py in the spike image with no network, one model at a time,
# and writes spikes/embeddings-local/results/<run-id>/<model>.json (numbers and page ids only).
#   bash spikes/embeddings-local/run.sh <pdf>                              # all models, all pages, questions.yaml (v1)
#   QUESTIONS=questions-v2.yaml TAG=v2 bash spikes/embeddings-local/run.sh <pdf>   # the harder set
#   IMAGE=reader-spike-embeddings:smoke MODELS=intfloat/multilingual-e5-small PAGES=99-118 QLIMIT=3 \
#     bash spikes/embeddings-local/run.sh <pdf>                            # smoke
# Chunk vectors are cached per (model, corpus sha256, chunking, page range) in the gitignored
# data/spikes/embeddings-local/cache/ (vectors and page numbers only), so reruns with another question set take
# seconds. A cache hit reports the embedding speed measured when the vectors were made ("cache": "hit").
# NOCACHE=1 forces a fresh, timed embedding. Do not run an uncached full benchmark while another CPU-bound
# measurement uses the Docker VM.
set -euo pipefail
PDF="$1"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
IMAGE="${IMAGE:-reader-spike-embeddings}"
MODELS="${MODELS:-intfloat/multilingual-e5-small deepvk/USER-bge-m3 BAAI/bge-m3}"
QUESTIONS="${QUESTIONS:-questions.yaml}"
CACHE="$REPO/data/spikes/embeddings-local/cache"
OUT="$HERE/results/$(date +%Y%m%dT%H%M%S)${TAG:+-$TAG}"
mkdir -p "$OUT" "$CACHE"
echo "pdf_sha256=$(shasum -a 256 "$PDF" | awk '{print $1}') docker_vm_cpus=$(docker info --format '{{.NCPU}}') questions=$QUESTIONS"

mount=(); cache_arg=()
if [[ -z "${NOCACHE:-}" ]]; then mount=(-v "$CACHE:/cache"); cache_arg=(--cache /cache); fi

for model in $MODELS; do
  name="${model//\//__}"
  docker run --rm --platform linux/arm64 --network none \
    -v "$PDF:/in/source.pdf:ro" -v "$HERE/$QUESTIONS:/in/questions.yaml:ro" ${mount[@]+"${mount[@]}"} \
    "$IMAGE" /in/source.pdf /in/questions.yaml --model "$model" ${cache_arg[@]+"${cache_arg[@]}"} \
    ${PAGES:+--pages "$PAGES"} ${QLIMIT:+--questions-limit "$QLIMIT"} > "$OUT/$name.json"
  jq -c '{model, cache, chunks, chunks_per_s, est_full_pilot_min, questions, recall_at_1, recall_at_3, recall_at_10, mrr, worst_rank}' "$OUT/$name.json"
done
