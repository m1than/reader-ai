// @trace FR-DOD-4, FR-DOD-5, FR-DOD-6, FR-DOD-7, FR-DOD-8, FR-DOD-9, FR-DOD-10, FR-DOD-11, INV-DOD-1, INV-DOD-2, INV-DOD-3, NFR-DOD-1, VEC-DOD-2
// OPS-DOD report: VEC-DOD-2's synthetic run gives the expected INDEX.md and report.json byte for byte; the live.log
// grammar; path normalisation; exit codes; determinism, privacy and no-false-PASS properties; the size bound.
// Run: node --test "tools/dod/*.test.mjs"
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { parse as parseYaml } from 'yaml';
import { buildReport, parseLiveLog, InputError, INPUT_FILES } from './report.mjs';

const root = resolve(import.meta.dirname, '../..');
const dir = join(root, 'spec/fixtures/OPS-DOD/results');
const read = (name) => readFileSync(join(dir, name), 'utf8');
const fixture = () => ({
  index: parseYaml(read('index.yaml')),
  traces: parseYaml(read('traces.yaml')),
  inputs: Object.fromEntries(INPUT_FILES.map((name) => [name, read(name)])),
  commit: 'abc1234',
  root: '/repo',
});
const expectedMd = read('expected/INDEX.md');
const expectedJson = read('expected/report.json');
const CANARY = 'CANARY';

// A small seeded PRNG (mulberry32) so property tests are reproducible without dependencies.
const prng = (seed) => () => {
  seed = (seed + 0x6d2b79f5) | 0;
  let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
  t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
};
const shuffle = (list, rnd) => {
  const out = [...list];
  for (let i = out.length - 1; i > 0; i -= 1) {
    const j = Math.floor(rnd() * (i + 1));
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out;
};
const cellOf = (report, op, criterion) =>
  JSON.parse(report.reportJson).operations.find((o) => o.id === op).criteria.find((c) => c.criterion === criterion);

test('VEC-DOD-2: the synthetic run gives the expected INDEX.md and report.json (FR-DOD-5, -6, -7, -11)', () => {
  const out = buildReport(fixture());
  assert.equal(out.indexMd, expectedMd);
  assert.equal(out.reportJson, expectedJson);
  assert.equal(out.exitCode, 1);
});

test('FR-DOD-10: one line per operation for verify-all.sh', () => {
  assert.deepEqual(buildReport(fixture()).lines, [
    'FAIL lesson.read (2 missing, 1 failed)',
    'FAIL tutor.ask (3 missing, 3 failed)',
    'FAIL stack.up (4 missing, 1 failed)',
  ]);
});

test('FR-DOD-4: live.log grammar — TAB details, own parentheticals, ignored lines, ANSI', () => {
  const lines = [
    'PASS browser: math and code render in kernel outputs (KaTeX, Shiki)\t(3.4s)',
    'FAIL browser: bootstrap token appears in no artifact\t(found and removed in: trace.zip)',
    'PASS stage-1: bootstrap link issued for live-check (not printed)',
    '  PASS stage-1: indented is not evidence',
    'PASS: 3 of 3 checks',
    'PASS stage-9: unknown key',
    'NOTE browser: not a status',
    '\u001b[32mSKIP\u001b[0m browser: coloured',
  ].join('\n');
  assert.deepEqual(parseLiveLog(lines), [
    { status: 'PASS', label: 'browser: math and code render in kernel outputs (KaTeX, Shiki)' },
    { status: 'FAIL', label: 'browser: bootstrap token appears in no artifact' },
    { status: 'PASS', label: 'stage-1: bootstrap link issued for live-check (not printed)' },
    { status: 'SKIP', label: 'browser: coloured' },
  ]);
});

test("FR-DOD-4 Red case: passkey_check.py's pre-TAB FAIL format never counts for its label", () => {
  // Before the TAB change, report() printed `FAIL <name>: <detail>`; stage-1.sh prefixes `stage-1: `.
  const old = 'FAIL stage-1: sign-in options: assertion rejected';
  assert.deepEqual(parseLiveLog(old), [{ status: 'FAIL', label: 'stage-1: sign-in options: assertion rejected' }]);
  const f = fixture();
  f.index.operations[1].criteria.security = { live: ['stage-1: sign-in options'] };
  f.inputs['live.log'] = `${old}\n`;
  const report = buildReport(f);
  assert.equal(cellOf(report, 'tutor.ask', 'security').status, 'MISSING');
  assert.equal(JSON.parse(report.reportJson).live_fail_unmapped, 1);
  // With the TAB separator the same failure is the label's FAIL.
  f.inputs['live.log'] = 'FAIL stage-1: sign-in options\tassertion rejected\n';
  assert.equal(cellOf(buildReport(f), 'tutor.ask', 'security').status, 'FAIL');
});

test('FR-DOD-4: the ANSI variant of the fixture gives the same report', () => {
  const f = fixture();
  f.inputs['live.log'] = f.inputs['live.log'].replace(/^(PASS|FAIL|SKIP)/gm, '\u001b[32m$1\u001b[0m');
  const out = buildReport(f);
  assert.equal(out.indexMd, expectedMd);
  assert.equal(out.reportJson, expectedJson);
});

test('FR-DOD-4: a missing input file is absent evidence, not a failure', () => {
  const f = fixture();
  for (const name of INPUT_FILES) f.inputs[name] = null;
  const report = JSON.parse(buildReport(f).reportJson);
  const statuses = report.operations.flatMap((o) => o.criteria.map((c) => c.status));
  assert.ok(!statuses.includes('FAIL'));
  assert.equal(report.exit_code, 3); // enforced operations are MISSING
});

test('FR-DOD-4: test paths are normalised; an absolute path outside the repository is invalid input', () => {
  const f = fixture();
  f.inputs['vitest.json'] = f.inputs['vitest.json'].replace('/repo/web/src/a.test.ts', '/elsewhere/web/src/a.test.ts');
  assert.throws(() => buildReport(f), (e) => e instanceof InputError && e.exitCode === 2
    && e.message === 'report: vitest.json: a test path is outside the repository');
});

test('FR-DOD-9: an unknown key in an input object stops the report with exit 2, naming the file and the key', () => {
  const f = fixture();
  f.inputs['axe.json'] = JSON.stringify([{ page: '/', violations: { minor: 0, moderate: 0, serious: 0, critical: 0 }, nodes: [{ html: `<b>${CANARY}</b>` }] }]);
  assert.throws(() => buildReport(f), (e) => e instanceof InputError && e.exitCode === 2
    && e.message === 'report: axe.json: unknown key "nodes"' && !e.message.includes(CANARY));
  const g = fixture();
  g.inputs['ledger.jsonl'] += '{"operation":"tutor.ask","prompt":"x"}\n';
  assert.throws(() => buildReport(g), (e) => e.message === 'report: ledger.jsonl: unknown key "prompt"');
});

test('FR-DOD-10: exit codes 0 (clean), 1 (a FAIL), 3 (an enforced operation MISSING)', () => {
  const index = {
    version: 1,
    criteria: parseYaml(read('index.yaml')).criteria,
    operations: [{
      id: 'a.b', title: 'A', surface: ['cli'], gate: 'enforced',
      criteria: Object.fromEntries(['functional', 'security', 'privacy', 'a11y', 'latency', 'cost', 'observability', 'recovery']
        .map((c) => [c, c === 'a11y' || c === 'cost' ? { na: 'n/a' } : c === 'latency'
          ? { budget: { p95_ms_max: 10, n_min: 1, kind: 'target', source: 'structural: t' } }
          : { live: ['stage-1: ok'] }])),
    }],
  };
  const run = (log, timings) => buildReport({ index, traces: {}, commit: 'c', root: '/repo',
    inputs: { 'live.log': log, 'timings.jsonl': timings } });
  const timing = '{"operation":"a.b","ms":5,"run":"r"}\n';
  assert.equal(run('PASS stage-1: ok\n', timing).exitCode, 0);
  assert.equal(run('PASS stage-1: ok\nFAIL stage-1: other\n', timing).exitCode, 1); // an unmapped FAIL line
  assert.equal(run('FAIL stage-1: ok\n', timing).exitCode, 1);
  assert.equal(run('PASS stage-1: ok\n', '').exitCode, 3);
});

test('FR-DOD-10: the CLI writes INDEX.md and report.json, prints the lines and exits with the code', () => {
  const tmp = mkdtempSync(join(tmpdir(), 'dod-'));
  try {
    const results = join(tmp, 'results');
    mkdirSync(results);
    for (const name of INPUT_FILES) writeFileSync(join(results, name), read(name));
    writeFileSync(join(tmp, 'traces.json'), JSON.stringify(parseYaml(read('traces.yaml'))));
    const out = join(tmp, 'out');
    const r = spawnSync('node', [join(root, 'tools/dod/report.mjs'), '--index', join(dir, 'index.yaml'),
      '--results', results, '--commit', 'abc1234', '--out', out, '--root', '/repo',
      '--traces', join(tmp, 'traces.json')], { encoding: 'utf8' });
    assert.equal(r.status, 1, r.stderr);
    assert.equal(r.stdout, 'FAIL lesson.read (2 missing, 1 failed)\nFAIL tutor.ask (3 missing, 3 failed)\nFAIL stack.up (4 missing, 1 failed)\n');
    assert.equal(readFileSync(join(out, 'INDEX.md'), 'utf8'), expectedMd);
    assert.equal(readFileSync(join(out, 'report.json'), 'utf8'), expectedJson);
    const bad = spawnSync('node', [join(root, 'tools/dod/report.mjs'), '--index', join(dir, 'index.yaml')], { encoding: 'utf8' });
    assert.equal(bad.status, 2);
    assert.ok(!existsSync(join(tmp, 'none')));
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

test('FR-DOD-8 / INV-DOD-1: byte-identical on rerun and under permuted lines and test cases', () => {
  const first = buildReport(fixture());
  assert.deepEqual(buildReport(fixture()), first);
  const rnd = prng(20261123);
  for (let i = 0; i < 25; i += 1) {
    const f = fixture();
    for (const name of ['live.log', 'timings.jsonl', 'ledger.jsonl']) {
      f.inputs[name] = `${shuffle(f.inputs[name].split('\n').filter(Boolean), rnd).join('\n')}\n`;
    }
    const xml = f.inputs['pytest.xml'];
    const cases = xml.match(/<testcase[\s\S]*?(?:\/>|<\/testcase>)/g);
    f.inputs['pytest.xml'] = xml.replace(cases.join('\n'), shuffle(cases, rnd).join('\n'));
    const vitest = JSON.parse(f.inputs['vitest.json']);
    vitest.testResults = shuffle(vitest.testResults, rnd).map((r) => ({ ...r, assertionResults: shuffle(r.assertionResults, rnd) }));
    f.inputs['vitest.json'] = JSON.stringify(vitest);
    f.inputs['axe.json'] = JSON.stringify(shuffle(JSON.parse(f.inputs['axe.json']), rnd));
    const out = buildReport(f);
    assert.equal(out.indexMd, first.indexMd);
    assert.equal(out.reportJson, first.reportJson);
  }
});

test('FR-DOD-9 / INV-DOD-2: no input text reaches the outputs', () => {
  const rnd = prng(7);
  for (let i = 0; i < 20; i += 1) {
    const canary = `${CANARY}${Math.floor(rnd() * 1e9)}`;
    const f = fixture();
    for (const name of INPUT_FILES) f.inputs[name] = f.inputs[name].replaceAll(CANARY, canary);
    // more free text: failure messages, captured output, attachment names, an unmapped label
    f.inputs['live.log'] += `FAIL browser: ${canary}\tdetail ${canary}\n`;
    f.inputs['vitest.json'] = f.inputs['vitest.json'].replace('"failureMessages":[]', `"failureMessages":["${canary}"]`);
    const out = buildReport(f);
    assert.ok(!out.indexMd.includes(canary) && !out.reportJson.includes(canary));
    assert.ok(!out.lines.join('\n').includes(canary));
  }
});

test('INV-DOD-3: no false PASS — any bad evidence keeps a cell from PASS', () => {
  const base = buildReport(fixture());
  const passing = JSON.parse(base.reportJson).operations.flatMap((o) =>
    o.criteria.filter((c) => c.status === 'PASS').map((c) => ({ op: o.id, criterion: c.criterion })));
  assert.ok(passing.length >= 6);
  const mutations = {
    'a failed case in every traced file': (f) => {
      f.inputs['pytest.xml'] = f.inputs['pytest.xml'].replace('</testsuite>',
        '<testcase classname="x" name="f" file="tests/test_a.py" time="0"><failure message="m">t</failure></testcase></testsuite>');
      f.inputs['vitest.json'] = f.inputs['vitest.json'].replace('"status":"passed","failureMessages"', '"status":"failed","failureMessages"');
    },
    'a skipped case': (f) => {
      f.inputs['pytest.xml'] = f.inputs['pytest.xml'].replace('</testsuite>',
        '<testcase classname="x" name="s" file="tests/test_a.py" time="0"><skipped message="m"/></testcase></testsuite>');
      f.inputs['vitest.json'] = f.inputs['vitest.json'].replaceAll('"status":"passed","failureMessages"', '"status":"todo","failureMessages"');
    },
    'a FAIL line for every mapped label': (f) => {
      f.inputs['live.log'] += f.index.operations.flatMap((o) => Object.values(o.criteria).flatMap((c) => c.live ?? []))
        .map((l) => `FAIL ${l}\n`).join('');
    },
    'a measurement above every budget': (f) => {
      f.inputs['timings.jsonl'] += f.index.operations.map((o) => `{"operation":"${o.id}","ms":99999999,"run":"z"}\n{"operation":"${o.id}","ms":99999999,"run":"z","per":"page"}\n`).join('');
      f.inputs['ledger.jsonl'] += '{"operation":"tutor.ask","run_id":"big","stage":"tutor","attempt":1,"model":"m","input":1,"output":1,"thinking":null,"cache_write_5m":0,"cache_write_1h":0,"cache_read":0,"usd":"9.5"}\n';
    },
    'a serious violation on every listed page': (f) => {
      f.inputs['axe.json'] = JSON.stringify(JSON.parse(f.inputs['axe.json']).map((e) => ({ ...e, violations: { ...e.violations, critical: 1 } })));
    },
    'measurements removed below n_min': (f) => {
      f.inputs['timings.jsonl'] = '';
      f.inputs['ledger.jsonl'] = '';
    },
    'a flaky Playwright case': (f) => {
      f.traces['FR-X-1'] = [...f.traces['FR-X-1'], 'e2e/web/x.spec.ts'];
    },
  };
  // The cells each mutation must take out of PASS (the fixture's PASS cells and the evidence behind them).
  const hits = {
    'a failed case in every traced file': ['lesson.read functional', 'tutor.ask functional'],
    'a skipped case': ['lesson.read functional', 'tutor.ask functional'],
    'a FAIL line for every mapped label': ['lesson.read functional', 'lesson.read observability', 'tutor.ask functional'],
    'a measurement above every budget': ['lesson.read latency', 'tutor.ask cost', 'stack.up latency'],
    'a serious violation on every listed page': ['lesson.read a11y'],
    'measurements removed below n_min': ['lesson.read latency', 'tutor.ask cost', 'stack.up latency'],
    'a flaky Playwright case': ['lesson.read functional'],
  };
  assert.deepEqual(new Set(Object.values(hits).flat()), new Set(passing.map(({ op, criterion }) => `${op} ${criterion}`)));
  // Every non-empty subset of mutations, in a seeded random order, keeps every hit cell out of PASS.
  const names = Object.keys(mutations);
  const rnd = prng(42);
  for (let mask = 1; mask < 2 ** names.length; mask += 1) {
    const chosen = shuffle(names.filter((_, i) => mask & (1 << i)), rnd);
    const f = fixture();
    for (const name of chosen) mutations[name](f);
    const out = buildReport(f);
    for (const key of chosen.flatMap((name) => hits[name])) {
      const [op, criterion] = key.split(' ');
      assert.notEqual(cellOf(out, op, criterion).status, 'PASS', `${chosen.join(' + ')}: ${key}`);
    }
  }
});

test('NFR-DOD-1: 30 operations, 10,000 cases over 500 files, 2,000 live lines, 5,000 timings and ledger rows in under 5 s', () => {
  const criteria = parseYaml(read('index.yaml')).criteria;
  const traces = {};
  const operations = Array.from({ length: 30 }, (_, i) => {
    const items = Array.from({ length: 10 }, (_, k) => `FR-P-${i * 10 + k + 1}`);
    for (const [k, id] of items.entries()) traces[id] = [`server/tests/test_${(i * 10 + k) % 500}.py`];
    return {
      id: `op.n${String.fromCharCode(97 + (i % 26))}${i}`.replace(/[0-9]/g, (d) => 'abcdefghij'[d]), title: `Op ${i}`, surface: ['web'], gate: 'tracked',
      criteria: {
        functional: { items: items.slice(0, 5), live: [`browser: label ${i}`] },
        security: { items: items.slice(5) }, privacy: { missing: 'x' }, a11y: { pages: [`/p${i}`] },
        latency: { budget: { p95_ms_max: 1000, n_min: 10, kind: 'target', source: 'structural: t' } },
        cost: { budget: { usd_per_run_max: 1, n_min: 10, kind: 'target', source: 'structural: t' } },
        observability: { missing: 'x' }, recovery: { missing: 'x' },
      },
    };
  });
  const cases = Array.from({ length: 10000 }, (_, i) =>
    `<testcase classname="c" name="t${i}" file="tests/test_${i % 500}.py" time="0.001"${i % 97 ? ' />' : '><failure message="m">x</failure></testcase>'}`);
  const inputs = {
    'pytest.xml': `<testsuites><testsuite name="pytest">${cases.join('\n')}</testsuite></testsuites>`,
    'live.log': Array.from({ length: 2000 }, (_, i) => `PASS browser: label ${i % 30}\t(0.1s)`).join('\n'),
    'timings.jsonl': Array.from({ length: 5000 }, (_, i) => JSON.stringify({ operation: operations[i % 30].id, ms: i % 900, run: 'r' })).join('\n'),
    'ledger.jsonl': Array.from({ length: 5000 }, (_, i) => JSON.stringify({ operation: operations[i % 30].id, run_id: `r${i % 600}`, stage: 's', attempt: 1, model: 'm', input: 1, output: 1, thinking: null, cache_write_5m: 0, cache_write_1h: 0, cache_read: 1, usd: '0.001' })).join('\n'),
    'axe.json': JSON.stringify(operations.map((_, i) => ({ page: `/p${i}`, violations: { minor: 0, moderate: 0, serious: 0, critical: 0 } }))),
  };
  const started = performance.now();
  buildReport({ index: { version: 1, criteria, operations }, traces, inputs, commit: 'c', root: '/repo' });
  assert.ok(performance.now() - started < 5000);
});
