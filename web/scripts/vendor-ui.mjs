#!/usr/bin/env node
// Builds the owner's component library at the pinned tag into web/vendor/ (WEB-UI FR-UI-1).
//   node web/scripts/vendor-ui.mjs <library checkout>
// Reads web/vendor/acme-ui.lock.json, checks that the tag resolves to the recorded commit in the given checkout,
// clones that commit into a temporary directory, runs the library's own `npm ci && npm run build && npm pack`,
// renames the tarball after the tag, and rewrites the lock file with its sha256 and the Node and npm versions used.
// It never runs a git command that writes to a remote (the library's AGENTS.md forbids agents to push or publish).
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { copyFileSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const vendorDir = resolve(here, '..', 'vendor');
const lockPath = join(vendorDir, 'acme-ui.lock.json');

const run = (cmd, args, cwd) => execFileSync(cmd, args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'inherit'] }).trim();

const main = (checkout) => {
  if (!checkout) throw new Error('usage: vendor-ui.mjs <library checkout>');
  const lock = JSON.parse(readFileSync(lockPath, 'utf8'));
  const commit = run('git', ['rev-parse', `${lock.tag}^{commit}`], resolve(checkout));
  if (commit !== lock.commit) throw new Error(`tag ${lock.tag} resolves to ${commit}, lock records ${lock.commit}`);
  const work = mkdtempSync(join(tmpdir(), 'reader-vendor-ui-'));
  try {
    run('git', ['clone', '--quiet', '--no-checkout', resolve(checkout), work]);
    run('git', ['checkout', '--quiet', '--detach', commit], work);
    run('npm', ['ci', '--no-audit', '--no-fund', '--ignore-scripts'], work);
    run('npm', ['run', 'build'], work);
    const packed = run('npm', ['pack', '--silent'], work).split('\n').pop();
    const tarball = `acme-ui-${lock.tag}.tgz`;
    copyFileSync(join(work, packed), join(vendorDir, tarball));
    const sha256 = createHash('sha256').update(readFileSync(join(vendorDir, tarball))).digest('hex');
    const next = { ...lock, tarball, sha256, node: process.version, npm: run('npm', ['--version'], work) };
    writeFileSync(lockPath, `${JSON.stringify(next, null, 2)}\n`);
    console.log(`${tarball} ${sha256}`);
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
};

main(process.argv[2]);
