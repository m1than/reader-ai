#!/usr/bin/env bash
# Spike ingest-docling (ADR-0008). Usage:
#   bash spikes/ingest-docling/run.sh <pdf> full
#   bash spikes/ingest-docling/run.sh <pdf> sample 128,130,...
#   bash spikes/ingest-docling/run.sh <pdf> ocr 30,95,...
# The PDF is copyrighted: it is mounted read-only and never copied into the repository. Converted text goes
# to data/spikes/ingest-docling/ (gitignored). Measurements go to spikes/ingest-docling/results/<mode>.json.
set -euo pipefail
PDF="$1"; MODE="$2"; PAGES="${3:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="$REPO/data/spikes/ingest-docling"
NAME="reader-spike-docling-$MODE"
mkdir -p "$OUT" "$HERE/results"
echo "pdf_sha256=$(shasum -a 256 "$PDF" | awk '{print $1}')"

docker rm -f "$NAME" >/dev/null 2>&1 || true
# No network at run time: models are in the image (ADR-0014 spirit; the ingest worker needs no egress).
docker run -d --name "$NAME" --platform linux/arm64 --network none \
  -v "$PDF:/in/source.pdf:ro" -v "$OUT:/out" reader-spike-docling "$MODE" /in/source.pdf --pages "$PAGES" ${EXTRA:-} >/dev/null

mem_mib() { docker stats --no-stream --format '{{.MemUsage}}' "$NAME" 2>/dev/null | awk '{ v = $1
  if (v ~ /GiB/) { sub(/GiB/, "", v); v *= 1024 } else if (v ~ /KiB/) { sub(/KiB/, "", v); v /= 1024 } else { sub(/MiB/, "", v) }
  print v + 0 }'; }
peak=0
while [[ "$(docker inspect -f '{{.State.Running}}' "$NAME")" == true ]]; do
  m=$(mem_mib); [[ -n "$m" ]] && peak=$(awk -v a="$peak" -v b="$m" 'BEGIN { print (b > a ? b : a) }')
  sleep 2
done
code=$(docker inspect -f '{{.State.ExitCode}}' "$NAME")
docker logs "$NAME" > "$OUT/$MODE.log" 2>&1
docker rm "$NAME" >/dev/null
tail -1 "$OUT/$MODE.log" | jq --argjson peak "$peak" --argjson code "$code" \
  --arg cpus "$(docker info --format '{{.NCPU}}')" \
  '{exit_code: $code, peak_mem_mib: $peak, docker_vm_cpus: ($cpus | tonumber)} + .' > "$HERE/results/$MODE.json"
jq -c 'del(.batches, .pages)' "$HERE/results/$MODE.json"
