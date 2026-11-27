#!/usr/bin/env bash
# Spike tutor-find-m1 (SRV-TUTOR kl_revision measurement_plan.free, M1). Zero model calls, never the owner's stack.
#   bash spikes/tutor-find-m1/run.sh <artifact dir with spans.jsonl, outline.json, manifest.json> [<reuse dir>]
# The artifact is the pilot PDF converted by the stack's own pinned converter (docling 2.151.1+r2), as in
# spikes/retrieval-ablation (gitignored data/, never committed). <reuse dir> optionally holds that spike's
# spans.npy (whole-span bge-m3 vectors, same image and encoder) so only longer spans' windows are encoded here.
#   1. m1.py questions: the 40 VEC-TUTOR-7 questions into the work dir;
#   2. reader-embed:local, --network none: embed_m1.py, vectors of every window and question (no text out);
#   3. m1.py measure: a throwaway pgvector container (testcontainers), the shipped seed/index/search/find/prompt
#      code, results.json (counts, bytes, flags, question ids).
set -euo pipefail
ART="$(cd "$1" && pwd)"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
WORK="$REPO/data/spikes/tutor-find-m1/work"
mkdir -p "$WORK"
reuse=(); reuse_arg=()
if [[ -n "${2:-}" ]]; then reuse=(-v "$(cd "$2" && pwd):/reuse:ro"); reuse_arg=(--reuse /reuse); fi

(cd "$REPO/server" && uv run python "$HERE/m1.py" questions --work "$WORK")

docker run --rm --network none -e HOME=/tmp \
  -v "$ART/spans.jsonl:/in/spans.jsonl:ro" -v "$WORK/questions.json:/in/questions.json:ro" \
  -v "$REPO/server/src/reader/search/chunking.py:/src/chunking.py:ro" -v "$HERE/embed_m1.py:/w/embed_m1.py:ro" \
  ${reuse[@]+"${reuse[@]}"} -v "$WORK:/out" --entrypoint python reader-embed:local /w/embed_m1.py \
  --spans /in/spans.jsonl --questions /in/questions.json --chunking /src/chunking.py \
  ${reuse_arg[@]+"${reuse_arg[@]}"} --out /out

(cd "$REPO/server" && uv run python "$HERE/m1.py" measure --art "$ART" --work "$WORK" --out "$HERE/results.json")
