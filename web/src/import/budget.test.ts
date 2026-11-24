// @trace NFR-IMPORT-1
// The /import page is a lazy chunk and stays under its gzip budget; it does not grow the initial chunks (NFR-UI-1).
import { mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { gzipSync } from 'node:zlib';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { readYaml, webRoot } from '../app/testing/fixtures';

let outDir = '';

beforeAll(async () => {
  outDir = mkdtempSync(join(tmpdir(), 'reader-web-import-build-'));
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

describe('import page budget (NFR-IMPORT-1)', () => {
  it('keeps the /import lazy chunks within the threshold and out of the initial chunks', () => {
    const spec = readYaml<{ nfr: Array<{ id: string; threshold: number }> }>('spec/modules/WEB-IMPORT.yaml');
    const threshold = spec.nfr.find((n) => n.id === 'NFR-IMPORT-1')?.threshold ?? 0;
    const assets = join(outDir, 'assets');
    const html = readFileSync(join(outDir, 'index.html'), 'utf8');
    const initial = new Set([...html.matchAll(/(?:src|href)="\/assets\/([^"]+\.js)"/g)].map((m) => m[1]!));
    const js = readdirSync(assets).filter((f) => f.endsWith('.js'));
    const page = js.filter((f) => /Choose a PDF|Your sources/.test(readFileSync(join(assets, f), 'utf8')));
    expect(page.length).toBeGreaterThan(0);
    expect(page.filter((f) => initial.has(f))).toEqual([]);
    const size = page.reduce((sum, f) => sum + gzipSync(readFileSync(join(assets, f)), { level: 9 }).length, 0);
    console.info(`NFR-IMPORT-1 /import lazy JS gzip ${size} B over ${page.length} chunks`);
    expect(threshold).toBeGreaterThan(0);
    expect(size).toBeLessThanOrEqual(threshold);
  });
});
