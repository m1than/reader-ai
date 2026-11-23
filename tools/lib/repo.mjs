// Shared repository model for trace-check, guard-tests and hooks.
// Pure functions over files: read -> parse -> return data.
import { readFileSync, readdirSync, existsSync, statSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import { parse as parseYaml } from 'yaml';

export const SPEC_STATUSES = ['draft', 'approved', 'implemented'];
export const LOCKED_SPEC_STATUSES = ['approved', 'implemented'];
// Delivery tracks (ADR-0001): product features, AI, engineering and optimization.
export const TRACKS = ['product', 'ai', 'engineering'];
export const ADR_STATUSES = ['proposed', 'accepted', 'superseded', 'rejected'];

export const MODULE_ID = /^(SRV|WEB|OPS)-[A-Z0-9]+$/;
export const ITEM_ID = /^(REF|FR|INV|NFR|VEC)-[A-Z0-9]+-\d+$/;
export const ADR_ID = /^ADR-\d{4}$/;
export const TRACE_TAG = /@trace\s+([A-Z0-9,\s-]+)/g;

const SKIP_DIRS = new Set([
  '.git', 'node_modules', 'dist', 'build', 'storybook-static',
  '.venv', '__pycache__', '.pytest_cache', '.ruff_cache', '.mypy_cache', 'data',
]);

const toPosix = (p) => p.split(sep).join('/');

export const walk = (root, dir = root) =>
  existsSync(dir)
    ? readdirSync(dir).flatMap((name) => {
        const full = join(dir, name);
        return SKIP_DIRS.has(name)
          ? []
          : statSync(full).isDirectory()
            ? walk(root, full)
            : [toPosix(relative(root, full))];
      })
    : [];

// Test locations (ADR-0001): pytest in server/tests/, Vitest *.test.ts(x) in web/,
// Playwright *.spec.ts in e2e/; node:test files of the Definition-of-Done tooling in tools/dod/ (OPS-DOD).
const TEST_PATTERNS = [
  /^server\/tests\/(.+\/)?test_[^/]+\.py$/,
  /^web\/.+\.test\.tsx?$/,
  /^e2e\/.+\.spec\.ts$/,
  /^tools\/dod\/.+\.test\.mjs$/,
];
// Implementation: application code in server/src, web/src, sandbox/ and deploy/.
// Stories (*.stories.tsx) are component documentation and count as implementation of the component.
const IMPL_PATTERN = /^(server\/src|web\/src|sandbox|deploy)\/.+\.(py|ts|tsx|js|mjs|css|Dockerfile|Caddyfile|ya?ml|toml|sh)$|^(sandbox|deploy)\/(.+\/)?(Dockerfile|Caddyfile)$/;
// Spikes (ADR-0001) are throwaway measurements outside the spec gate; they never ship.
const SPIKE_PATTERN = /^spikes\//;

export const isTestFile = (rel) => TEST_PATTERNS.some((re) => re.test(rel));
export const isImplFile = (rel) =>
  IMPL_PATTERN.test(rel) && !isTestFile(rel) && !SPIKE_PATTERN.test(rel);

// Glob subset used in spec `paths`: ** (any depth), * (one segment), literal rest.
export const globToRegex = (glob) =>
  new RegExp(`^${glob
    .replace(/[.+^${}()|[\]\\]/g, '\\$&')
    .replace(/\*\*\//g, '\u0000')
    .replace(/\*\*/g, '\u0001')
    .replace(/\*/g, '[^/]*')
    .replace(/\u0000/g, '(.+/)?')
    .replace(/\u0001/g, '.*')}$`);

// Specs that claim a file through their `paths` globs (ADR-0001, rules H1 and T8).
export const claimsOf = (specs, rel) =>
  specs.filter((spec) => (spec.doc.paths ?? []).some((glob) => globToRegex(glob).test(rel)));


export const isSpecFile = (rel) => /^spec\/modules\/[^/]+\.ya?ml$/.test(rel);
export const isAdrFile = (rel) => /^docs\/adr\/\d{4}-[^/]+\.md$/.test(rel) && !rel.includes('/0000-');

export const traceIds = (text) =>
  [...text.matchAll(TRACE_TAG)].flatMap((m) =>
    m[1].split(/[\s,]+/).filter((id) => ITEM_ID.test(id)),
  );

const SPEC_SECTIONS = ['normative', 'requirements', 'invariants', 'nfr', 'vectors'];

// A file that does not parse is reported by trace-check (T0), not thrown: one bad file must not hide the rest.
const safeYaml = (text) => {
  try { return { value: parseYaml(text) ?? {}, error: null }; }
  catch (e) { return { value: {}, error: e.message.split('\n')[0] }; }
};

export const parseSpec = (rel, text) => {
  const { value: doc, error } = safeYaml(text);
  const items = SPEC_SECTIONS.flatMap((section) =>
    (Array.isArray(doc[section]) ? doc[section] : []).map((item) => ({ ...item, section })),
  );
  return { file: rel, doc, items, parseError: error };
};

export const statusOf = (text) => safeYaml(text).value.status;

export const parseAdr = (rel, text) => {
  const front = text.match(/^---\n([\s\S]*?)\n---/);
  if (!front) return { file: rel, meta: null, parseError: null };
  const { value, error } = safeYaml(front[1]);
  return { file: rel, meta: value, parseError: error };
};

export const loadRepo = (root) => {
  const files = walk(root);
  const read = (rel) => readFileSync(join(root, rel), 'utf8');
  const specs = files.filter(isSpecFile).map((rel) => parseSpec(rel, read(rel)));
  const adrs = files.filter(isAdrFile).map((rel) => parseAdr(rel, read(rel)));
  const tests = files
    .filter(isTestFile)
    .map((rel) => ({ file: rel, ids: traceIds(read(rel)) }));
  const ownerOf = new Map(
    specs.flatMap((spec) => spec.items.map((item) => [item.id, spec])),
  );
  return { files, specs, adrs, tests, ownerOf };
};
