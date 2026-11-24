#!/usr/bin/env bash
# SRV-OBS live check (spec/modules/SRV-OBS.yaml, [live]) against `reader up` with the `observability` profile:
# the owner gate on https://localhost:8443, the blocked paths, the headers, the summary's trace links, and in the
# browser the trace link, the span tree, CSP and one UI write.
#
#   bash e2e/live/obs.sh --out DIR [--project NAME] [--no-browser] [--with-credit]
#
# Zero credit by default. --with-credit asks the tutor one question (spends Agent SDK credit) to check the job →
# agent.call → CLI nesting and the canary's absence; the live stack has no stub provider.
#
# Sessions: one owner row and one live-check learner row, created and deleted by their id hash only
# (e2e/live/lib/obs_live.py); the owner's passkeys and other sessions are never touched. This script runs no
# `docker exec` itself. DIR must be outside the repository (screenshots show lesson titles).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/e2e/live/lib.sh"
OUT=""
PROJECT="reader"
BROWSER=1
CREDIT=0
while (($#)); do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --project) PROJECT="$2"; shift 2 ;;
    --no-browser) BROWSER=0; shift ;;
    --with-credit) CREDIT=1; shift ;;
    *) echo "usage: $0 --out DIR [--project NAME] [--no-browser] [--with-credit]" >&2; exit 2 ;;
  esac
done
[[ -n "$OUT" ]] || { echo "--out is required" >&2; exit 2; }
mkdir -p "$OUT"
case "$(cd "$OUT" && pwd -P)/" in
  "$(cd "$ROOT" && pwd -P)/"*) echo "$OUT must be outside the repository" >&2; exit 2 ;;
esac

section "observability (SRV-OBS)"
check "api answers /api/healthz" curl -fsSk "$READER_URL/api/healthz"
args=(--project "$PROJECT" --out "$OUT")
((BROWSER)) && args+=(--browser "$ROOT/e2e/live/lib/obs_browser.mjs")
((CREDIT)) && args+=(--with-credit)
rc=0
(cd "$ROOT" && uv run --project server python e2e/live/lib/obs_live.py "${args[@]}") >"$OUT/obs.txt" 2>&1 || rc=$?
while IFS= read -r l; do
  case "$l" in
    PASS\ *) printf '  \033[32mPASS\033[0m %s\n' "${l#PASS }"; LIVE_PASS=$((LIVE_PASS + 1)) ;;
    FAIL\ *) printf '  \033[31mFAIL\033[0m %s\n' "${l#FAIL }"; LIVE_FAIL=$((LIVE_FAIL + 1)); LIVE_FAILED+=("${l#FAIL }") ;;
  esac
done <"$OUT/obs.txt"
((rc == 0)) || [[ $LIVE_FAIL -gt 0 ]] || check "obs_live.py exited cleanly" false
summary
