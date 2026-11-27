#!/usr/bin/env bash
# No-spend dry run of paid.py: every stage in the real image and SDK, --network none, no token, against the stub CLI
# (stub/claude), into a scratch directory. Asserts per stage: exit 0, full coverage, calls ≤ cap / price +
# concurrency at a low cap (and the stop message), the cache rule failing without cache hits, and no argv over the
# Linux limit (the stub refuses one).
#   bash spikes/retrieval-ablation/dry_run.sh <scratch dir>
set -euo pipefail
S="$(mkdir -p "$1" && cd "$1" && pwd)"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
ART="$REPO/data/spikes/retrieval-ablation/convert/out"
IMAGE=reader-spike-compile
# the dry run never builds (building needs the network to install the SDK): paid.sh builds the image
docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "refused: image $IMAGE is missing; build it first (docker build --platform linux/arm64 -t $IMAGE spikes/compile-cost)" >&2
  exit 2; }

docker run --rm --network none -v "$REPO/spec/fixtures/eval/pilot-eval-v1.yaml:/in/eval.yaml:ro" \
  -v "$REPO/server/src/reader/agent/pricing.yaml:/in/pricing.yaml:ro" --entrypoint python reader-embed:local -c '
import json, yaml
json.dump(yaml.safe_load(open("/in/eval.yaml", encoding="utf-8"))["questions"], open("/dev/stdout", "w"), ensure_ascii=False)
' > "$S/eval.json"
docker run --rm --network none -v "$REPO/server/src/reader/agent/pricing.yaml:/in/pricing.yaml:ro" \
  --entrypoint python reader-embed:local -c 'import json, yaml; print(json.dumps(yaml.safe_load(open("/in/pricing.yaml"))["models"]))' \
  > "$S/prices.json"
# the judge needs V1/V2 card sets: in the dry run they reuse V0's, V2 with one summary entry
jq '. + {V1_prefix: .V0_spans, V2_summaries: (.V0_spans | map_values(.[0:9] + ["n5"]))}' "$HERE/results/topk.json" > "$S/topk.json"

run() {  # run <case> <stage> <cap> [env...]
  local case="$1" stage="$2" cap="$3"; shift 3
  mkdir -p "$S/$case/num" "$S/$case/text"
  # full runs chain like the real stages: each starts from the texts the earlier stages wrote
  if [[ "$case" =~ ^(full-|cap-judge|cap-factcheck) && -d "$S/text-seed" ]]; then cp "$S"/text-seed/* "$S/$case/text/" 2>/dev/null || true; fi
  docker run --rm --network none --platform linux/arm64 --read-only \
    --tmpfs /home/worker:uid=1000,size=512m --tmpfs /tmp:uid=1000,size=512m --cap-drop ALL \
    --security-opt no-new-privileges -e STAGE_CAP_USD="$cap" -e TOTAL_LEFT_USD=12.00 -e CONCURRENCY=4 \
    -e CLI_PATH=/stub/claude -e CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK=1 "$@" \
    -v "$HERE/paid.py:/app/paid.py:ro" -v "$HERE/stub:/stub:ro" -v "$REPO/server/src/reader/search/chunking.py:/in/chunking.py:ro" \
    -v "$ART/spans.jsonl:/in/spans.jsonl:ro" -v "$ART/outline.json:/in/outline.json:ro" \
    -v "$S/eval.json:/in/eval.json:ro" -v "$S/prices.json:/in/prices.json:ro" -v "$S/topk.json:/work/topk.json:ro" \
    -v "$S/$case/num:/out/numbers" -v "$S/$case/text:/out/text" \
    --entrypoint python "$IMAGE" /app/paid.py "$stage" > "$S/$case/out.log" 2>&1 && echo 0 || echo $?
}
fail=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fail=1; fi; }
sum() { jq -r "$1" "$S/$2/num/$3/summary.json"; }

# full stages at the real caps, price 0.0004 per call
for stage in stage1 prefixes summaries; do
  rc=$(run "full-$stage" "$stage" 12.00 -e STUB_USD=0.0004)
  check "$stage exit 0" '[[ $rc == 0 ]]'
  check "$stage coverage" '[[ $(sum .coverage_complete full-$stage $stage) == true ]]'
  mkdir -p "$S/text-seed"; cp "$S/full-$stage/text/"*.jsonl "$S/text-seed/" 2>/dev/null || true
done
check "stage1 cache rule passes with cache hits" '[[ $(sum .cache_rule_pass full-stage1 stage1) == true ]]'
check "stage1 calls 822" '[[ $(sum .calls full-stage1 stage1) == 822 ]]'
check "summaries calls 495" '[[ $(sum .calls full-summaries summaries) == 495 ]]'
for stage in judge factcheck; do
  rc=$(run "full-$stage" "$stage" 12.00 -e STUB_USD=0.0004)
  check "$stage exit 0" '[[ $rc == 0 ]]'
  check "$stage coverage" '[[ $(sum .coverage_complete full-$stage $stage) == true ]]'
done
check "judge calls 477" '[[ $(sum .calls full-judge judge) == 477 ]]'
check "factcheck calls 30" '[[ $(sum .calls full-factcheck factcheck) == 30 ]]'
check "no committed row holds free text" \
  '! cat "$S"/full-*/num/*/calls.jsonl | jq -r "to_entries[] | select(.value|type==\"string\") | .key" | sort -u | grep -vxE "model|error|subtype|id|kind|qid|type|claim" >/dev/null'

# rerun of a passed stage1: no calls, still passes
rm -rf "$S/rerun"; mkdir -p "$S/rerun"; cp -R "$S/full-stage1/num" "$S/full-stage1/text" "$S/rerun/"
rc=$(run rerun stage1 12.00 -e STUB_USD=0.0004)
check "stage1 rerun keeps the pass" '[[ $rc == 0 && $(sum .calls_this_run rerun stage1) == 0 && $(sum .cache_rule_pass rerun stage1) == true ]]'

# caps: each stage at a low cap with price p must stop, and calls ≤ cap / p + concurrency
for stage in stage1 prefixes summaries judge factcheck; do
  case $stage in judge|factcheck) p=0.1 cap=1.00 ;; *) p=0.01 cap=0.10 ;; esac
  rc=$(run "cap-$stage" "$stage" "$cap" -e STUB_USD=$p)
  n=$(sum .calls cap-$stage $stage)
  check "$stage stops at its cap ($n calls)" \
    '[[ $rc == 3 && $(sum .stopped cap-$stage $stage) == stage\ cap* ]] && jq -n --argjson n $n --argjson c $cap --argjson p $p "\$n <= (\$c / \$p) + 4" | grep -q true'
done

# the cache rule: no cache hits stops stage1 after the first call + 3 misses; calls already in flight finish
# (at most concurrency - 1 more): 4 to 7 calls at concurrency 4
rc=$(run nocache stage1 12.00 -e STUB_USD=0.0004 -e STUB_NOCACHE=1)
n=$(sum .calls nocache stage1)
check "no cache: stage1 stops (exit 3, $n calls)" '[[ $rc == 3 && $n -ge 4 && $n -le 7 && $(sum .stopped nocache stage1) == cache\ rule* ]]'
# chapter-first calls the CLI continued to k requests (usage summed): cold k = 2, 3, 4 and warm k = 3; no false
# cache-rule stop, context estimated from one request
for k in 2 3 4; do
  rc=$(run "continued-$k" stage1 12.00 -e STUB_USD=0.0004 -e STUB_CONTINUE_FIRST=$k)
  check "cold first call continued to $k requests: stage1 passes" '[[ $rc == 0 && $(sum .cache_rule_pass continued-$k stage1) == true ]]'
done
rc=$(run warm-3 stage1 12.00 -e STUB_USD=0.0004 -e STUB_CONTINUE_FIRST=3 -e STUB_PREWARM=1)
check "warm first call continued to 3 requests: stage1 passes" '[[ $rc == 0 && $(sum .cache_rule_pass warm-3 stage1) == true ]]'

# failures run last and cannot block the others: the spans whose calls failed in the owner's prefixes run 1 fail
# every time here (the stub gets their sha256 prefixes, never their text); in one run each is retried once after
# every other span, its second failure records `prefix: none`, and the stage completes
FAIL_SHA=$(docker run --rm --network none -v "$ART/spans.jsonl:/in/spans.jsonl:ro" \
  -v "$HERE/results/paid/prefixes/calls.jsonl:/in/calls.jsonl:ro" --entrypoint python reader-embed:local -c '
import hashlib, json
failed = {r["id"] for r in map(json.loads, open("/in/calls.jsonl")) if r.get("error")}
spans = {s["seq"]: s["text"] for s in map(json.loads, open("/in/spans.jsonl", encoding="utf-8"))}
print(",".join(sorted({hashlib.sha256(spans[i].encode()).hexdigest()[:16] for i in failed})))')
rc=$(run retry prefixes 12.00 -e STUB_USD=0.0004 -e STUB_FAIL_SHA="$FAIL_SHA")
check "retry: prefixes completes in one run despite spans that always fail" \
  '[[ $rc == 0 && $(sum .coverage_complete retry prefixes) == true && $(sum .book_spans_prefix_none retry prefixes) -ge 50 ]]'
check "retry: in the final run every last try comes after every first try" \
  'jq -s "(to_entries | map(select(.value.retry == true)) | map(.key) | min) as \$a | (to_entries | map(select(.value.retry == false)) | map(.key) | max) as \$b | (\$a == null or \$b == null or \$a > \$b)" <(tail -n $(sum .calls_this_run retry prefixes) "$S/retry/num/prefixes/calls.jsonl") | grep -q true'
# empty answers: failed calls; all empty is systemic, so the stage stops (exit 3) with coverage incomplete
rc=$(run empty summaries 12.00 -e STUB_USD=0.0004 -e STUB_EMPTY=1)
check "empty answers fail coverage" '[[ $rc == 3 && $(sum .coverage_complete empty summaries) == false ]]'
# summaries run 1's failure shape: answers longer than the output cap are continued to 4 requests and fail.
# 210-token answers (run 1's 70 words) failed under the old 200 cap; under the leaf cap of 400 they finish
rc=$(run sum-210 summaries 12.00 -e STUB_USD=0.0004 -e STUB_ANSWER_TOKENS=210)
check "summaries: 210-token answers finish under the new caps" \
  '[[ $rc == 0 && $(sum .coverage_complete sum-210 summaries) == true && $(sum .failed_calls sum-210 summaries) == 0 ]]'
# answers over every leaf cap (450 > 400): every leaf fails, so the stage stops after 10 in a row (exit 3)
rc=$(run sum-450 summaries 12.00 -e STUB_USD=0.0004 -e STUB_ANSWER_TOKENS=450)
check "summaries: answers over the cap stop the stage" '[[ $rc == 3 && $(sum .stopped sum-450 summaries) == 10\ consecutive* ]]'
# files left in HOME: 100 kB per call extrapolates past the 512 MB tmpfs over 9,104 calls, so stage1 fails
rc=$(run home stage1 12.00 -e STUB_USD=0.0004 -e STUB_HOME_BYTES=100000)
check "home tmpfs: stage1 fails when HOME grows per call" '[[ $rc == 3 && $(sum .home_tmpfs_ok home stage1) == false ]]'
check "home tmpfs: the stub run left HOME empty" '[[ $(sum .home_tmpfs_ok full-stage1 stage1) == true ]]'

# gates (gates.sh, used by paid.sh and v12.sh)
source "$HERE/gates.sh"
check "gate: stage1 passed" 'passed "$S/full-stage1/num" stage1'
check "gate: no-cache stage1 refused" '! passed "$S/nocache/num" stage1'
check "gate: stage1 with a growing HOME refused" '! passed "$S/home/num" stage1'
check "gate: capped stage refused" '! passed "$S/cap-summaries/num" summaries'
mkdir -p "$S/gate/num"
cp -R "$S/full-stage1/num/stage1" "$S/full-prefixes/num/prefixes" "$S/full-summaries/num/summaries" "$S/gate/num/"
check "gate: judge allowed after full prefixes and summaries" '(require_prefixes_and_summaries "$S/gate/num")'
cp -R "$S/empty/num/summaries" "$S/gate/num/"
check "gate: judge refused with incomplete summaries" '! (require_prefixes_and_summaries "$S/gate/num" 2>/dev/null)'
exit $fail
