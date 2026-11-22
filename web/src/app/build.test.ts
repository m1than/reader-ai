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
  await build({ root: webRoot, configFile: `${webRoot}vite.config.ts`, logLevel: 'silent', build: { outDir, emptyOutDir: true } });
}, 180_000);

afterAll(() => rmSync(outDir, { recursive: true, force: true }));

const files = (dir: string): string[] =>
  readdirSync(dir, { withFileTypes: true }).flatMap((d) => (d.isDirectory() ? files(join(dir, d.name)) : [join(dir, d.name)]));

describe('build (FR-UI-9)', () => {
  it('emits fonts and images as files and inlines no data: URL', () => {
    const all = files(outDir);
    expect(all.some((f) => /\.woff2$/.test(f))).toBe(true);
    const text = all.filter((f) => /\.(css|js|html)$/.test(f)).map((f) => readFileSync(f, 'utf8'));
    expect(text.filter((t) => /url\(\s*["']?data:/.test(t))).toEqual([]);
    expect(text.filter((t) => /["'`]data:(image|font)\//.test(t))).toEqual([]);
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
