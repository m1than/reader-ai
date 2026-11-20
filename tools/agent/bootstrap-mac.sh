#!/usr/bin/env bash
# Owner's Mac readiness for the autonomous agent (ADR-0019). Read-only checks by default.
#   bash tools/agent/bootstrap-mac.sh            check everything, print PASS/FAIL, exit 1 on any FAIL
#   bash tools/agent/bootstrap-mac.sh --install  also install missing CLI tools with Homebrew
# Secrets are only tested for presence and file mode; their values are never printed.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
TOKEN_FILE="${READER_TG_TOKEN_FILE:-$HOME/Developer/.token_tg_bot}"
CHAT_FILE="$HOME/.config/reader-ai/telegram-chat-id"
INSTALL=$([[ "${1:-}" == "--install" ]] && echo 1 || echo 0)
FAILS=0

pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n       → %s\n' "$1" "$2"; FAILS=$((FAILS + 1)); }
check() { local name="$1" fix="$2"; shift 2; if "$@" >/dev/null 2>&1; then pass "$name"; else fail "$name" "$fix"; fi; }
need_tool() { # name, brew formula
  command -v "$1" >/dev/null 2>&1 && { pass "$1 installed"; return; }
  [[ $INSTALL == 1 ]] && command -v brew >/dev/null && brew install "$2" >/dev/null && { pass "$1 installed (brew)"; return; }
  fail "$1 installed" "brew install $2"
}
outside_git() { ! git -C "$(dirname "$1")" rev-parse --is-inside-work-tree; }
mode_600() { [[ "$(stat -f '%Lp' "$1")" == "600" ]]; }

echo "Machine"
check "macOS on Apple Silicon" "v1 targets the owner's Apple Silicon Mac (ADR-0012)" \
  bash -c '[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]]'
check "≥ 40 GB free disk" "free disk space: images, kernels and models need room" \
  bash -c '(( $(df -g "$HOME" | awk "NR==2{print \$4}") >= 40 ))'

echo "Tools"
check "Homebrew" "install from https://brew.sh" command -v brew
need_tool git git
need_tool gh gh
need_tool node node
need_tool uv uv
need_tool jq jq
check "node ≥ 22" "brew upgrade node" bash -c 'v=$(node -v | tr -d v); [[ "$(printf "%s\n22.0.0\n" "$v" | sort -V | head -1)" == 22.0.0 ]]'
check "Docker Desktop running" "start Docker Desktop" docker info
check "docker compose v2" "update Docker Desktop" docker compose version
check "Claude Code CLI" "npm install -g @anthropic-ai/claude-code" command -v claude
check "GitHub CLI signed in" "gh auth login (scopes: repo, workflow)" gh auth status
check "access to m1than/reader-ai" "gh auth refresh -s repo" gh repo view m1than/reader-ai
check "access to m1than/ui-builder-lib-with-agents" "gh auth refresh -s repo" gh repo view m1than/ui-builder-lib-with-agents

echo "Secrets (presence and file mode only)"
check "CLAUDE_CODE_OAUTH_TOKEN is set in this shell" "export it in ~/.zshrc (claude setup-token)" \
  bash -c '[[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]'
check "Telegram token file exists" "put the bot token into $TOKEN_FILE" test -s "$TOKEN_FILE"
check "Telegram token file mode is 600" "chmod 600 $TOKEN_FILE" mode_600 "$TOKEN_FILE"
check "Telegram token file is outside any git work tree" "move it out of the repository" outside_git "$TOKEN_FILE"
check "Telegram chat linked" "send /start to your bot in Telegram, then: node tools/agent/notify.mjs link" test -s "$CHAT_FILE"

echo "Claude Code settings"
check "auto mode rules in ~/.claude/settings.json" "merge tools/agent/user-settings.json into ~/.claude/settings.json" \
  bash -c 'jq -e ".autoMode" "$HOME/.claude/settings.json"'
check "auto mode rules load" "run: claude auto-mode config" claude auto-mode config

echo "Repository"
check "on an up-to-date main" "git checkout main && git pull" \
  bash -c "cd '$REPO' && git fetch -q origin main && [[ \$(git rev-parse HEAD) == \$(git rev-parse origin/main) ]]"
check "tooling dependencies" "npm ci --prefix tools" bash -c "cd '$REPO' && npm ci --prefix tools --silent"
check "trace-check green" "node tools/trace/check.mjs" bash -c "cd '$REPO' && node tools/trace/check.mjs"
check "ADR-0001…0019 accepted by the owner" "set status: accepted in docs/adr/0001…0019 and merge (owner gate)" \
  bash -c "cd '$REPO' && ! grep -l '^status: proposed' \$(ls docs/adr/00*.md | grep -E '/00(0[1-9]|1[0-9])-')"

echo
if (( FAILS == 0 )); then
  echo "Ready. Start the agent from $REPO:"
  echo '  caffeinate -dimsu claude --permission-mode auto "$(cat tools/agent/kickoff.md)"'
else
  echo "$FAILS check(s) failed. Fix them and run again."
  exit 1
fi
