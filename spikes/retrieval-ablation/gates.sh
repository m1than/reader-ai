# Sourced by paid.sh and v12.sh: the stage gates of the paid arms, read from results/paid/<stage>/summary.json.
# A stage has passed when its last run stopped on nothing, covered every item and (prefix stages) met the cache rule.
passed() {  # passed <results/paid dir> <stage>
  local f="$1/$2/summary.json"
  case "$2" in
    stage1|prefixes) jq -e '.stopped == null and .coverage_complete == true and .cache_rule_pass == true' "$f" ;;
    *) jq -e '.stopped == null and .coverage_complete == true' "$f" ;;
  esac >/dev/null 2>&1
}
require_prefixes_and_summaries() {  # V1/V2 need every indexed span prefixed and every outline node summarised
  local num="$1"
  passed "$num" stage1 && passed "$num" prefixes && passed "$num" summaries \
    && jq -e '.book_spans_with_prefix == .book_indexed_spans' "$num/prefixes/summary.json" >/dev/null 2>&1 \
    || { echo "refused: stage1, prefixes and summaries must all have passed with full coverage" >&2; exit 3; }
}
