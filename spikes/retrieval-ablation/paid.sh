#!/usr/bin/env bash
# Spike retrieval-ablation, paid arms (ADR-0039 B10). Approved by the owner 2026-11-26: at most $12.00 from
# budget.test_budget_usd, in stages, stop and re-ask if cache reads fall below 80% of projection or a stage would pass
# its projection + 20%.
#   bash spikes/retrieval-ablation/paid.sh <stage> [concurrency]
#   stage, in order:
#     stage1     context prefixes for chapters 6 and 13 (822 Haiku calls), cap $0.47 (projection $0.39 + 20%)
#     prefixes   the rest of the book (8,282 calls), cap $4.01 ($3.34 + 20%); refused unless stage1 passed
#     summaries  L2 summaries, 495 calls, cap $0.19 ($0.16 + 20%)
#     (then, free and local: bash spikes/retrieval-ablation/v12.sh — V1/V2 embedding, recall, topk.json)
#     judge      judge.eval sufficiency, Sonnet 5.5, deduped card sets of 6 variants (≈ 477 calls), cap $4.73
#     factcheck  the whole-chapter fact-check baseline, 30 Sonnet 5.5 calls, cap $2.42
# Caps sum to $11.82 ≤ $12.00; the runner also refuses to start when the recorded spend of all stages leaves less
# than this stage needs, and paid.py stops before any call that could pass either cap.
#
# Token: like spikes/tutor-call and spikes/compile-cost, this runner reads the Keychain item
# reader-ai/claude-oauth-token and pipes it to the container on stdin; it is never printed, written or passed as an
# argument or env var of `docker run`. THE OWNER RUNS THIS SCRIPT (the agent is not allowed to read the Keychain).
#
# Outputs: results/paid/<stage>/calls.jsonl and summary.json (ids, tokens, USD, wall time; committed) and the model
# texts (prefixes, summaries) in the gitignored data/spikes/retrieval-ablation/paid/text. The last line printed is
# the budget.ledger row to add to docs/progress.yaml.
set -euo pipefail
STAGE="${1:?stage: stage1 | prefixes | summaries | judge | factcheck}"
CONCURRENCY="${2:-4}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
ART="$REPO/data/spikes/retrieval-ablation/convert/out"     # the converter artifact of run.sh (RESULT.md Method)
TEXT="$REPO/data/spikes/retrieval-ablation/paid/text"
NUM="$HERE/results/paid"
IMAGE=reader-spike-compile          # the compile-cost image: python 3.14 + claude-agent-sdk, non-root
TOTAL_USD=12.00

case "$STAGE" in
  stage1) CAP=0.47; KILL_S=3600 ;;
  prefixes) CAP=4.01; KILL_S=28800 ;;
  summaries) CAP=0.19; KILL_S=3600 ;;
  judge) CAP=4.73; KILL_S=7200 ;;
  factcheck) CAP=2.42; KILL_S=3600 ;;
  *) echo "unknown stage $STAGE" >&2; exit 2 ;;
esac
[[ "$CONCURRENCY" =~ ^[1-8]$ ]] || { echo "concurrency must be 1-8" >&2; exit 2; }
for f in spans.jsonl outline.json; do
  [[ -f "$ART/$f" ]] || { echo "missing $ART/$f: run the converter step of RESULT.md first" >&2; exit 2; }
done

if [[ "$STAGE" == prefixes ]]; then
  jq -e '.cache_rule_pass == true and .stopped == null' "$NUM/stage1/summary.json" >/dev/null 2>&1 \
    || { echo "refused: stage1 has not passed (cache rule or cap); re-ask the owner" >&2; exit 3; }
fi
if [[ "$STAGE" == judge ]]; then
  jq -e 'has("V1_prefix") and has("V2_summaries")' "$HERE/results/topk.json" >/dev/null 2>&1 \
    || { echo "refused: results/topk.json lacks V1/V2; run spikes/retrieval-ablation/v12.sh first" >&2; exit 3; }
fi

shopt -s nullglob
calls=("$NUM"/*/calls.jsonl)
spent_all=0; spent_stage=0
((${#calls[@]})) && spent_all=$(cat "${calls[@]}" | jq -s 'map(.usd // 0) | add // 0')
[[ -f "$NUM/$STAGE/calls.jsonl" ]] && spent_stage=$(jq -s 'map(.usd // 0) | add // 0' "$NUM/$STAGE/calls.jsonl")
left_total=$(jq -n --argjson t "$TOTAL_USD" --argjson s "$spent_all" '$t - $s')
echo "stage=$STAGE cap=$CAP spent_stage=$spent_stage spent_all_stages=$spent_all left_of_12=$left_total"
jq -e -n --argjson l "$left_total" --argjson c "$CAP" --argjson s "$spent_stage" '$l >= ($c - $s)' >/dev/null \
  || { echo "refused: the stage cap does not fit in what is left of the owner's \$12.00" >&2; exit 3; }

mkdir -p "$NUM" "$TEXT"
# the evaluation set as JSON for the SDK image (no PyYAML there); kept with the other gitignored inputs
docker run --rm --network none -v "$REPO/spec/fixtures/eval/pilot-eval-v1.yaml:/in/eval.yaml:ro" \
  --entrypoint python reader-embed:local -c \
  'import json, yaml; print(json.dumps(yaml.safe_load(open("/in/eval.yaml", encoding="utf-8"))["questions"], ensure_ascii=False))' \
  > "$TEXT/../eval.json"
[[ -f "$HERE/results/topk.json" ]] || echo '{}' > "$HERE/results/topk.json"

docker build -q --platform linux/arm64 -t "$IMAGE" "$HERE/../compile-cost" >/dev/null
security find-generic-password -s reader-ai -a claude-oauth-token -w | docker run --rm -i --platform linux/arm64 \
  -e STAGE_CAP_USD="$CAP" -e TOTAL_LEFT_USD="$left_total" -e CONCURRENCY="$CONCURRENCY" \
  --read-only --tmpfs /home/worker:uid=1000,size=512m --tmpfs /tmp:uid=1000,size=512m \
  --cap-drop ALL --security-opt no-new-privileges --memory 4g --memory-swap 4g --pids-limit 512 \
  -v "$HERE/paid.py:/app/paid.py:ro" -v "$REPO/server/src/reader/search/chunking.py:/in/chunking.py:ro" \
  -v "$ART/spans.jsonl:/in/spans.jsonl:ro" -v "$ART/outline.json:/in/outline.json:ro" \
  -v "$TEXT/../eval.json:/in/eval.json:ro" -v "$HERE/results/topk.json:/work/topk.json:ro" \
  -v "$NUM:/out/numbers" -v "$TEXT:/out/text" \
  --entrypoint sh "$IMAGE" -c \
  "IFS= read -r CLAUDE_CODE_OAUTH_TOKEN && export CLAUDE_CODE_OAUTH_TOKEN && exec timeout -s KILL $KILL_S python /app/paid.py $STAGE"
