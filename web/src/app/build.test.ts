// @trace FR-UI-9 NFR-UI-1
import { mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { gzipSync } from 'node:zlib';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { readYaml, webRoot } from './testing/fixtures';

let outDir = '';

beforeAll(async () => {
  outDir = mkdtempSync(join(tmpdir(), 'reader-web-build-'));
  const { build } = await import('vite');
  // Vitest sets NODE_ENV=test, which would make Vite bundle React's development build; measure the production build.
  const previous = process.env.NODE_ENV;
  process.env.NODE_ENV = 'production';
  try {
    await build({ root: webRoot, configFile: `${webRoot}vite.config.ts`, mode: 'production', logLevel: 'silent', build: { outDir, emptyOutDir: true } });
  } finally {
    process.env.NODE_ENV = previous;
  }
}, 180_000);

afterAll(() => rmSync(outDir, { recursive: true, force: true }));

const files = (dir: string): string[] =>
  readdirSync(dir, { withFileTypes: true }).flatMap((d) => (d.isDirectory() ? files(join(dir, d.name)) : [join(dir, d.name)]));

describe('build (FR-UI-9)', () => {
  it('emits fonts and images as files and inlines no data: URL', () => {
    const all = files(outDir);
    expect(all.some((f) => /\.woff2$/.test(f))).toBe(true);
    const text = all.filter((f) => /\.(css|js|html)$/.test(f)).map((f) => readFileSync(f, 'utf8'));
    // The vendored library's stylesheet carries data: URLs as literals of its own (FR-UI-9 note); nothing else may.
    const dataUrls = (s: string) => [...s.matchAll(/url\(\s*["']?(data:[^"')]*)/g)].map((m) => m[1]!.replace(/\s+/g, ''));
    const library = new Set(dataUrls(readFileSync(`${webRoot}node_modules/@acme/ui/dist/styles.css`, 'utf8')));
    // Exactly one library literal (the table sort mask at v0.1.0): a new one fails here until it is reviewed.
    expect(library.size).toBe(1);
    // Distinct URLs: the minifier may repeat the literal for a vendor-prefixed property (-webkit-mask).
    expect(new Set(text.flatMap(dataUrls)).size).toBe(1);
    expect(text.flatMap(dataUrls).filter((u) => !library.has(u))).toEqual([]);
    const withoutLibrary = text.map((t) => [...library].reduce((s, u) => s.split(u).join(''), t.replace(/\s+/g, '')));
    expect(withoutLibrary.flatMap((t) => t.match(/["'`]data:(image|font)\/[^"'`]{0,40}/g) ?? [])).toEqual([]);
  });

  it('keeps markdown, math, highlighting and the lesson view out of the initial chunks', () => {
    const html = readFileSync(join(outDir, 'index.html'), 'utf8');
    const initial = [...html.matchAll(/(?:src|href)="\/(assets\/[^"]+\.js)"/g)].map((m) => readFileSync(join(outDir, m[1]!), 'utf8'));
    expect(initial.length).toBeGreaterThan(0);
    const joined = initial.join('\n');
    expect(joined).not.toMatch(/katex|KaTeX/);
    expect(joined).not.toMatch(/createHighlighterCore|shiki/);
  });
});

describe('bundle budget (NFR-UI-1)', () => {
  it('initial JavaScript of the lesson-list route stays within the spec threshold', () => {
    const spec = readYaml<{ nfr: Array<{ id: string; threshold: number }> }>('spec/modules/WEB-UI.yaml');
    const threshold = spec.nfr.find((n) => n.id === 'NFR-UI-1')?.threshold ?? 0;
    const html = readFileSync(join(outDir, 'index.html'), 'utf8');
    const initial = [...html.matchAll(/(?:src|href)="\/(assets\/[^"]+\.js)"/g)].map((m) => m[1]!);
    const size = initial.reduce((sum, f) => sum + gzipSync(readFileSync(join(outDir, f)), { level: 9 }).length, 0);
    console.info(`NFR-UI-1 initial JS gzip ${size} B over ${initial.length} chunks`);
    expect(threshold).toBeGreaterThan(0);
    expect(size).toBeLessThanOrEqual(threshold);
  });
});
