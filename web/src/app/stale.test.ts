// @trace INV-UI-4
// "Fresh builds": the test server refuses a dist or storybook-static older than any of its inputs (code review
// 2026-11-24, after a stale Storybook build failed two render.spec checks with no product fault).
import { mkdirSync, mkdtempSync, rmSync, utimesSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { BUILDS, INPUT_DIRS, INPUT_FILES, staleBuilds } from '../../test-server/stale.mjs';

const roots: string[] = [];
afterEach(() => {
  for (const r of roots.splice(0)) rmSync(r, { recursive: true, force: true });
});

const T0 = 1_700_000_000; // seconds
const put = (root: string, rel: string, at: number) => {
  const path = join(root, rel);
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, 'x');
  utimesSync(path, at, at);
};
/** A web root whose inputs are at T0 and whose builds are at T0 + 10. */
const fresh = () => {
  const root = mkdtempSync(join(tmpdir(), 'stale-'));
  roots.push(root);
  for (const d of INPUT_DIRS) put(root, `${d}/a/b.txt`, T0);
  for (const f of INPUT_FILES) put(root, f, T0);
  for (const b of BUILDS) put(root, b, T0 + 10);
  return root;
};

describe('staleBuilds (INV-UI-4, fresh builds)', () => {
  it('names the inputs and builds the server checks', () => {
    expect(INPUT_DIRS).toEqual(['src', '.storybook', 'vendor']);
    expect(INPUT_FILES).toEqual(['index.html', 'package.json', 'package-lock.json', 'vite.config.ts']);
    expect(BUILDS).toEqual(['dist/index.html', 'storybook-static/index.json']);
  });

  it('accepts builds newer than every input', () => {
    expect(staleBuilds(fresh())).toEqual([]);
  });

  it('names a stale dist and a stale Storybook build', () => {
    const root = fresh();
    put(root, 'dist/index.html', T0 - 1);
    expect(staleBuilds(root)).toEqual(['dist/index.html']);
    put(root, 'storybook-static/index.json', T0 - 1);
    expect(staleBuilds(root)).toEqual(['dist/index.html', 'storybook-static/index.json']);
  });

  it('names a missing build', () => {
    const root = fresh();
    rmSync(join(root, 'storybook-static'), { recursive: true });
    expect(staleBuilds(root)).toEqual(['storybook-static/index.json']);
  });

  it.each([...INPUT_DIRS.map((d) => `${d}/deep/er/new.ts`), ...INPUT_FILES])('treats a newer %s as making both builds stale', (rel) => {
    const root = fresh();
    put(root, rel, T0 + 20);
    expect(staleBuilds(root)).toEqual(BUILDS);
  });

  it('ignores a newer .DS_Store', () => {
    const root = fresh();
    put(root, 'src/.DS_Store', T0 + 20);
    put(root, 'vendor/x/.DS_Store', T0 + 20);
    expect(staleBuilds(root)).toEqual([]);
  });
});
