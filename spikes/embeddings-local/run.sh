#!/usr/bin/env bash
# Spike embeddings-local (roadmap 0.3). Runs bench.py in the spike image with no network, one model at a time,
# and writes spikes/embeddings-local/results/<run-id>/<model>.json (numbers and page ids only).
#   bash spikes/embeddings-local/run.sh <pdf>                         # full benchmark: all models, all pages, 30 questions
#   IMAGE=reader-spike-embeddings:smoke MODELS=intfloat/multilingual-e5-small PAGES=99-118 QLIMIT=3 \
#     bash spikes/embeddings-local/run.sh <pdf>                       # smoke
# Do not run the full benchmark while another CPU-bound measurement uses the Docker VM.
set -euo pipefail
PDF="$1"
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${IMAGE:-reader-spike-embeddings}"
MODELS="${MODELS:-intfloat/multilingual-e5-small deepvk/USER-bge-m3 BAAI/bge-m3}"
OUT="$HERE/results/$(date +%Y%m%dT%H%M%S)${TAG:+-$TAG}"
mkdir -p "$OUT"
echo "pdf_sha256=$(shasum -a 256 "$PDF" | awk '{print $1}') docker_vm_cpus=$(docker info --format '{{.NCPU}}')"

for model in $MODELS; do
  name="${model//\//__}"
  docker run --rm --platform linux/arm64 --network none \
    -v "$PDF:/in/source.pdf:ro" -v "$HERE/questions.yaml:/in/questions.yaml:ro" \
    "$IMAGE" /in/source.pdf /in/questions.yaml --model "$model" \
    ${PAGES:+--pages "$PAGES"} ${QLIMIT:+--questions-limit "$QLIMIT"} > "$OUT/$name.json"
  jq -c 'del(.per_question)' "$OUT/$name.json"
done
