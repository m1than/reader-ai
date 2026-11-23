#!/usr/bin/env bash
# The vertical slice live check (V.live): PDF → ingest → embed → search bench → compile L02 → lesson in the browser
# (lab cell, citations) → tutor. Precondition: `reader up --config PATH` and a healthy stack.
#
#   bash e2e/live/slice.sh --config PATH --out DIR [--steps LIST] [--no-ingest] [--reset-owner-passkeys]
#                          [--tutor-bench-dir DIR] [--tutor-bench-from N]
#
#   --steps   default: routes,import,embed,bench,compile,lesson,tutor,ui (see e2e/web/live/owner.slice.ts).
#             `routes` alone, `bench` or `ui` spends nothing; `compile` spends up to the run cap ($8), `tutor` up to
#             3 × $0.30; `tutor-bench` (not in the default) asks SRV-TUTOR's 40 bench questions without a cell:
#             about 40 × $0.046 ≈ $1.8, at most 40 × $0.30, stopping at the $3 daily cap.
#   --tutor-bench-dir  where tutor-bench keeps one record file per run (default: OUT), resolved to an absolute path
#             and refused inside the repository (record files are numbers only, but stay with the run's outputs);
#             point day 2 at day 1's directory to resume: only questions not yet recorded there are asked, and the
#             summary merges all runs.
#   --tutor-bench-from N  also skip the first N bench questions (warned about without --tutor-bench-dir: a new
#             directory has no earlier records to merge with).
#   --out     required, outside the repository: screenshots and Playwright's failure snapshots show book text.
#             Only OUT/slice-results.json and OUT/ingest.json (numbers and ids) are meant for e2e/live/results/.
#   --no-ingest  skip `reader-ingest` (the material is already imported).
#   --reset-owner-passkeys  only when a previous run died before removing its owner passkey: deletes every passkey
#             and session of the owner, including a real one made in Safari.
#
# Uses only the product's interfaces: `reader-ingest` (host converter, then the worker's receive) and
# `verify-all.sh --slice` (owner session with a CDP virtual passkey, Playwright, HTTP API). Writes
# OUT/ingest.json (wall time, peak RSS of the converter, whether it converted) and OUT/slice-results.json, numbers
# and ids only. Never prints a secret, a token, the environment or book text.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
USAGE="usage: $0 --config PATH --out DIR [--steps LIST] [--no-ingest] [--reset-owner-passkeys] [--tutor-bench-dir DIR] [--tutor-bench-from N]"
CONFIG=""
STEPS="routes,import,embed,bench,compile,lesson,tutor,ui"
OUT=""
INGEST=1
RESET=()
BENCH_DIR=""
while (($#)); do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --steps) STEPS="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --no-ingest) INGEST=0; shift ;;
    --reset-owner-passkeys) RESET=(--reset-owner-passkeys); shift ;;
    # SRV-TUTOR's bench resume: read by web_live.py's slice_inputs, which passes them to Playwright.
    --tutor-bench-dir) BENCH_DIR="$2"; shift 2 ;;
    --tutor-bench-from) [[ "$2" =~ ^[0-9]+$ ]] || { echo "$USAGE" >&2; exit 2; }; export READER_TUTOR_BENCH_FROM="$2"; shift 2 ;;
    *) echo "$USAGE" >&2; exit 2 ;;
  esac
done
[[ -n "$CONFIG" && -f "$CONFIG" ]] || { echo "$USAGE  (reader.yaml not found)" >&2; exit 2; }
[[ -n "$OUT" ]] || { echo "$USAGE  (--out is required)" >&2; exit 2; }
command -v reader-ingest >/dev/null || { [[ -x "$ROOT/server/.venv/bin/reader-ingest" ]] && export PATH="$ROOT/server/.venv/bin:$PATH"; }
for tool in reader reader-ingest jq; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 2; }
done
mkdir -p "$OUT"
chmod 700 "$OUT"
case "$(cd "$OUT" && pwd -P)/" in
  "$(cd "$ROOT" && pwd -P)/"*) echo "--out must be outside the repository (screenshots show book text)" >&2; exit 2 ;;
esac
if [[ -n "$BENCH_DIR" ]]; then
  mkdir -p "$BENCH_DIR"
  BENCH_DIR="$(cd "$BENCH_DIR" && pwd -P)"   # absolute: Playwright runs in another working directory
  case "$BENCH_DIR/" in
    "$(cd "$ROOT" && pwd -P)/"*) echo "--tutor-bench-dir must be outside the repository" >&2; exit 2 ;;
  esac
  export READER_TUTOR_BENCH_DIR="$BENCH_DIR"
elif [[ -n "${READER_TUTOR_BENCH_FROM:-}" ]]; then
  echo "warning: --tutor-bench-from without --tutor-bench-dir: OUT has no earlier bench records to resume or merge" >&2
fi
rm -f "$OUT/ingest.json"   # never pair a previous run's ingest numbers with this run
MATERIAL=bashir-2025

if ((INGEST)) && [[ ",$STEPS," == *",import,"* ]]; then
  echo "ingest: reader-ingest $MATERIAL (host conversion, then receive in the worker)"
  t0=$(date +%s)
  rc=0
  # /usr/bin/time -l reports the largest resident set of the waited children (the confined converter).
  /usr/bin/time -l reader-ingest "$MATERIAL" --config "$CONFIG" </dev/null >"$OUT/ingest.log" 2>"$OUT/ingest.time" || rc=$?
  wall=$(( $(date +%s) - t0 ))
  peak=$(awk '/maximum resident set size/ {print $1}' "$OUT/ingest.time")
  # Exit 0 also means "already ingested" or "import in progress" (no conversion): no throughput then.
  converted=true
  if grep -qE '^(already ingested|import in progress)' "$OUT/ingest.log"; then converted=false; fi
  jq -n --argjson rc "$rc" --argjson wall "$wall" --arg peak "${peak:-}" --argjson converted "$converted" \
    '{exit: $rc, converted: $converted, wall_s: $wall,
      peak_rss_bytes: (if $peak == "" then null else ($peak | tonumber) end)}' >"$OUT/ingest.json"
  grep -hE '^reader-ingest: ' "$OUT/ingest.log" "$OUT/ingest.time" || true
  if ((rc != 0)); then echo "FAIL ingest: reader-ingest exited $rc"; exit 1; fi
  echo "PASS ingest: ${wall}s, converted $converted, peak RSS ${peak:-?} bytes"
fi

rc=0
bash "$ROOT/e2e/live/verify-all.sh" --config "$CONFIG" --slice "$STEPS" --out "$OUT" ${RESET[@]+"${RESET[@]}"} || rc=$?
if [[ -f "$OUT/ingest.json" && -f "$OUT/slice-results.json" ]] && jq -e '.converted' "$OUT/ingest.json" >/dev/null; then
  pages=$(jq '.import.page_count // empty' "$OUT/slice-results.json")
  wall=$(jq '.wall_s' "$OUT/ingest.json")
  if [[ -n "$pages" && "$wall" -gt 0 ]]; then
    echo "ingest: $(jq -n "$pages * 60 / $wall | . * 10 | round / 10") pages/min over $pages pages (host wall time incl. receive)"
  fi
fi
exit "$rc"
