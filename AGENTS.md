# AGENTS.md — rules for agents working on reader-ai

reader-ai turns any study material into an interactive, verified notebook course and runs it with an AI tutor. Owner: Ivan. Start with [docs/architecture.md](docs/architecture.md), [docs/roadmap.md](docs/roadmap.md) and the run state in [docs/progress.yaml](docs/progress.yaml).

You run **autonomously** (ADR-0019): you draft and approve specs, write tests and code, open and merge pull requests, and keep going without waiting for the owner. The owner reviews after, on each stage's live check.

## The loop

1. Read `docs/progress.yaml`. Pick the first task whose status is `todo` and whose `needs` are all `done`. If nothing is ready, take the first `todo` of the next stage that does not depend on a blocked task.
2. Create a branch `<stage>/<task-id>` from an up-to-date `main`.
3. Do the task completely: the spike, the spec, the Red tests, the Green code, and the live check script for its stage, as the task says.
4. Run the checks (below). Fix until green.
5. Update `docs/progress.yaml` in the same branch: status, PR link, measured numbers, anything learned that changes later tasks.
6. Push, open a PR, wait for CI. Merge it yourself (squash) when green, unless it changes an ADR status. ADR-status PRs wait for the owner.
7. When a stage's tasks are all done, run its live check (`bash e2e/live/stage-N.sh`), record the result in `docs/progress.yaml`, notify the owner and continue with the next stage.
8. Repeat. Never stop to ask. When something needs the owner, set the task to `blocked` with `reason` and `ask`, run `node tools/agent/notify.mjs "<one line>"`, and take the next ready task.

Commit at least after every unit of work. A run may be cut off at any time: what is committed and recorded in `docs/progress.yaml` survives, nothing else does.

## What blocks a task (ADR-0019)

- An action outside this repository that cannot be undone, such as publishing a package, a release in another repository, or deleting data outside `data/`.
- Spending above `budget.live_runs_usd` in `docs/progress.yaml`. That budget covers Agent SDK credit used by live checks and spikes. Building itself runs in this interactive session and spends nothing from it.
- A missing secret or access.
- A change that contradicts an accepted ADR. Write a new ADR with `status: proposed` instead, and keep working on what does not depend on it.
- The same check failing after 3 different fixes.

## Gates you never cross

- Spec `implemented`: the owner sets it after the stage live check.
- ADR `accepted` / `rejected` / `superseded`: the owner decides. You write ADRs as `proposed`.
- `main` is changed only through merged PRs; never force-push.

## Hard rules (hooks and CI)

- **H1 spec-first.** An implementation file is writable only if a spec with `status: approved` (or `implemented`) claims it through `paths`. Before approving your own spec, check it against its ADRs and run trace-check. Then set `status: approved` and `approved_by: agent`.
- **H2.** A test traced to an approved spec changes only together with that spec.
- **H3.** See "Gates you never cross".
- Write files only with your file tools. Never write files through shell redirection or `sed -i`. Generated files come from committed generators.
- Every test file contains `@trace <ID>`; IDs come only from specs.
- **Secrets never enter the repository, logs, traces, notebooks, prompts or your context.**
  - Never read `~/Developer/.token_tg_bot`, `.env` files or Keychain items.
  - Never print environment variables.
  - Reference secrets as `secret://…` (ADR-0011).
- Copyrighted source material is never committed. Pilots reference it by sha256.

## How to work

- Spec first; unknown numbers are measured in `spikes/` (never imported by shipped code) and recorded in `spikes/<name>/RESULT.md`.
- Simplicity: the least code and the fewest files that solve the task. Surgical changes.
- Declarative style: data plus functions over it. Pipeline stages, config, schemas, routes and MCP tools are declared as data.
- Production quality: typed code (mypy `--strict`, TypeScript `strict`), precise English, every claim backed by a source or a measurement.
- Verification follows the owner's preference: live `curl` checks against the running stack (`reader up`) in `e2e/live/`, plus unit and property tests where they pin behaviour.

## Complex work goes to Fable (owner, 2026-11-20)

Complex work is always done with Claude Fable, through the `deep-reviewer` and `investigator` subagents in `.claude/agents/`.

**Complex work means:**
- approving any spec, and drafting any ADR;
- these areas: sandbox and egress, auth and sessions, secrets, MCP authorization, outbox, jobs and idempotency, kernel and WebSocket relays, OTel context propagation, pricing and cost math, the compile pipeline's verification stages;
- any number that goes into a spec or ADR without a spike behind it;
- a check that failed twice after two different fixes.

**Rules:**
- `deep-reviewer` must return `VERDICT: approve` before you set a spec to `approved` or merge a change in one of the areas above. Record the verdict in the PR description.
- Use `investigator` for root causes and for subtle behaviour. Act on its FIX and TEST; do not keep guessing.
- If Fable is unavailable in the session, use the strongest available model, note `review_model: <model>` in docs/progress.yaml for that task, and notify the owner once.

## Checks before every PR

`bash tools/ci-local.sh` runs the first four lines below, the same jobs as `.github/workflows/ci.yml`. While GitHub Actions cannot run on the owner's account, its PASS is the gate (owner, 2026-11-20).

```bash
node tools/trace/check.mjs                              # T1–T9 (T9: docs/progress.yaml parses and is consistent)
node tools/trace/guard-tests.mjs --base origin/main     # P7
(cd server && uv run ruff check . && uv run mypy src && uv run pytest)    # once server/ exists
(cd web && npm run typecheck && npm test && npm run build-storybook)      # once web/ exists
bash e2e/live/stage-N.sh                                # for the stage you touched, against `reader up`
```

## Repository map

```
AGENTS.md, CLAUDE.md  agent rules (this file; CLAUDE.md adds Claude Code specifics)
docs/                 architecture.md, roadmap.md, progress.yaml (run state), adr/, research/
spec/modules/         module specs (YAML); template spec/_template.yaml; fixtures in spec/fixtures/
server/               Python 3.13: api, worker, mcp, host CLI `reader`; tests in server/tests/
web/                  React + TypeScript + Vite + Storybook; tests *.test.ts(x)
sandbox/              kernel image (ADR-0014)
deploy/               compose.yaml, Caddyfile, Rotel config, reader.example.yaml
e2e/                  Playwright *.spec.ts; live curl checks in e2e/live/stage-N.sh
spikes/               measurements outside the spec gate
pilot/bashir/         pilot course manifest, KB snapshot
tools/                trace-check, guard-tests, hooks; tools/agent/ (bootstrap, notify, kickoff)
```

## The owner's machine

- macOS, Apple Silicon, Docker Desktop. v1 runs only here (ADR-0012).
- Provider credential: `CLAUDE_CODE_OAUTH_TOKEN` in the environment, referenced as `secret://env/CLAUDE_CODE_OAUTH_TOKEN`.
- Telegram bot token: a private file on the host, referenced as `secret://hostfile/…`. Only `tools/agent/notify.mjs` and `reader up` read it.
- Repositories: `m1than/reader-ai` and `m1than/ui-builder-lib-with-agents`. The library has its own rules: in that repository, follow its AGENTS.md, which forbids agents to push or publish. Prepare changes there as a branch, then block the task for the owner.

## Language

Everything in the repository is in English. Course content follows the language of the material (the Bashir pilot is Russian). Messages to the owner, including Telegram notifications, are in Russian.
