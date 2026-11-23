#!/usr/bin/env bash
# The vertical slice live check (V.live): PDF → ingest → embed → search bench → compile L02 → lesson in the browser
# (lab cell, citations) → tutor. Precondition: `reader up --config PATH` and a healthy stack.
#
#   bash e2e/live/slice.sh --config PATH [--steps LIST] [--out DIR] [--no-ingest] [--reset-owner-passkeys]
#
#   --steps   default: routes,import,embed,bench,compile,lesson,tutor (see e2e/web/live/owner.slice.ts).
#             `routes` alone, or `bench`, spends nothing; `compile` spends up to the run cap ($8), `tutor` up to
#             3 × $0.30.
#   --out     where results and screenshots go (default e2e/live/out-slice, git-ignored). Keep it outside the
#             repository: screenshots show book text.
#   --no-ingest  skip `reader-ingest` (the material is already imported).
#
# Uses only the product's interfaces: `reader-ingest` (host converter, then the worker's receive) and
# `verify-all.sh --slice` (owner session with a CDP virtual passkey, Playwright, HTTP API). Writes
# OUT/ingest.json (wall time, pages per minute, peak RSS of the converter) and OUT/slice-results.json, numbers
# and ids only. Never prints a secret, a token, the environment or book text.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
USAGE="usage: $0 --config PATH [--steps LIST] [--out DIR] [--no-ingest] [--reset-owner-passkeys]"
CONFIG=""
STEPS="routes,import,embed,bench,compile,lesson,tutor"
OUT="$ROOT/e2e/live/out-slice"
INGEST=1
RESET=()
while (($#)); do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --steps) STEPS="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --no-ingest) INGEST=0; shift ;;
    --reset-owner-passkeys) RESET=(--reset-owner-passkeys); shift ;;
    *) echo "$USAGE" >&2; exit 2 ;;
  esac
done
[[ -n "$CONFIG" && -f "$CONFIG" ]] || { echo "$USAGE  (reader.yaml not found)" >&2; exit 2; }
command -v reader-ingest >/dev/null || { [[ -x "$ROOT/server/.venv/bin/reader-ingest" ]] && export PATH="$ROOT/server/.venv/bin:$PATH"; }
for tool in reader reader-ingest jq; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 2; }
done
mkdir -p "$OUT"
chmod 700 "$OUT"
MATERIAL=bashir-2025

if ((INGEST)) && [[ ",$STEPS," == *",import,"* ]]; then
  echo "ingest: reader-ingest $MATERIAL (host conversion, then receive in the worker)"
  t0=$(date +%s)
  rc=0
  # /usr/bin/time -l reports the largest resident set of the waited children (the confined converter).
  /usr/bin/time -l reader-ingest "$MATERIAL" --config "$CONFIG" </dev/null >"$OUT/ingest.log" 2>"$OUT/ingest.time" || rc=$?
  wall=$(( $(date +%s) - t0 ))
  peak=$(awk '/maximum resident set size/ {print $1}' "$OUT/ingest.time")
  jq -n --argjson rc "$rc" --argjson wall "$wall" --arg peak "${peak:-}" \
    '{exit: $rc, wall_s: $wall, peak_rss_bytes: (if $peak == "" then null else ($peak | tonumber) end)}' >"$OUT/ingest.json"
  grep -E '^reader-ingest: ' "$OUT/ingest.log" "$OUT/ingest.time" | sed 's/^[^:]*://' || true
  if ((rc != 0)); then echo "FAIL ingest: reader-ingest exited $rc"; exit 1; fi
  echo "PASS ingest: ${wall}s, peak RSS ${peak:-?} bytes"
fi

rc=0
bash "$ROOT/e2e/live/verify-all.sh" --config "$CONFIG" --slice "$STEPS" --out "$OUT" ${RESET[@]+"${RESET[@]}"} || rc=$?
if [[ -f "$OUT/ingest.json" && -f "$OUT/slice-results.json" ]]; then
  pages=$(jq '.import.page_count // empty' "$OUT/slice-results.json")
  wall=$(jq '.wall_s' "$OUT/ingest.json")
  [[ -n "$pages" && "$wall" -gt 0 ]] && echo "ingest: $(jq -n "$pages * 60 / $wall | . * 10 | round / 10") pages/min over $pages pages"
fi
exit "$rc"
