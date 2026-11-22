// Source scans used by the tests (WEB-UI INV-UI-2, FR-UI-1, FR-UI-8). Not imported by the app.
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative } from 'node:path';

const webRoot = process.cwd();

const walk = (dir: string): string[] =>
  readdirSync(dir, { withFileTypes: true }).flatMap((d) => (d.isDirectory() ? walk(join(dir, d.name)) : [join(dir, d.name)]));

/** Application sources: every .ts/.tsx/.mjs under src/ except tests, stories, test helpers and this scanner. */
const appSources = (): string[] =>
  walk(join(webRoot, 'src'))
    .map((f) => relative(webRoot, f))
    .filter((f) => /\.(ts|tsx|mjs)$/.test(f))
    .filter((f) => !/\.(test|stories)\.tsx?$/.test(f) && !f.startsWith('src/app/testing/') && f !== 'src/app/sourceScan.ts');

const FORBIDDEN: Array<[string, RegExp]> = [
  ['dangerouslySetInnerHTML', /dangerouslySetInnerHTML/],
  ['innerHTML', /\.innerHTML\b/],
  ['outerHTML', /\.outerHTML\b/],
  ['insertAdjacentHTML', /insertAdjacentHTML/],
  ['document.write', /document\.write(ln)?\s*\(/],
  ['DOMParser', /\bDOMParser\b/],
  ['createContextualFragment', /createContextualFragment/],
  ['eval', /(^|[^.\w])eval\s*\(/],
  ['new Function', /new\s+Function\s*\(/],
  ['string timer', /set(Timeout|Interval)\s*\(\s*['"`]/],
];

export const findForbidden = (source: string): string[] => FORBIDDEN.filter(([, re]) => re.test(source)).map(([name]) => name);

export const forbiddenUses = (): string[] =>
  appSources().flatMap((f) => findForbidden(readFileSync(join(webRoot, f), 'utf8')).map((name) => `${f}: ${name}`));

/** FR-UI-1: the library entries the slice may import. */
export const allowedEntries = [
  'button', 'toast', 'progress', 'skeleton', 'tooltip', 'dialog', 'chip', 'divider', 'link', 'typography', 'stack',
  'container', 'app-bar', 'list-view', 'alert', 'styles.css',
];

export const libraryImports = (): string[] => {
  const files = [...appSources(), ...walk(join(webRoot, '.storybook')).map((f) => relative(webRoot, f))];
  const entries = files.flatMap((f) =>
    [...readFileSync(join(webRoot, f), 'utf8').matchAll(/['"]@acme\/ui\/([\w.-]+)['"]/g)].map((m) => m[1]!),
  );
  return [...new Set(entries)].sort();
};

/** FR-UI-8: .tsx files under src/{ui,app,notebook,render} that export a PascalCase component. */
export const componentFiles = (): string[] =>
  appSources()
    .filter((f) => /^src\/(ui|app|notebook|render)\/.+\.tsx$/.test(f))
    .filter((f) => /export\s+(function|const)\s+[A-Z]/.test(readFileSync(join(webRoot, f), 'utf8')))
    .sort();
