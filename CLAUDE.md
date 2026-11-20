@AGENTS.md

## Claude Code

- Start: `claude --permission-mode auto "$(cat tools/agent/kickoff.md)"` from the repository root. Auto mode and its classifier rules come from the owner's `~/.claude/settings.json` (`tools/agent/user-settings.json` is the snippet). Project settings cannot enable auto mode.
- `.claude/settings.json` holds the deny rules (secrets, environment dumps, force-push, pushes to `main`) and the hooks:
  - PreToolUse H1–H3: `tools/hooks/pre-write.mjs`;
  - Stop and Notification: `tools/agent/notify.mjs`, so the owner hears when you stop or need attention.
- A blocked edit or a denied command carries its reason. Fix the cause; never route around a hook or a deny rule.
- Long runs: keep `docs/progress.yaml` current, so a compaction or a new session loses nothing. After a compaction, re-read `docs/progress.yaml` and the current task's spec before continuing.
- Delegate broad searches to the Explore agent to keep the main context small.
- Complex work goes to the Fable subagents (AGENTS.md "Complex work goes to Fable"):
  - `deep-reviewer`: a fresh-context review; its approval is mandatory before you approve a spec or merge a change in a complex area;
  - `investigator`: root causes and subtle behaviour.
  Both are pinned to `model: fable` in `.claude/agents/`.
