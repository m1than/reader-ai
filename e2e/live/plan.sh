#!/usr/bin/env bash
# SRV-PLAN / WEB-PLAN live check (V.onboarding, planning track) against `reader up` with the pilot already imported:
# preview, confirm with compile_first false (no credit), replay, re-plan, back to 12 weeks; then screenshots and axe of
# /import at 1280, 1024 and 390 px.
#
#   bash e2e/live/plan.sh --out DIR [--shots DIR] [--project NAME]
#
# The owner session is one row this script creates and deletes by its id (e2e/live/lib/plan_live.py); it creates no
# passkey and never removes the owner's own sessions or passkeys. DIR and the shots directory must be outside the
# repository (the screenshots show lesson titles). OUT/plan-results.json holds numbers and ids only.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/e2e/live/lib.sh"
OUT=""
SHOTS=""
PROJECT="reader"
while (($#)); do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --shots) SHOTS="$2"; shift 2 ;;
    --project) PROJECT="$2"; shift 2 ;;
    *) echo "usage: $0 --out DIR [--shots DIR] [--project NAME]" >&2; exit 2 ;;
  esac
done
[[ -n "$OUT" ]] || { echo "--out is required" >&2; exit 2; }
mkdir -p "$OUT"
for d in "$OUT" ${SHOTS:+"$SHOTS"}; do
  mkdir -p "$d"
  case "$(cd "$d" && pwd -P)/" in
    "$(cd "$ROOT" && pwd -P)/"*) echo "$d must be outside the repository" >&2; exit 2 ;;
  esac
done

section "plan (SRV-PLAN, WEB-PLAN)"
check "api answers /api/healthz" curl -fsSk "$READER_URL/api/healthz"
rc=0
(cd "$ROOT" && uv run --project server python e2e/live/lib/plan_live.py --project "$PROJECT" --url "$READER_URL" \
  --out "$OUT" ${SHOTS:+--shots "$SHOTS"}) >"$OUT/plan.txt" 2>&1 || rc=$?
while IFS= read -r l; do
  case "$l" in
    PASS\ *) printf '  \033[32mPASS\033[0m %s\n' "${l#PASS }"; LIVE_PASS=$((LIVE_PASS + 1)) ;;
    FAIL\ *) printf '  \033[31mFAIL\033[0m %s\n' "${l#FAIL }"; LIVE_FAIL=$((LIVE_FAIL + 1)); LIVE_FAILED+=("${l#FAIL }") ;;
  esac
done <"$OUT/plan.txt"
((rc == 0)) || [[ $LIVE_FAIL -gt 0 ]] || check "plan_live.py exited cleanly" false
summary
