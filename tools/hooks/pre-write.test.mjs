// Tests for the PreToolUse write hook: the gates hold in every git worktree of this repository.
// Run: node --test tools/hooks/pre-write.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const repo = resolve(import.meta.dirname, '../..');
const hook = join(repo, 'tools/hooks/pre-write.mjs');
const run = (toolInput) =>
  spawnSync('node', [hook], {
    input: JSON.stringify({ tool_input: toolInput }),
    env: { ...process.env, CLAUDE_PROJECT_DIR: repo },
    encoding: 'utf8',
  });

test('a sibling worktree is gated like the main checkout', (t) => {
  const parent = mkdtempSync(join(tmpdir(), 'hook-wt-'));
  const wt = join(parent, 'wt');
  execFileSync('git', ['-C', repo, 'worktree', 'add', '--detach', '-q', wt, 'HEAD']);
  t.after(() => {
    execFileSync('git', ['-C', repo, 'worktree', 'remove', '--force', wt]);
    rmSync(parent, { recursive: true, force: true });
  });
  // H1: an implementation file no spec claims.
  const h1 = run({ file_path: join(wt, 'server/src/reader/zz_unclaimed/x.py'), content: 'x' });
  assert.equal(h1.status, 2);
  assert.match(h1.stderr, /^\[H1\]/);
  // H3: an ADR decision is the owner's.
  const h3 = run({
    file_path: join(wt, 'docs/adr/0001-process-and-enforcement.md'),
    old_string: 'status: proposed',
    new_string: 'status: accepted',
  });
  assert.equal(h3.status, 2);
  assert.match(h3.stderr, /^\[H3\]/);
});

test('files outside every checkout of this repository are not gated', () => {
  const dir = mkdtempSync(join(tmpdir(), 'hook-out-'));
  try {
    writeFileSync(join(dir, 'a.txt'), '');
    assert.equal(run({ file_path: join(dir, 'a.txt'), content: 'x' }).status, 0);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
