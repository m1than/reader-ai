// @trace NFR-NOTEBOOK-1
// The lab editor's CodeMirror chunks stay lazy and within their budget (WEB-NOTEBOOK NFR-NOTEBOOK-1, design pass).
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { gzipSync } from 'node:zlib';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { readYaml, webRoot } from '../app/testing/fixtures';

let outDir = '';

beforeAll(async () => {
  outDir = mkdtempSync(join(tmpdir(), 'reader-web-editor-'));
  const { build } = await import('vite');
  const previous = process.env.NODE_ENV;
  process.env.NODE_ENV = 'production';
  try {
    await build({
      root: webRoot,
      configFile: `${webRoot}vite.config.ts`,
      mode: 'production',
      logLevel: 'silent',
      build: { outDir, emptyOutDir: true, sourcemap: true },
    });
  } finally {
    process.env.NODE_ENV = previous;
  }
}, 180_000);

afterAll(() => rmSync(outDir, { recursive: true, force: true }));

const assets = () => readdirSync(join(outDir, 'assets')).filter((f) => f.endsWith('.js'));
const editorChunks = () =>
  assets().filter((f) => {
    const map = join(outDir, 'assets', `${f}.map`);
    if (!existsSync(map)) return false;
    const sources = (JSON.parse(readFileSync(map, 'utf8')) as { sources: string[] }).sources;
    return sources.some((s) => /node_modules\/@(codemirror|lezer)\//.test(s));
  });

describe('editor chunk budget (NFR-NOTEBOOK-1)', () => {
  it('puts CodeMirror in lazy chunks only, within the spec threshold', () => {
    const spec = readYaml<{ nfr: Array<{ id: string; threshold: number }> }>('spec/modules/WEB-NOTEBOOK.yaml');
    const threshold = spec.nfr.find((n) => n.id === 'NFR-NOTEBOOK-1')?.threshold ?? 0;
    const chunks = editorChunks();
    expect(chunks.length).toBeGreaterThan(0);
    const html = readFileSync(join(outDir, 'index.html'), 'utf8');
    for (const c of chunks) expect(html, c).not.toContain(c);
    const size = chunks.reduce((sum, f) => sum + gzipSync(readFileSync(join(outDir, 'assets', f)), { level: 9 }).length, 0);
    console.info(`NFR-NOTEBOOK-1 editor chunks gzip ${size} B over ${chunks.length} chunks`);
    expect(threshold).toBe(137768);
    expect(size).toBeLessThanOrEqual(threshold);
  });
});
