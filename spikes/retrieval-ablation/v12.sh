#!/usr/bin/env bash
# Spike retrieval-ablation, free step after the paid `prefixes` and `summaries` stages (zero model calls): embed V1
# (prefix + span) and V2 (V1 + L2 summary entries) with the embed service's encoder, then rerun analyze.py, which adds
# V1/V2 to recall.json and mcnemar.json and writes results/topk.json for the paid `judge` stage.
#   bash spikes/retrieval-ablation/v12.sh
# Same containers and mounts as run.sh, --network none; the prefixes and summaries stay in the gitignored data/.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
ART="$REPO/data/spikes/retrieval-ablation/convert/out"
WORK="$REPO/data/spikes/retrieval-ablation/work"
TEXT="$REPO/data/spikes/retrieval-ablation/paid/text"
for f in prefixes.jsonl summaries.jsonl; do
  [[ -s "$TEXT/$f" ]] || { echo "missing $TEXT/$f: run the paid prefixes and summaries stages first" >&2; exit 2; }
done

docker run --rm --network none -e HOME=/tmp \
  -v "$ART/spans.jsonl:/in/spans.jsonl:ro" -v "$ART/outline.json:/in/outline.json:ro" -v "$TEXT:/text:ro" \
  -v "$HERE/embed_v12.py:/w/embed_v12.py:ro" -v "$WORK:/out" --entrypoint python reader-embed:local \
  /w/embed_v12.py --spans /in/spans.jsonl --outline /in/outline.json --text /text --work /out

docker run --rm --network none -e HOME=/tmp \
  -v "$ART/spans.jsonl:/in/spans.jsonl:ro" -v "$ART/outline.json:/in/outline.json:ro" \
  -v "$REPO/server/src/reader/agent/pricing.yaml:/in/pricing.yaml:ro" -v "$HERE/analyze.py:/w/analyze.py:ro" \
  -v "$WORK:/work:ro" -v "$HERE/results:/out" --entrypoint python reader-embed:local /w/analyze.py \
  --work /work --spans /in/spans.jsonl --outline /in/outline.json --pricing /in/pricing.yaml --out /out
