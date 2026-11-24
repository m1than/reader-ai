// @trace NFR-FRESHUI-1
// The card is a lazy chunk of its own, within the spec's gzip bound, and none of it is in the initial chunks.
import { mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { gzipSync } from 'node:zlib';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { readYaml, webRoot } from '../app/testing/fixtures';

let outDir = '';

beforeAll(async () => {
  outDir = mkdtempSync(join(tmpdir(), 'reader-web-fresh-build-'));
  const { build } = await import('vite');
  const previous = process.env.NODE_ENV;
  process.env.NODE_ENV = 'production';
  try {
    await build({ root: webRoot, configFile: `${webRoot}vite.config.ts`, mode: 'production', logLevel: 'silent', build: { outDir, emptyOutDir: true } });
  } finally {
    process.env.NODE_ENV = previous;
  }
}, 180_000);

afterAll(() => rmSync(outDir, { recursive: true, force: true }));

// Strings only the card has (FR-FRESHUI-3, -6).
const MARKERS = ['Run for up to', '(as reported)', 'noopener noreferrer nofollow'];

describe('freshness chunk (NFR-FRESHUI-1)', () => {
  it('is lazy, within the threshold, and absent from the initial chunks', () => {
    const spec = readYaml<{ nfr: Array<{ id: string; threshold: number }> }>('spec/modules/WEB-FRESH.yaml');
    const threshold = spec.nfr.find((n) => n.id === 'NFR-FRESHUI-1')?.threshold ?? 0;
    expect(threshold).toBeGreaterThan(0);
    const assets = join(outDir, 'assets');
    const html = readFileSync(join(outDir, 'index.html'), 'utf8');
    const initial = new Set([...html.matchAll(/(?:src|href)="\/assets\/([^"]+\.js)"/g)].map((m) => m[1]!));
    const js = readdirSync(assets).filter((f) => f.endsWith('.js'));
    const card = js.filter((f) => MARKERS.every((m) => readFileSync(join(assets, f), 'utf8').includes(m)));
    expect(card).toHaveLength(1);
    expect(initial.has(card[0]!)).toBe(false);
    for (const f of initial) for (const m of MARKERS) expect(readFileSync(join(assets, f), 'utf8'), `${f} holds ${m}`).not.toContain(m);
    const size = gzipSync(readFileSync(join(assets, card[0]!)), { level: 9 }).length;
    console.info(`NFR-FRESHUI-1 freshness chunk ${card[0]} gzip ${size} B`);
    expect(size).toBeLessThanOrEqual(threshold);
  });
});
