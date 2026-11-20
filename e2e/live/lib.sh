# Helpers for the live checks in e2e/live/stage-N.sh (docs/roadmap.md, "How every stage runs").
# Source it, call `check` per assertion, end with `summary`:
#
#   source "$(dirname "$0")/lib.sh"
#   check "api answers /health" curl -fsS "$READER_URL/health"
#   summary
#
# `check` runs the command, prints PASS or FAIL with the name, and keeps going; on FAIL it prints
# the last lines of the command's output. `summary` prints the totals and exits 1 if any check failed.
# Output never includes environment variables; checks must not echo secrets.

READER_URL="${READER_URL:-https://localhost}"
LIVE_PASS=0
LIVE_FAIL=0
LIVE_FAILED=()

check() {
  local name="$1"
  shift
  local out
  if out="$("$@" 2>&1)"; then
    printf '  \033[32mPASS\033[0m %s\n' "$name"
    LIVE_PASS=$((LIVE_PASS + 1))
  else
    printf '  \033[31mFAIL\033[0m %s\n' "$name"
    printf '%s\n' "$out" | tail -n 5 | sed 's/^/       /'
    LIVE_FAIL=$((LIVE_FAIL + 1))
    LIVE_FAILED+=("$name")
  fi
}

section() {
  printf '%s\n' "$1"
}

summary() {
  echo
  if ((LIVE_FAIL == 0)); then
    printf 'PASS: %d of %d checks\n' "$LIVE_PASS" "$LIVE_PASS"
    return 0
  fi
  printf 'FAIL: %d of %d checks failed\n' "$LIVE_FAIL" "$((LIVE_PASS + LIVE_FAIL))"
  printf '  - %s\n' "${LIVE_FAILED[@]}"
  exit 1
}
