#!/usr/bin/env node
// Writes ../results/sizes.json: the gallery's JS and CSS, the React-only baseline, the tarball, and the
// library's own per-entry sizes. Run after `vite build` and `vite build --mode baseline`.
//   node sizes.mjs <library checkout> <tarball>
// gzip level 9 everywhere, as the library's size gate measures.
import { execFileSync, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readdirSync, readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { gzipSync } from 'node:zlib';

const here = dirname(fileURLToPath(import.meta.url));
const [library, tarball] = process.argv.slice(2);
const ENTRIES = ['dialog', 'tabs', 'menu', 'tooltip', 'toast', 'popover', 'drawer', 'data-grid', 'tree-view', 'progress', 'switch', 'slider', 'text-field', 'select', 'chip', 'avatar', 'date-calendar', 'charts', 'skeleton', 'scroll-area'];

const asset = (dir, ext) => {
  const file = readdirSync(join(here, dir, 'assets')).find((name) => name.endsWith(ext));
  const bytes = readFileSync(join(here, dir, 'assets', file));
  return { file: `${dir}/assets/${file}`, raw: bytes.length, gzip9: gzipSync(bytes, { level: 9 }).length };
};
const js = asset('dist', '.js');
const css = asset('dist', '.css');
const baseline = asset('dist-baseline', '.js');

// The library's size gate prints a table: │ i │ 'entry' │ size │ budget │ status │. It runs on the library's own
// runtime (LIBRARY_NODE, default `node` on PATH; its CI uses Node 22). Under Node 26 the same gate reports a few
// bytes more for some entries (different bundled zlib), so the Node binary is recorded with the numbers.
const libraryNode = process.env.LIBRARY_NODE ?? 'node';
const sizeGate = spawnSync(libraryNode, ['scripts/check-size.mjs'], { cwd: library, encoding: 'utf8' });
const table = sizeGate.stdout;
const libraryNodeVersion = execFileSync(libraryNode, ['--version'], { encoding: 'utf8' }).trim();
const perEntry = Object.fromEntries(table.split('\n')
  .map((line) => line.split('│').map((cell) => cell.trim().replace(/'/g, '')))
  .filter((cells) => ENTRIES.includes(cells[2]))
  .map((cells) => [cells[2], { gzip9: Number(cells[3]), budget: Number(cells[4]) }]));

const tgz = readFileSync(tarball);
const result = {
  commands: {
    gallery: 'vite build (consumer/, Node 26.13.0 from /opt/homebrew/opt/node@26/bin)',
    baseline: 'vite build --mode baseline (React 19 + ReactDOM only, same config)',
    perEntry: 'node scripts/check-size.mjs in the library checkout (min + gzip -9 per entry, React external, shared layers and Floating UI counted in every entry)',
    this: 'node consumer/sizes.mjs <library> <tarball>',
  },
  galleryJs: js,
  galleryCss: css,
  baselineJs: baseline,
  libraryShareJsGzip9: js.gzip9 - baseline.gzip9,
  tarball: { file: tarball.split('/').at(-1), bytes: tgz.length, sha256: createHash('sha256').update(tgz).digest('hex') },
  perEntryRuntime: { node: libraryNodeVersion, sizeGateExit: sizeGate.status, sizeGateFailures: sizeGate.stderr.trim().split('\n').filter(Boolean) },
  perEntry,
  perEntrySum: Object.values(perEntry).reduce((n, { gzip9 }) => n + gzip9, 0),
};
mkdirSync(join(here, '..', 'results'), { recursive: true });
writeFileSync(join(here, '..', 'results', 'sizes.json'), `${JSON.stringify(result, null, 2)}\n`);
console.log(JSON.stringify({ libraryShareJsGzip9: result.libraryShareJsGzip9, galleryCss: css.gzip9, baseline: baseline.gzip9, tarball: result.tarball, entries: Object.keys(perEntry).length }));
