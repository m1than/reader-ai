#!/usr/bin/env bash
# Local CI: the same jobs as .github/workflows/ci.yml, run on the owner's Mac.
# Owner, 2026-11-20: GitHub Actions do not start on the account (billing), so this script is the gate
# before a PR is opened or merged. Prints PASS/FAIL per step and exits 1 on any FAIL.
#   bash tools/ci-local.sh                 all jobs, guard-tests against origin/main
#   BASE=origin/some-branch bash tools/ci-local.sh
#   bash /path/to/tools/ci-local.sh        from any checkout or worktree: checks the one you are in
set -uo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || echo "$(dirname "$0")/..")"
BASE="${BASE:-origin/main}"
NODE26="/opt/homebrew/opt/node@26/bin"   # web/ pins Node 26 in web/.nvmrc (proposed ADR-0020)
FAILS=0

step() { # name, command...
  local name="$1"; shift
  local log; log="$(mktemp)"
  if "$@" >"$log" 2>&1; then printf '  \033[32mPASS\033[0m %s\n' "$name"
  else printf '  \033[31mFAIL\033[0m %s\n' "$name"; tail -n 15 "$log" | sed 's/^/       /'; FAILS=$((FAILS + 1)); fi
  rm -f "$log"
}

echo "trace (specd-lite)"
step "tooling dependencies" npm ci --prefix tools --silent
step "T0–T10 trace-check" node tools/trace/check.mjs
step "P7/P8 guard-tests vs $BASE" node tools/trace/guard-tests.mjs --base "$BASE"
step "write hook (H1–H4 in every worktree)" node --test tools/hooks/pre-write.test.mjs
step "OPS-DOD tools (T10, report, waivers, P8)" node --test "tools/dod/*.test.mjs"

if [[ -f server/pyproject.toml ]]; then
  echo "server"
  step "uv sync --frozen" bash -c "cd server && uv sync --frozen"
  step "ruff" bash -c "cd server && uv run ruff check ."
  [[ -d server/src ]] && step "mypy --strict" bash -c "cd server && uv run mypy src"
  [[ -d server/tests ]] && step "pytest" bash -c "cd server && uv run pytest"
fi

if [[ -f web/package.json ]]; then
  echo "web (node $(PATH="$NODE26:$PATH" node -v 2>/dev/null || echo missing))"
  step "npm ci" bash -c "cd web && PATH='$NODE26:$PATH' npm ci --no-audit --no-fund"
  step "typecheck" bash -c "cd web && PATH='$NODE26:$PATH' npm run typecheck"
  step "vitest" bash -c "cd web && PATH='$NODE26:$PATH' npm test"
  step "storybook build" bash -c "cd web && PATH='$NODE26:$PATH' npm run build-storybook"
fi

echo
if ((FAILS == 0)); then echo "LOCAL CI: PASS"; else echo "LOCAL CI: FAIL ($FAILS step(s))"; exit 1; fi
