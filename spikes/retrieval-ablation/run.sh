#!/usr/bin/env bash
# Spike retrieval-ablation, free arms (ADR-0039 B10; docs/plans/adr-0039-execution.md). Zero model calls.
#   bash spikes/retrieval-ablation/run.sh <artifact dir with spans.jsonl and outline.json>
# The artifact comes from the stack's own pinned converter (SRV-INGEST, docling 2.151.1+r2) run on the host into the
# gitignored data/spikes/retrieval-ablation/convert/out (see RESULT.md "Method"); it is never committed.
# Three spike containers, all --network none, none of them part of the owner's stack:
#   1. reader-embed:local (the embed service's image and encoder): embed.py, vectors and dense ranks;
#   2. pgvector/pgvector (pinned digest), cluster on tmpfs: pg.sql, full text and the vector step's latency;
#   3. reader-embed:local again: analyze.py, recall, McNemar, cost projection -> results/*.json.
# Work files (vectors, ranks; no text) go to the gitignored data/spikes/retrieval-ablation/work.
set -euo pipefail
ART="$(cd "$1" && pwd)"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
WORK="$REPO/data/spikes/retrieval-ablation/work"
PG_IMAGE="pgvector/pgvector@sha256:ab49524d18df7200b74ac9a475e1db6297fc1eb57da4c214c63d10ecc4b754fa"
mkdir -p "$WORK" "$HERE/results"
echo "docker_vm_cpus=$(docker info --format '{{.NCPU}}')"

docker run --rm --network none -e HOME=/tmp \
  -v "$ART/spans.jsonl:/in/spans.jsonl:ro" -v "$REPO/spec/fixtures/eval/pilot-eval-v1.yaml:/in/eval.yaml:ro" \
  -v "$REPO/server/src/reader/search/chunking.py:/src/chunking.py:ro" -v "$HERE/embed.py:/w/embed.py:ro" \
  -v "$WORK:/out" --entrypoint python reader-embed:local /w/embed.py \
  --spans /in/spans.jsonl --questions /in/eval.yaml --chunking /src/chunking.py --out /out

docker run --rm --network none --user postgres --tmpfs /tmp:rw,size=2g \
  -v "$ART/spans.jsonl:/in/spans.jsonl:ro" -v "$WORK/spans_vec.tsv:/in/spans_vec.tsv:ro" \
  -v "$WORK/queries_vec.tsv:/in/queries_vec.tsv:ro" -v "$HERE/pg.sql:/w/pg.sql:ro" -v "$HERE/pg.sh:/w/pg.sh:ro" \
  -v "$WORK:/out" --entrypoint bash "$PG_IMAGE" /w/pg.sh

docker run --rm --network none -e HOME=/tmp \
  -v "$ART/spans.jsonl:/in/spans.jsonl:ro" -v "$ART/outline.json:/in/outline.json:ro" \
  -v "$REPO/server/src/reader/agent/pricing.yaml:/in/pricing.yaml:ro" -v "$HERE/analyze.py:/w/analyze.py:ro" \
  -v "$WORK:/work:ro" -v "$HERE/results:/out" --entrypoint python reader-embed:local /w/analyze.py \
  --work /work --spans /in/spans.jsonl --outline /in/outline.json --pricing /in/pricing.yaml --out /out
cp "$WORK/latency.json" "$HERE/results/latency.json"
