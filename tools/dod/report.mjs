#!/usr/bin/env node
// OPS-DOD report (FR-DOD-4..11): one live run's results → docs/dod/INDEX.md and report.json.
//   node tools/dod/report.mjs --index <yaml> --results <dir> --commit <sha> --out <dir> [--root <repo>] [--traces <json>]
// Exit 0: no FAIL and no enforced operation MISSING; 1: a FAIL (a cell, or a FAIL line no operation maps);
// 2: invalid input; 3: an enforced operation MISSING. Prints one line per operation for verify-all.sh.
// Only ids, titles, criterion keys, item ids, index labels, statuses, counts, durations, tokens and usd reach the
// outputs (FR-DOD-9): test names, messages, captured output and log text are read for their status only.
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { isAbsolute, join, posix, relative, resolve, sep } from 'node:path';
import { parse as parseYaml } from 'yaml';
import { CRITERIA, validateIndex } from './index.mjs';

export const INPUT_FILES = ['pytest.xml', 'vitest.json', 'playwright.json', 'live.log', 'axe.json', 'timings.jsonl', 'ledger.jsonl'];
const TIMING_KEYS = ['operation', 'ms', 'run', 'per'];
const LEDGER_KEYS = ['operation', 'run_id', 'stage', 'attempt', 'model', 'input', 'output', 'thinking', 'cache_write_5m',
  'cache_write_1h', 'cache_read', 'usd'];
const TOKENS = ['input', 'output', 'thinking', 'cache_write_5m', 'cache_write_1h', 'cache_read'];
const IMPACTS = ['minor', 'moderate', 'serious', 'critical'];
const LIVE_LINE = /^(PASS|FAIL|SKIP) ((?:stage-1|browser): [^\t]+)(\t.*)?$/;
const ANSI = /\u001b\[[0-9;]*m/g;

export class InputError extends Error {
  constructor(message) { super(message); this.exitCode = 2; }
}

const byCode = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
const sortKeys = (x) => (Array.isArray(x) ? x.map(sortKeys)
  : x !== null && typeof x === 'object' ? Object.fromEntries(Object.keys(x).sort(byCode).map((k) => [k, sortKeys(x[k])])) : x);
const isMap = (x) => x !== null && typeof x === 'object' && !Array.isArray(x);

// ---------------------------------------------------------------- inputs (FR-DOD-4)

export const parseLiveLog = (text) => (text ?? '').split('\n').flatMap((raw) => {
  const m = raw.replace(ANSI, '').replace(/\r$/, '').match(LIVE_LINE);
  return m ? [{ status: m[1], label: m[2] }] : [];
});

const toRepoPath = (file, name, root, base) => {
  const full = isAbsolute(file) ? file : posix.join(base, file);
  const rel = relative(root, full);
  if (rel.startsWith('..') || isAbsolute(rel)) throw new InputError(`report: ${name}: a test path is outside the repository`);
  return rel.split(sep).join('/');
};

const json = (name, text) => {
  try { return JSON.parse(text); } catch { throw new InputError(`report: ${name}: not valid JSON`); }
};
const jsonl = (name, text, keys) => (text ?? '').split('\n').filter((l) => l.trim()).map((line) => {
  const row = json(name, line);
  if (!isMap(row)) throw new InputError(`report: ${name}: a line is not an object`);
  const unknown = Object.keys(row).find((k) => !keys.includes(k));
  if (unknown !== undefined) throw new InputError(`report: ${name}: unknown key "${unknown}"`);
  return row;
});

const decode = (s) => s.replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');

// Cases as [repo-relative file, status], status ∈ passed | failed | error | skipped.
const pytestCases = (xml, root) => [...(xml ?? '').matchAll(/<testcase\b([^>]*?)(\/>|>([\s\S]*?)<\/testcase>)/g)].map((m) => {
  const file = m[1].match(/\bfile="([^"]*)"/)?.[1];
  if (file === undefined) throw new InputError('report: pytest.xml: a testcase has no file attribute (use -o junit_family=xunit1)');
  const body = m[3] ?? '';
  const status = /<failure\b/.test(body) ? 'failed' : /<error\b/.test(body) ? 'error' : /<skipped\b/.test(body) ? 'skipped' : 'passed';
  return [toRepoPath(decode(file), 'pytest.xml', root, posix.join(root, 'server')), status];
});

const VITEST = { passed: 'passed', failed: 'failed', skipped: 'skipped', pending: 'skipped', todo: 'skipped', disabled: 'skipped' };
const vitestCases = (text, root) => {
  if (text === null || text === undefined) return [];
  const doc = json('vitest.json', text);
  return (doc.testResults ?? []).flatMap((r) => {
    const file = toRepoPath(String(r.name), 'vitest.json', root, root);
    return (r.assertionResults ?? []).map((a) => [file, VITEST[a.status] ?? 'failed']);
  });
};

const PLAYWRIGHT = { expected: 'passed', unexpected: 'failed', flaky: 'failed', skipped: 'skipped' };
const playwrightCases = (text, root) => {
  if (text === null || text === undefined) return [];
  const doc = json('playwright.json', text);
  const base = doc.config?.rootDir ?? root;
  const walk = (suite) => [
    ...(suite.specs ?? []).flatMap((spec) => (spec.tests ?? []).map((t) =>
      [toRepoPath(String(spec.file ?? suite.file), 'playwright.json', root, base), PLAYWRIGHT[t.status] ?? 'failed'])),
    ...(suite.suites ?? []).flatMap(walk),
  ];
  return (doc.suites ?? []).flatMap(walk);
};

const axeEntries = (text) => {
  if (text === null || text === undefined) return [];
  const doc = json('axe.json', text);
  if (!Array.isArray(doc)) throw new InputError('report: axe.json: must be a list');
  return doc.map((e) => {
    const unknown = Object.keys(e).find((k) => !['page', 'violations'].includes(k))
      ?? Object.keys(e.violations ?? {}).find((k) => !IMPACTS.includes(k));
    if (unknown !== undefined) throw new InputError(`report: axe.json: unknown key "${unknown}"`);
    return { page: String(e.page), bad: (e.violations?.serious ?? 0) + (e.violations?.critical ?? 0) > 0 };
  });
};

// Decimal string → integer micro-USD, without floating point.
const micro = (usd) => {
  const m = String(usd).match(/^(\d+)(?:\.(\d{1,6}))?$/);
  if (!m) throw new InputError('report: ledger.jsonl: usd must be a decimal string with at most 6 decimals');
  return Number(m[1]) * 1e6 + Number((m[2] ?? '').padEnd(6, '0'));
};
const usdText = (µ) => `${Math.floor(µ / 1e6)}.${String(µ % 1e6).padStart(6, '0')}`;
const budgetMicro = (n) => Math.round(n * 1e6);

// ---------------------------------------------------------------- evidence (FR-DOD-5, -6)

const nearest = (sorted, q) => sorted[Math.max(1, Math.ceil(q * sorted.length)) - 1];

const emptyTotals = () => Object.fromEntries([...TOKENS, 'usd_micro'].map((k) => [k, 0]));
const addRow = (t, row) => {
  for (const k of TOKENS) t[k] += Number(row[k] ?? 0) || 0;
  t.usd_micro += row.µ;
  return t;
};
const shown = (t) => (t === null ? null : { ...t, usd: usdText(t.usd_micro) });

const costColumns = (rows, budget) => {
  const runs = new Map();
  for (const r of rows) runs.set(r.run_id, addRow(runs.get(r.run_id) ?? emptyTotals(), r));
  const per = [...runs.values()];
  const group = (key) => Object.fromEntries([...rows.reduce((m, r) => m.set(r[key], addRow(m.get(r[key]) ?? emptyTotals(), r)), new Map())]
    .map(([k, t]) => [k, shown(t)]));
  const all = rows.reduce(addRow, emptyTotals());
  const denom = all.input + all.cache_write_5m + all.cache_write_1h + all.cache_read;
  const fold = (fn) => (per.length ? Object.fromEntries(Object.keys(emptyTotals()).map((k) => [k, fn(per.map((t) => t[k]))])) : null);
  return {
    budget: budget ?? null,
    by_model: group('model'),
    by_stage: group('stage'),
    cache_read_share: rows.length ? (denom ? Math.round((all.cache_read / denom) * 1e4) / 1e4 : 0) : null,
    per_run_max: shown(fold((v) => Math.max(...v))),
    per_run_mean: shown(fold((v) => Math.round(v.reduce((a, b) => a + b, 0) / v.length))),
    runs: per.length,
    _per: per,
  };
};

const latencyColumns = (rows, budget) => {
  const per = budget?.per ?? null;
  const ms = rows.filter((r) => (r.per ?? null) === per).map((r) => Math.round(Number(r.ms))).sort((a, b) => a - b);
  return { budget: budget ?? null, n: ms.length, p50_ms: ms.length ? nearest(ms, 0.5) : null, p95_ms: ms.length ? nearest(ms, 0.95) : null, per };
};

const budgetStatus = (criterion, budget, lat, cost) => {
  if (criterion === 'latency') {
    if (lat.n < budget.n_min) return 'NONE';
    const over = (budget.p95_ms_max !== undefined && lat.p95_ms > budget.p95_ms_max)
      || (budget.p50_ms_max !== undefined && lat.p50_ms > budget.p50_ms_max);
    return over ? 'FAIL' : 'PASS';
  }
  if (cost.runs < budget.n_min) return 'NONE';
  const over = cost._per.some((t) => (budget.usd_per_run_max !== undefined && t.usd_micro > budgetMicro(budget.usd_per_run_max))
    || (budget.tokens_per_run_max !== undefined
      && t.input + t.output + t.cache_write_5m + t.cache_write_1h + t.cache_read > budget.tokens_per_run_max));
  return over ? 'FAIL' : 'PASS';
};

const RANK = { FAIL: 4, MISSING: 3, PASS: 2, WAIVED: 1, NA: 0 };

export const buildReport = ({ index, traces, inputs, commit, root }) => {
  const shape = validateIndex(index);
  if (shape.length) throw new InputError(`report: index: ${shape.length} error(s); run trace-check (T10)`);
  const input = (name) => inputs[name] ?? null;

  const cases = new Map();
  for (const [file, status] of [...pytestCases(input('pytest.xml'), root), ...vitestCases(input('vitest.json'), root),
    ...playwrightCases(input('playwright.json'), root)]) {
    cases.set(file, [...(cases.get(file) ?? []), status]);
  }
  const itemStatus = (id) => {
    const files = traces[id] ?? [];
    const per = files.map((f) => cases.get(f) ?? []);
    if (per.some((c) => c.includes('failed') || c.includes('error'))) return 'FAIL';
    if (files.length && per.every((c) => c.includes('passed') && !c.includes('skipped'))) return 'PASS';
    return 'NONE';
  };

  const live = new Map();
  for (const { status, label } of parseLiveLog(input('live.log'))) live.set(label, [...(live.get(label) ?? []), status]);
  const labelStatus = (label) => {
    const s = live.get(label) ?? [];
    return s.includes('FAIL') ? 'FAIL' : s.includes('PASS') ? 'PASS' : 'NONE';
  };
  const mapped = new Set(index.operations.flatMap((op) => CRITERIA.flatMap((c) => op.criteria[c].live ?? [])));
  const unmappedFail = [...live].filter(([label, s]) => !mapped.has(label) && s.includes('FAIL')).length;

  const axe = new Map();
  for (const { page, bad } of axeEntries(input('axe.json'))) axe.set(page, (axe.get(page) ?? false) || bad);
  const pageStatus = (page) => (!axe.has(page) ? 'NONE' : axe.get(page) ? 'FAIL' : 'PASS');

  const timings = jsonl('timings.jsonl', input('timings.jsonl'), TIMING_KEYS);
  const ledger = jsonl('ledger.jsonl', input('ledger.jsonl'), LEDGER_KEYS).map((r) => ({ ...r, µ: micro(r.usd) }));

  const operations = index.operations.map((op) => {
    const lat = latencyColumns(timings.filter((t) => t.operation === op.id), op.criteria.latency.budget);
    const costRows = ledger.filter((r) => r.operation === op.id);
    const cost = costColumns(costRows, op.criteria.cost.budget);
    const criteria = CRITERIA.map((c) => {
      const cell = op.criteria[c];
      const evidence = [];
      const gaps = [];
      let status;
      if ('waived' in cell) status = 'WAIVED';
      else if ('na' in cell) {
        status = c === 'cost' && costRows.length ? 'FAIL' : 'NA';
        if (status === 'FAIL') evidence.push({ id: 'cost', kind: 'ledger', status: 'FAIL' });
      } else {
        for (const id of cell.items ?? []) evidence.push({ id, kind: 'item', status: itemStatus(id) });
        for (const l of cell.live ?? []) evidence.push({ id: l, kind: 'label', status: labelStatus(l) });
        if (cell.budget) evidence.push({ id: c, kind: 'budget', status: budgetStatus(c, cell.budget, lat, cost) });
        for (const p of cell.pages ?? []) evidence.push({ id: p, kind: 'page', status: pageStatus(p) });
        for (const e of evidence.filter((x) => x.status === 'NONE')) {
          gaps.push(`no evidence in this run for ${e.kind}${e.kind === 'budget' || e.kind === 'item' || e.kind === 'page' ? ` ${e.id}` : ` "${e.id}"`}`);
        }
        if (cell.missing) gaps.push(cell.missing);
        status = evidence.some((e) => e.status === 'FAIL') ? 'FAIL' : gaps.length ? 'MISSING' : 'PASS';
      }
      evidence.sort((a, b) => byCode(a.kind, b.kind) || byCode(a.id, b.id));
      return { criterion: c, evidence, gaps: status === 'MISSING' ? gaps.sort(byCode) : [], status };
    });
    const status = criteria.reduce((worst, c) => (RANK[c.status] > RANK[worst] ? c.status : worst), 'NA');
    delete cost._per;
    return {
      cost, criteria,
      fail: criteria.filter((c) => c.status === 'FAIL').length,
      gate: op.gate, id: op.id, latency: lat,
      missing: criteria.filter((c) => c.status === 'MISSING').length,
      status, title: op.title,
    };
  });

  const anyFail = unmappedFail > 0 || operations.some((o) => o.fail > 0);
  const enforcedMissing = operations.some((o) => o.gate === 'enforced' && o.missing > 0);
  const exitCode = anyFail ? 1 : enforcedMissing ? 3 : 0;
  const report = { commit, exit_code: exitCode, live_fail_unmapped: unmappedFail, operations };
  return {
    reportJson: `${JSON.stringify(sortKeys(report), null, 2)}\n`,
    indexMd: renderMd(report),
    lines: operations.map((o) => `${o.status} ${o.id} (${o.missing} missing, ${o.fail} failed)`),
    exitCode,
  };
};

// ---------------------------------------------------------------- INDEX.md (FR-DOD-11)

const cellText = (s) => String(s).replaceAll('|', '\\|');
const row = (cells) => `| ${cells.map(cellText).join(' | ')} |`;
const DASH = '–';

const latencyBudgetText = (b) => {
  if (!b) return DASH;
  const max = [b.p50_ms_max !== undefined ? `p50 ≤ ${b.p50_ms_max} ms` : null, b.p95_ms_max !== undefined ? `p95 ≤ ${b.p95_ms_max} ms` : null]
    .filter(Boolean).join(', ');
  return `${max}${b.per ? ` per ${b.per}` : ''}; n ≥ ${b.n_min}; ${b.kind}; ${b.source}`;
};
const costBudgetText = (b) => {
  if (!b) return DASH;
  const max = [b.usd_per_run_max !== undefined ? `≤ $${usdText(budgetMicro(b.usd_per_run_max))} per run` : null,
    b.tokens_per_run_max !== undefined ? `≤ ${b.tokens_per_run_max} tokens per run` : null].filter(Boolean).join(', ');
  return `${max}; n ≥ ${b.n_min}; ${b.kind}; ${b.source}`;
};

const renderMd = (report) => {
  const lines = [
    '# Definition of Done',
    '',
    `Commit ${report.commit}. Report exit code ${report.exit_code}. FAIL lines in live.log that no operation maps: ${report.live_fail_unmapped}.`,
    '',
    row(['Operation', 'Gate', 'Status', ...CRITERIA]),
    row(Array(3 + CRITERIA.length).fill('---')),
    ...report.operations.map((o) => row([`${o.id} — ${o.title}`, o.gate, o.status, ...o.criteria.map((c) => c.status)])),
    '',
    '## Cost and latency',
    '',
    row(['Operation', 'n', 'p50 ms', 'p95 ms', 'Latency budget', 'Runs', 'USD max per run', 'USD mean per run', 'Cache-read share', 'Cost budget']),
    row(Array(10).fill('---')),
    ...report.operations.map((o) => row([
      o.id, o.latency.n, o.latency.p50_ms ?? DASH, o.latency.p95_ms ?? DASH, latencyBudgetText(o.latency.budget),
      o.cost.runs, o.cost.per_run_max?.usd ?? DASH, o.cost.per_run_mean?.usd ?? DASH,
      o.cost.cache_read_share === null ? DASH : o.cost.cache_read_share.toFixed(4), costBudgetText(o.cost.budget),
    ])),
    '',
    '## Missing',
  ];
  const missing = report.operations.filter((o) => o.missing > 0);
  if (missing.length === 0) lines.push('', 'None.');
  for (const o of missing) {
    lines.push('', `### ${o.id}`, '');
    for (const c of o.criteria.filter((x) => x.status === 'MISSING')) {
      for (const gap of c.gaps) lines.push(`- ${o.id} × ${c.criterion} — ${gap}`);
    }
  }
  return `${lines.join('\n')}\n`;
};

// ---------------------------------------------------------------- CLI

const isMain = process.argv[1] && resolve(process.argv[1]) === resolve(import.meta.filename);
if (isMain) {
  const arg = (name) => {
    const i = process.argv.indexOf(name);
    return i > 0 ? process.argv[i + 1] : undefined;
  };
  try {
    const [indexPath, results, commit, out] = ['--index', '--results', '--commit', '--out'].map(arg);
    if (!indexPath || !results || !commit || !out) throw new InputError('report: usage: --index <yaml> --results <dir> --commit <sha> --out <dir>');
    const root = resolve(arg('--root') ?? resolve(import.meta.dirname, '../..'));
    let traces;
    if (arg('--traces')) traces = json('traces', readFileSync(arg('--traces'), 'utf8'));
    else {
      const { loadRepo } = await import('../lib/repo.mjs');
      traces = {};
      for (const t of loadRepo(root).tests) for (const id of t.ids) traces[id] = [...(traces[id] ?? []), t.file];
    }
    const read = (name) => (existsSync(join(results, name)) ? readFileSync(join(results, name), 'utf8') : null);
    const index = parseYaml(readFileSync(indexPath, 'utf8'));
    const report = buildReport({ index, traces, inputs: Object.fromEntries(INPUT_FILES.map((n) => [n, read(n)])), commit, root });
    mkdirSync(out, { recursive: true });
    writeFileSync(join(out, 'INDEX.md'), report.indexMd);
    writeFileSync(join(out, 'report.json'), report.reportJson);
    process.stdout.write(`${report.lines.join('\n')}\n`);
    process.exit(report.exitCode);
  } catch (e) {
    process.stderr.write(`${e instanceof InputError ? e.message : `report: ${e.message.split('\n')[0]}`}\n`);
    process.exit(2);
  }
}
