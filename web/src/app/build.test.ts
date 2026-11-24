// @trace FR-UI-9 NFR-UI-1 FR-UI-11 NFR-NOTEBOOK-1 FR-UI-30
// One production build for the build checks, WEB-UI's initial-chunk budget and WEB-NOTEBOOK's editor-chunk budget
// (design pass review: shared build). `sourcemap: 'hidden'` writes maps without a sourceMappingURL comment, so the
// measured bytes are the served ones.
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
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
    await build({
      root: webRoot,
      configFile: `${webRoot}vite.config.ts`,
      mode: 'production',
      logLevel: 'silent',
      build: { outDir, emptyOutDir: true, sourcemap: 'hidden' },
    });
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
    // Design pass (FR-UI-9): CodeMirror's `.cm-highlightTab` literal, reviewed; only in the lazy editor chunk.
    const codemirror = new Set(dataUrls(readFileSync(`${webRoot}node_modules/@codemirror/view/dist/index.js`, 'utf8')));
    expect(codemirror.size).toBe(1);
    const allowed = new Set([...library, ...codemirror]);
    // Distinct URLs: the minifier may repeat the library literal for a vendor-prefixed property (-webkit-mask).
    expect(new Set(text.flatMap(dataUrls)).size).toBe(2);
    expect(text.flatMap(dataUrls).filter((u) => !allowed.has(u))).toEqual([]);
    const initial = new Set(initialChunks());
    for (const f of all.filter((x) => x.endsWith('.js'))) {
      const asset = f.slice(outDir.length + 1);
      if (!dataUrls(readFileSync(f, 'utf8')).some((u) => codemirror.has(u))) continue;
      expect(initial.has(asset), asset).toBe(false);
      expect(sourcesOf(asset).some((s) => /node_modules\/@codemirror\/view\//.test(s)), asset).toBe(true);
    }
    const withoutAllowed = text.map((t) => [...allowed].reduce((s, u) => s.split(u).join(''), t.replace(/\s+/g, '')));
    expect(withoutAllowed.flatMap((t) => t.match(/["'`]data:(image|font)\/[^"'`]{0,40}/g) ?? [])).toEqual([]);
  });

  it('keeps markdown, math, highlighting and the lesson view out of the initial chunks', () => {
    const html = readFileSync(join(outDir, 'index.html'), 'utf8');
    const initial = [...html.matchAll(/(?:src|href)="\/(assets\/[^"]+\.js)"/g)].map((m) => readFileSync(join(outDir, m[1]!), 'utf8'));
    expect(initial.length).toBeGreaterThan(0);
    const joined = initial.join('\n');
    expect(joined).not.toMatch(/katex|KaTeX/);
    expect(joined).not.toMatch(/createHighlighterCore|shiki/);
  });

  it('copies theme.js from src/public to the build root, where index.html loads it (FR-UI-16, code review B1)', () => {
    expect(existsSync(join(outDir, 'theme.js'))).toBe(true);
    expect(readFileSync(join(outDir, 'theme.js'), 'utf8')).toContain("localStorage.getItem('reader-ai.theme')");
    expect(readFileSync(join(outDir, 'index.html'), 'utf8')).toMatch(/<head>[\s\S]*<script src="\/theme\.js"><\/script>[\s\S]*<\/head>/);
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

// Every chunk's sources, from its hidden source map.
const sourcesOf = (asset: string): string[] => {
  const map = join(outDir, `${asset}.map`);
  return existsSync(map) ? (JSON.parse(readFileSync(map, 'utf8')) as { sources: string[] }).sources : [];
};
const initialChunks = () => {
  const html = readFileSync(join(outDir, 'index.html'), 'utf8');
  return [...html.matchAll(/(?:src|href)="\/(assets\/[^"]+\.js)"/g)].map((m) => m[1]!);
};

describe('lazy menu, drawer and icons (FR-UI-11, design pass review B1)', () => {
  it('keeps the library menu, drawer and icon entries, and CodeMirror, out of the initial chunks', () => {
    const initial = initialChunks();
    expect(initial.length).toBeGreaterThan(0);
    const sources = initial.flatMap(sourcesOf);
    expect(sources.length).toBeGreaterThan(10);
    expect(sources.filter((s) => /@acme\/ui\/dist\/esm\/(menu|drawer|icon)\.js$/.test(s))).toEqual([]);
    expect(sources.filter((s) => /node_modules\/@(codemirror|lezer)\//.test(s))).toEqual([]);
  });
});

describe('lazy Russian catalogues and management layer (FR-UI-30)', () => {
  it('lists no messages.ru module and no web/src/manage module other than the slot stubs in an initial chunk', () => {
    const initial = initialChunks();
    const sources = initial.flatMap(sourcesOf);
    expect(sources.length).toBeGreaterThan(10);
    expect(sources.filter((s) => /messages\.ru\.ts$/.test(s))).toEqual([]);
    expect(sources.filter((s) => /src\/manage\//.test(s) && !/src\/manage\/slot\.tsx$/.test(s))).toEqual([]);
    // The English catalogue of the shell is static; the Russian one exists, in a lazy chunk.
    expect(sources.some((s) => /src\/app\/messages\.en\.ts$/.test(s))).toBe(true);
    const assets = readdirSync(join(outDir, 'assets')).filter((f) => f.endsWith('.js')).map((f) => `assets/${f}`);
    expect(assets.some((a) => sourcesOf(a).some((s) => /src\/app\/messages\.ru\.ts$/.test(s)))).toBe(true);
  });
});

describe('editor chunk budget (NFR-NOTEBOOK-1)', () => {
  it('puts CodeMirror in lazy chunks only, within the spec threshold', () => {
    const spec = readYaml<{ nfr: Array<{ id: string; threshold: number }> }>('spec/modules/WEB-NOTEBOOK.yaml');
    const threshold = spec.nfr.find((n) => n.id === 'NFR-NOTEBOOK-1')?.threshold ?? 0;
    const assets = readdirSync(join(outDir, 'assets')).filter((f) => f.endsWith('.js')).map((f) => `assets/${f}`);
    const chunks = assets.filter((a) => sourcesOf(a).some((s) => /node_modules\/@(codemirror|lezer)\//.test(s)));
    expect(chunks.length).toBeGreaterThan(0);
    const initial = new Set(initialChunks());
    for (const c of chunks) expect(initial.has(c), c).toBe(false);
    const size = chunks.reduce((sum, f) => sum + gzipSync(readFileSync(join(outDir, f)), { level: 9 }).length, 0);
    console.info(`NFR-NOTEBOOK-1 editor chunks gzip ${size} B over ${chunks.length} chunks`);
    expect(threshold).toBe(137768);
    expect(size).toBeLessThanOrEqual(threshold);
  });
});
