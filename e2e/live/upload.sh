#!/usr/bin/env bash
# Upload track live check (SRV-INGEST FR-INGEST-18..23, WEB-IMPORT, OPS-DEPLOY FR-DEPLOY-14, OPS-CLI FR-CLI-23)
# against `reader up` from this checkout, with the ingest watcher running (`reader up` starts it): the converter is
# online; the synthetic PDF uploaded in the browser reaches Ready (axe at 1280, 1024 and 390 px, no CSP violation); a
# cancelled upload leaves no row and no file; exactly 209,715,200 bytes pass and are fetched (time recorded); one
# byte more is 413. Spends nothing. Cleans up only what it created, by id.
#
#   bash e2e/live/upload.sh --out DIR [--project NAME] [--with-offline --config READER_YAML]
#
# --with-offline (opt-in, ADR-0029 verified_by): stops only the ingest watcher (SIGTERM to its lock's pid; never the
# stack), checks that a new upload stays `queued` and the converter reads `offline` after 60 s, then starts the
# watcher again exactly as `reader up` does (FR-CLI-23's spawn, also on failure) and waits for it to be online.
# --config is the reader.yaml `reader up` used (it locates the watcher's lock under data_dir).
#
# DIR must be outside the repository. OUT/upload-results.json holds numbers and ids only.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
source "$ROOT/e2e/live/lib.sh"
OUT=""
PROJECT="reader"
EXTRA=()
while (($#)); do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --project) PROJECT="$2"; shift 2 ;;
    --with-offline) EXTRA+=(--with-offline); shift ;;
    --config) EXTRA+=(--config "$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"); shift 2 ;;
    *) echo "usage: $0 --out DIR [--project NAME] [--with-offline --config READER_YAML]" >&2; exit 2 ;;
  esac
done
[[ -n "$OUT" ]] || { echo "--out is required" >&2; exit 2; }
mkdir -p "$OUT"
case "$(cd "$OUT" && pwd -P)/" in
  "$(cd "$ROOT" && pwd -P)/"*) echo "$OUT must be outside the repository" >&2; exit 2 ;;
esac

section "upload (SRV-INGEST revision 2, WEB-IMPORT)"
check "api answers /api/healthz" curl -fsSk "$READER_URL/api/healthz"
check "reader status reports the ingest watcher running" \
  bash -c "cd '$ROOT/server' && uv run reader status --json | grep -q '\"ingest_watcher\": \"running\"'"
rc=0
(cd "$ROOT" && uv run --project server python e2e/live/lib/upload_live.py --project "$PROJECT" --url "$READER_URL" \
  --out "$OUT" ${EXTRA[@]+"${EXTRA[@]}"}) >"$OUT/upload.txt" 2>&1 || rc=$?
while IFS= read -r l; do
  case "$l" in
    PASS\ *) printf '  \033[32mPASS\033[0m %s\n' "${l#PASS }"; LIVE_PASS=$((LIVE_PASS + 1)) ;;
    FAIL\ *) printf '  \033[31mFAIL\033[0m %s\n' "${l#FAIL }"; LIVE_FAIL=$((LIVE_FAIL + 1)); LIVE_FAILED+=("${l#FAIL }") ;;
  esac
done <"$OUT/upload.txt"
((rc == 0)) || [[ $LIVE_FAIL -gt 0 ]] || check "upload_live.py exited cleanly" false
summary
