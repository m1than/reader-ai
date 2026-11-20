#!/usr/bin/env bash
# One command for the owner: prepare the Mac and start the autonomous agent (ADR-0019).
#   cd ~/Developer/reader-ai && bash tools/agent/start.sh
# Every step is idempotent: run it again after any interruption.
# Owner gates (accepting ADRs) are asked interactively; nothing is accepted without "y".
set -euo pipefail

cd "$(dirname "$0")/../.."
say() { printf '\n\033[1m▶ %s\033[0m\n' "$1"; }

say "1/7 Утилиты (Homebrew)"
command -v brew >/dev/null || { echo "  Нужен Homebrew: https://brew.sh"; exit 1; }
for tool in git gh node uv jq; do command -v "$tool" >/dev/null || brew install "$tool"; done
gh auth status >/dev/null 2>&1 || gh auth login --scopes repo,workflow

say "2/7 Свежий main"
git checkout -q main
git pull -q --ff-only

say "3/7 Зависимости тулинга"
npm ci --prefix tools --silent

say "4/7 Статусы ADR-0001…0019"
# macOS ships bash 3.2 (no mapfile); ADR paths contain no spaces.
# shellcheck disable=SC2207
proposed=($(grep -l '^status: proposed$' docs/adr/00*.md | grep -E '/00(0[1-9]|1[0-9])-' || true))
if (( ${#proposed[@]} > 0 )); then
  printf '  В статусе proposed: %s ADR. Принять их все (status: accepted)? [y/N] ' "${#proposed[@]}"
  read -r answer
  if [[ "$answer" == "y" ]]; then
    git checkout -q -b adr/accept-0001-0019
    perl -pi -e 's/^status: proposed$/status: accepted/' "${proposed[@]}"
    node tools/trace/check.mjs >/dev/null
    git commit -qam "docs(adr): accept ADR-0001…0019 (owner)"
    git push -q -u origin adr/accept-0001-0019
    gh pr create --fill --base main >/dev/null
    gh pr checks --watch --interval 10 >/dev/null || true
    gh pr merge --squash --delete-branch
    git checkout -q main && git pull -q --ff-only
    echo "  ADR приняты и смержены."
  else
    echo "  Пропущено. Агент начнёт, но bootstrap отметит это как незакрытый гейт."
  fi
else
  echo "  Все ADR-0001…0019 уже приняты."
fi

say "5/7 Правила auto mode в ~/.claude/settings.json"
settings="$HOME/.claude/settings.json"
mkdir -p "$(dirname "$settings")"
[[ -f "$settings" ]] || echo '{}' > "$settings"
if jq -e '.autoMode' "$settings" >/dev/null; then
  echo "  Уже есть (проверить: claude auto-mode config)."
else
  cp "$settings" "$settings.bak.$(date +%Y%m%d%H%M%S)"
  jq -s '.[0] * .[1]' "$settings" tools/agent/user-settings.json > "$settings.tmp" && mv "$settings.tmp" "$settings"
  echo "  Добавлены; резервная копия рядом (.bak)."
fi

say "6/7 Telegram"
if [[ -s "$HOME/.config/reader-ai/telegram-chat-id" ]]; then
  echo "  Чат уже привязан."
else
  printf '  Отправь /start своему боту в Telegram, затем нажми Enter. '
  read -r _
  node tools/agent/notify.mjs link
fi

say "7/7 Проверка готовности"
bash tools/agent/bootstrap-mac.sh --install

say "Запуск агента (Ctrl+C остановит; повторный запуск продолжит с docs/progress.yaml)"
exec caffeinate -dimsu claude --permission-mode auto "$(cat tools/agent/kickoff.md)"
