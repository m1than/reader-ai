# tools/

specd-lite tooling ([ADR-0001](../docs/adr/0001-process-and-enforcement.md)). Node ≥ 20, one dependency: `yaml`. Adapted from web3-noncustody: Python and TypeScript layers, and a spec-first gate instead of a closed implementation.

```bash
npm ci --prefix tools
node tools/trace/check.mjs                           # T1–T8: spec ↔ tests ↔ code ↔ ADRs
node tools/trace/guard-tests.mjs --base origin/main  # P7: tests change only with their spec
```

| File | Purpose |
| --- | --- |
| `lib/repo.mjs` | Repository model: specs, ADRs, tests, `@trace` tags, file classification, `paths` claims |
| `trace/check.mjs` | Rules T1–T8, CI job `trace` |
| `trace/guard-tests.mjs` | Rule P7 on pull requests; escape hatch: commit trailer `Spec-Neutral: <reason>` |
| `hooks/pre-write.mjs` | Claude Code PreToolUse hook: H1–H3, wired in `.claude/settings.json` |

## File classes

| Class | Pattern |
| --- | --- |
| Test | `server/tests/**/test_*.py`, `web/**/*.test.ts(x)`, `e2e/**/*.spec.ts` |
| Implementation (spec-gated) | `server/src/**`, `web/src/**`, `sandbox/**`, `deploy/**` source and config files |
| Outside the gate | `spikes/**`, manifests (`pyproject.toml`, `package.json`), tool configs (`vite.config.ts`), `pilot/**`, docs |

## Verified scenarios (2026-11-20)

Run against a scratch copy of the repository with an `SRV-INGEST` fixture spec claiming `server/src/reader/ingest/**`.

| # | Scenario | Expected | Result |
| --- | --- | --- | --- |
| 0 | Current repository (16 ADRs, no specs) | check passes | ✓ 0 errors |
| 1 | Write implementation claimed only by a draft spec | H1 blocks | ✓ exit 2 |
| 2 | Write implementation no spec claims | H1 blocks | ✓ exit 2 |
| 3 | Write under `spikes/` | allowed | ✓ exit 0 |
| 4 | Agent sets a spec to `approved` | H3 blocks | ✓ exit 2 |
| 5 | Agent sets an ADR to `accepted` | H3 blocks | ✓ exit 2 |
| 6 | Agent creates a new ADR already `accepted` | H3 blocks | ✓ exit 2 |
| 7 | Write implementation claimed by an approved spec | allowed | ✓ exit 0 |
| 8 | Write implementation outside the approved spec's paths | H1 blocks | ✓ exit 2 |
| 9 | Write `web/vite.config.ts` | allowed (tooling) | ✓ exit 0 |
| 10 | CI with an unclaimed implementation file next to a claimed one | T8 fails on the unclaimed file only | ✓ 1 error |
| 11 | Edit a test of an approved spec, spec untouched | H2 blocks | ✓ exit 2 |
| 12 | Tooling dependencies missing | hook fails closed | ✓ exit 2 |

P7 (`guard-tests.mjs`) is unchanged from web3-noncustody, where its five scenarios were verified on 2026-11-07.
