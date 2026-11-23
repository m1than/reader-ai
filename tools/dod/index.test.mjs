// @trace FR-DOD-1, FR-DOD-2, FR-DOD-3, FR-DOD-12, FR-DOD-13, INV-DOD-4, VEC-DOD-1, VEC-DOD-3
// OPS-DOD: the index shape (FR-DOD-1/2), trace-check T10 (FR-DOD-3), the waiver rule (FR-DOD-12) and the P8
// ratchet (FR-DOD-13) over the hand-written cases of VEC-DOD-3, plus the committed index (VEC-DOD-1) against the
// real repository. Run: node --test "tools/dod/*.test.mjs"
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { parse as parseYaml } from 'yaml';
import { loadRepo } from '../lib/repo.mjs';
import {
  CRITERIA, INDEX_FILE, DISABLED_LABELS, validateIndex, t10, t10Repo, repoContext, routeRegistry, cliRegistry,
  liveLabels, waiverViolations, ratchet, p8, nextText,
} from './index.mjs';

const root = resolve(import.meta.dirname, '../..');
const cases = parseYaml(readFileSync(join(root, 'spec/fixtures/OPS-DOD/t10-cases.yaml'), 'utf8'));
const byCode = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
const sorted = (list) => [...list].sort(byCode);
const clone = (x) => structuredClone(x);

const applyPatch = (doc, patch = []) => {
  const out = clone(doc);
  for (const { path, value, delete: del } of patch) {
    const parent = path.slice(0, -1).reduce((node, key) => node[key], out);
    const key = path.at(-1);
    if (del) {
      if (Array.isArray(parent)) parent.splice(key, 1);
      else delete parent[key];
    } else parent[key] = value;
  }
  return out;
};
const applyReplace = (text, pairs = []) => pairs.reduce((t, [from, to]) => {
  assert.equal(t.split(from).length, 2, `replace: "${from}" must occur exactly once`);
  return t.replace(from, to);
}, text);

const baseIndex = parseYaml(cases.base_yaml);

test('VEC-DOD-3 base: valid, no error, the expected information lines', () => {
  const { errors, info } = t10(baseIndex, cases.context);
  assert.deepEqual(errors, []);
  assert.deepEqual(info, cases.info);
});

for (const c of cases.cases) {
  test(`VEC-DOD-3 T10: ${c.name}`, () => {
    const index = applyPatch(parseYaml(applyReplace(cases.base_yaml, c.replace)), c.patch);
    const context = applyPatch(cases.context, c.context_patch);
    assert.deepEqual(t10(index, context).errors, sorted(c.errors));
  });
}

for (const c of cases.ratchet) {
  test(`VEC-DOD-3 ratchet: ${c.name}`, () => {
    const base = c.base_absent ? null : applyPatch(baseIndex, c.base_patch);
    assert.deepEqual(ratchet(base, applyPatch(baseIndex, c.head_patch)), sorted(c.errors));
  });
}

for (const c of cases.waivers) {
  test(`VEC-DOD-3 waiver rule: ${c.name}`, () => {
    const before = applyPatch(baseIndex, c.before_patch);
    assert.deepEqual(waiverViolations(before, applyPatch(baseIndex, c.after_patch)), sorted(c.errors));
  });
}

test('validateIndex alone checks FR-DOD-1/2 without a repository', () => {
  assert.deepEqual(validateIndex(baseIndex), []);
  assert.deepEqual(validateIndex(null), ['index: must be a mapping']);
  assert.deepEqual(CRITERIA, ['functional', 'security', 'privacy', 'a11y', 'latency', 'cost', 'observability', 'recovery']);
});

test('P8: errors fail unless a commit carries the DoD-Owner-Change trailer', () => {
  const base = cases.base_yaml;
  const head = cases.base_yaml.replace('gate: enforced', 'gate: tracked');
  const blocked = p8(base, head, ['feat: something\n']);
  assert.deepEqual(blocked.errors, ['web.read: enforced → tracked']);
  assert.equal(blocked.ok, false);
  const owner = p8(base, head, ['chore: demote\n\nDoD-Owner-Change: the owner moved web.read back\n']);
  assert.equal(owner.ok, true);
  assert.deepEqual(owner.errors, ['web.read: enforced → tracked']);
  assert.deepEqual(p8(null, head, []), { errors: [], ok: true });
  assert.deepEqual(p8(base, null, []).errors, ['index removed']);
});

test('FR-DOD-12: the hook computes the written text for Write, Edit (also replace_all) and MultiEdit', () => {
  assert.equal(nextText('a b a', { content: 'x' }), 'x');
  assert.equal(nextText('a b a', { old_string: 'b', new_string: 'c' }), 'a c a');
  assert.equal(nextText('a b a', { old_string: 'a', new_string: 'c', replace_all: true }), 'c b c');
  assert.equal(nextText('a b', { edits: [{ old_string: 'a', new_string: 'x' }, { old_string: 'b', new_string: 'y' }] }), 'x y');
});

// Against the real repository (registries, live scripts, specs, tests).

test('the route registry is read from the ROUTERS modules', () => {
  const routes = routeRegistry(root);
  for (const r of ['GET /api/healthz', 'POST /api/search', 'POST /api/compiles/{run_id}/resume',
    'WS /api/kernel/sessions/{session_id}/channels', 'GET /api/lessons/{lesson_id}/questions']) {
    assert.ok(routes.includes(r), r);
  }
  assert.equal(new Set(routes).size, routes.length);
});

test('the CLI registry holds leaf subcommands, other scripts and their store_true flags', () => {
  const { entries, flags } = cliRegistry(root);
  for (const e of ['reader up', 'reader watcher status', 'reader watcher stop', 'reader secrets set', 'reader-ingest',
    'reader-ingest --setup', 'reader-ingest --requeue', 'reader-server', 'reader-server --healthcheck']) {
    assert.ok(entries.includes(e), e);
  }
  for (const e of ['reader watcher', 'reader secrets', 'reader down --untrust', 'reader-ingest --config']) {
    assert.ok(!entries.includes(e), e);
  }
  assert.ok(flags['reader down'].includes('--untrust'));
  assert.ok(!flags['reader up'].includes('--untrust'));
});

test('live labels come from the scripts named in index.mjs; format strings are skipped', () => {
  const labels = liveLabels(root);
  assert.ok(labels['stage-1'].includes('reader doctor: all ok'));
  assert.ok(labels['stage-1'].includes('sign-in options'));
  assert.ok(labels.browser.includes('bootstrap token appears in no artifact'));
  assert.ok(labels.browser.includes('math and code render in kernel outputs (KaTeX, Shiki)'));
  assert.ok(!labels.browser.some((l) => l.includes('{')));
});

test('the disabled labels are data and each is emitted by stage-1.sh', () => {
  const emitted = liveLabels(root)['stage-1'];
  assert.ok(DISABLED_LABELS.length >= 6);
  for (const { label, reason } of DISABLED_LABELS) {
    assert.ok(label.startsWith('stage-1: ') && emitted.includes(label.slice('stage-1: '.length)), label);
    assert.equal(reason, 'S1.OPS-CLI-restore-seed');
  }
});

test('VEC-DOD-1 / INV-DOD-4: the committed index passes T10 with gate tracked everywhere', () => {
  const index = parseYaml(readFileSync(join(root, INDEX_FILE), 'utf8'));
  assert.ok(index.operations.every((op) => op.gate === 'tracked'));
  const { errors } = t10(index, repoContext(root, loadRepo(root)));
  assert.deepEqual(errors, []);
});

test('T10 over the repository: index absent is information while draft, an error once approved', () => {
  const ctx = repoContext(root, loadRepo(root));
  assert.deepEqual(t10Repo(null, ctx, 'draft'), { errors: [], info: [`${INDEX_FILE} absent: T10 skipped (OPS-DOD is draft)`] });
  assert.deepEqual(t10Repo(null, ctx, 'approved').errors, [`${INDEX_FILE} is absent and OPS-DOD is approved`]);
});
