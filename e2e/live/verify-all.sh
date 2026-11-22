#!/usr/bin/env bash
# One command for every automated live check of the slice against the running stack.
#
#   reader up --config ~/path/to/reader.yaml          # first; the reader.yaml declares the learner `live-check`
#   bash e2e/live/verify-all.sh --config ~/path/to/reader.yaml
#
# Runs e2e/live/stage-1.sh (stack, edge, ADR-0026 secrets, software passkey, watcher) and the Playwright browser suite
# in e2e/web/live/ (CDP virtual passkey, lesson rendering, notebook cells, sandbox probes, CSP, axe). Writes
# e2e/live/out/REPORT.md (one PASS/FAIL line per check, timings, artifact paths) and exits 1 on any FAIL.
# Starts nothing; changes only the test learner `live-check` (reset, fixture lesson) and restarts api and postgres
# once (stage-1 watcher checks). Never prints a secret, a token or the environment.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/e2e/live/out"
SUITE="$ROOT/e2e/web/live"
READER_URL="${READER_URL:-https://localhost}"

CONFIG=""
while (($#)); do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    *) echo "usage: $0 --config PATH" >&2; exit 2 ;;
  esac
done
[[ -n "$CONFIG" && -f "$CONFIG" ]] || { echo "usage: $0 --config PATH  (reader.yaml not found)" >&2; exit 2; }
# The project's own `reader` (server/.venv, created by `uv sync`) when none is installed globally; stage-1.sh inherits it.
command -v reader >/dev/null || { [[ -x "$ROOT/server/.venv/bin/reader" ]] && export PATH="$ROOT/server/.venv/bin:$PATH"; }
for tool in reader docker jq curl uv; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 2; }
done
NODE26=/opt/homebrew/opt/node@26/bin
[[ -d "$NODE26" ]] && export PATH="$NODE26:$PATH"
node -e 'process.exit(Number(process.versions.node.split(".")[0]) === 26 ? 0 : 1)' \
  || { echo "Node 26 is required (brew install node@26)" >&2; exit 2; }

# --- Preflight: the stack is up and healthy; nothing is started here -------------------------------------------------
status_json="$(reader status --config "$CONFIG" --json 2>/dev/null || true)"
if ! jq -e '.watcher == "running" and ([.services[] | select(.state == "running" and .health == "healthy") | .name]
            | (index("api") != null and index("caddy") != null and index("postgres") != null and index("worker") != null))' \
     <<<"$status_json" >/dev/null 2>&1; then
  echo "The stack is not up and healthy. Start it first: reader up --config $CONFIG" >&2
  exit 2
fi
PROJECT="$(jq -r '.project // "reader"' <<<"$status_json")"

rm -rf "$OUT"
mkdir -p "$OUT"
chmod 700 "$OUT"
[[ -d "$SUITE/node_modules" ]] || (cd "$SUITE" && npm ci --no-audit --no-fund --silent)

strip() { sed -E $'s/\x1b\\[[0-9;]*m//g'; }
RESULTS="$OUT/results.txt"
: >"$RESULTS"

# --- Stage 1 ----------------------------------------------------------------------------------------------------------
t0=$SECONDS
bash "$ROOT/e2e/live/stage-1.sh" --config "$CONFIG" 2>&1 | strip >"$OUT/stage-1.log" || true
stage1_s=$((SECONDS - t0))
# POSIX classes, not \s: macOS grep and sed do not know \s.
grep -E '^[[:space:]]+(PASS|FAIL) ' "$OUT/stage-1.log" | sed -E 's/^[[:space:]]+//; s/^(PASS|FAIL) /\1 stage-1: /' >>"$RESULTS" || true
grep -q '^STAGE 1: PASS' "$OUT/stage-1.log" || grep -q '^STAGE 1:' "$OUT/stage-1.log" \
  || echo "FAIL stage-1: script did not finish (see out/stage-1.log)" >>"$RESULTS"

# --- Browser suite ----------------------------------------------------------------------------------------------------
t0=$SECONDS
(cd "$ROOT/server" && uv run --quiet python -I "$ROOT/e2e/live/lib/web_live.py" \
  --project "$PROJECT" --url "$READER_URL" --out "$OUT" --suite "$SUITE") >"$OUT/browser.txt" 2>&1 || true
browser_s=$((SECONDS - t0))
grep -E '^(PASS|FAIL|SKIP) ' "$OUT/browser.txt" >>"$RESULTS" || true
[[ -s "$OUT/browser.txt" ]] || echo "FAIL browser: helper produced no output" >>"$RESULTS"

# --- Report -----------------------------------------------------------------------------------------------------------
pass=$(grep -c '^PASS ' "$RESULTS" || true)
fail=$(grep -c '^FAIL ' "$RESULTS" || true)
skip=$(grep -c '^SKIP ' "$RESULTS" || true)
verdict=PASS; ((fail == 0)) || verdict=FAIL
{
  echo "# Live verification — $verdict"
  echo
  echo "- When: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "- Commit: $(git -C "$ROOT" rev-parse --short HEAD) ($(git -C "$ROOT" rev-parse --abbrev-ref HEAD))"
  echo "- Stack: project \`$PROJECT\` at $READER_URL"
  echo "- Totals: $pass passed, $fail failed, $skip skipped"
  echo "- Timings: stage-1 ${stage1_s}s, browser ${browser_s}s"
  echo
  echo "| Result | Check |"
  echo "| --- | --- |"
  sed -E 's/^(PASS|FAIL|SKIP) (.*)$/| \1 | \2 |/' "$RESULTS"
  echo
  echo "## Artifacts (in e2e/live/out/, not committed)"
  echo
  echo "- stage-1.log, browser.txt, playwright.log, playwright.json"
  shots=("$OUT"/screenshots/*.png)
  [[ -e "${shots[0]}" ]] && echo "- screenshots/: ${shots[*]##*/}"
  echo "- trace.zip (cookie values redacted; open with: npx playwright show-trace e2e/live/out/trace.zip)"
  echo "- video/ (whole browser session), a11y.txt (axe: serious and critical issues, report-only)"
  echo
  echo "## Not automated"
  echo
  echo "- The owner's own passkey in Safari with Apple Passwords: reader bootstrap --config <reader.yaml>, then"
  echo "  https://localhost/api/me shows \"role\":\"owner\". Safari offers no virtual authenticator."
} >"$OUT/REPORT.md"

cat "$OUT/REPORT.md"
[[ "$verdict" == PASS ]]
