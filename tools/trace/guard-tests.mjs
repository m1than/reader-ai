#!/usr/bin/env node
// guard-tests: tests must not change unless their spec changes too (specd P7, ADR-0001).
// CI on pull requests: node tools/trace/guard-tests.mjs --base origin/main
// A test traced to an approved/implemented spec may change only in a PR that also
// changes that spec. Escape hatch: commit trailer "Spec-Neutral: <reason>"
// (a test refactor that does not change meaning).
import { execFileSync } from 'node:child_process';
import { resolve } from 'node:path';
import { loadRepo, isTestFile, traceIds, LOCKED_SPEC_STATUSES } from '../lib/repo.mjs';

const arg = (name) => process.argv[process.argv.indexOf(name) + 1];
const base = arg('--base') ?? 'origin/main';
const root = resolve('.');
const git = (...args) => execFileSync('git', args, { cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] });
const gitOr = (fallback, ...args) => { try { return git(...args); } catch { return fallback; } };

if (gitOr(null, 'rev-parse', '--verify', '--quiet', base) === null) {
  console.error(`✗ base "${base}" not found: CI needs a checkout with fetch-depth: 0`);
  process.exit(1);
}

const changed = git('diff', '--name-only', `${base}...HEAD`).split('\n').filter(Boolean);
const neutral = /^Spec-Neutral:\s*\S+/m.test(git('log', '--format=%B', `${base}..HEAD`));
const repo = loadRepo(root);

const idsAt = (file) => [
  ...traceIds(gitOr('', 'show', `HEAD:${file}`)),
  ...traceIds(gitOr('', 'show', `${base}:${file}`)),
];

const violations = changed
  .filter(isTestFile)
  .map((file) => ({
    file,
    locked: [...new Set(idsAt(file).map((id) => repo.ownerOf.get(id)).filter(Boolean))]
      .filter((spec) => LOCKED_SPEC_STATUSES.includes(spec.doc.status)),
  }))
  .filter(({ locked }) => locked.length > 0)
  .filter(({ locked }) => !locked.some((spec) => changed.includes(spec.file)));

const lines = violations.map(({ file, locked }) =>
  `✗ ${file} changed, but its spec ${locked.map((s) => s.file).join(', ')} did not`);

console.log(lines.length
  ? [...lines, neutral
    ? '\nSpec-Neutral trailer present, allowing.'
    : '\nChange the spec, or add a commit trailer "Spec-Neutral: <reason>".'].join('\n')
  : `✓ P7: ${changed.filter(isTestFile).length} test file(s) changed, no violations`);
process.exit(lines.length && !neutral ? 1 : 0);
