# CLAUDE.md — agent rules for reader-ai

reader-ai turns any study material into an interactive, verified notebook course and runs it with Claude as a tutor. Owner: Ivan. Process: specd-lite, see [ADR-0001](docs/adr/0001-process-and-enforcement.md). Architecture overview: [docs/architecture.md](docs/architecture.md).

Unlike web3-noncustody, here **the agent writes the implementation**, but only for modules whose spec the owner has approved.

## How to work

- **Think before writing.** State what you understood and which options exist before the first edit.
- **Stop at every fork and ask the owner.** Present options with numbers and trade-offs; justify why a rejected option is worse with a number.
- **Spec first.** No implementation file without an approved spec whose `paths` claim it (hook H1, CI T8). Unknown numbers are measured in `spikes/` first; spikes never ship.
- **Simplicity first.** The least code and the fewest files that solve the task. No abstractions "for later".
- **Surgical changes.** Touch only what was asked; leave neighbouring code alone.
- **Verifiable goals.** Every change ends with a check: a test, trace-check, a Storybook story, or a live run against `docker compose up`.
- **Declarative style.** Data plus functions over it: pipeline stages, config, cell schemas, routes and MCP tools are declared as data; code interprets them.
- **Production quality.** Write as a senior engineer would for a shipped product: precise English, typed code (mypy strict, TypeScript strict), no filler, every claim backed by a source or a measurement.

## Roles (ADR-0001)

| What | Who |
| --- | --- |
| ADR draft (`proposed`), spec draft (`spec/modules/*.yaml`) | Agent |
| ADR `accepted` / `rejected`, spec `approved` / `implemented` | **Owner only** |
| Failing tests with `@trace` | Agent drafts, owner reviews with the spec |
| Implementation in `server/`, `web/`, `sandbox/`, `deploy/` | Agent, after spec approval |
| Spikes in `spikes/` (measurements that feed ADR numbers) | Agent, no spec needed, never imported by shipped code |
| Pilot course content in `pilot/` | Agent drafts, owner reviews |

## Hard rules (enforced by hooks and CI)

- **H1 spec-first.** An implementation file is writable only if an `approved`/`implemented` spec claims it through `paths`.
- **H2.** A test traced to an approved spec changes only together with the spec (specd P7). Propose the spec change first.
- **H3.** Never raise status: specs to `approved`/`implemented`, ADRs to `accepted`/`rejected`/`superseded`.
- **Do not bypass hooks through Bash** (`echo >`, `sed -i`, heredocs into project files). Write files only through Write/Edit. Generated files are produced by a committed generator, not by ad-hoc shell.
- Every test file contains `@trace <ID>`; IDs come only from specs.
- **Secrets never enter the repository, logs, traces, notebooks or prompts** (ADR-0011). Use `secret://` references.
- **Copyrighted source material is never committed.** Pilots reference sources by sha256 (ADR-0002).

## Repository map

```
spec/modules/     module specs (YAML); template: spec/_template.yaml
spec/fixtures/    fixed inputs with known outputs (VEC-*)
docs/adr/         decisions; template: 0000-template.md
docs/             architecture.md, glossary
server/           Python 3.13 service: API, workers, pipeline, tutor, MCP; tests in server/tests/
web/              React + TypeScript + Vite UI and Storybook; tests *.test.ts(x)
sandbox/          kernel image for lab execution (ADR-0014)
deploy/           compose.yaml, Caddyfile, reader.example.yaml
e2e/              Playwright end-to-end tests *.spec.ts
spikes/           throwaway measurements, outside the spec gate
pilot/bashir/     pilot course manifest (Bashir, Mastering Blockchain, RU 2025)
tools/            trace-check, guard-tests, hooks (Node)
```

## Before finishing a task

1. `node tools/trace/check.mjs` is green.
2. Tests for the touched layer were run; on a Red branch they fail as expected and for the right reason.
3. Report: done / how verified (commands + results) / not done and why / open questions.

## Language

Everything in the repository is in English. Course content follows the source language of the material (the Bashir pilot is Russian). Talk to the owner in Russian.
