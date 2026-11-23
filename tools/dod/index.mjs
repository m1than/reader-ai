// OPS-DOD: the Definition-of-Done index as data, and pure functions over it.
//   validateIndex  FR-DOD-1/2  the index's shape and where each kind of evidence may stand
//   t10            FR-DOD-3    the index against the repository (specs, tests, live labels, registries)
//   waiverViolations FR-DOD-12 waived cells gained or changed (the write hook's rule H4)
//   ratchet, p8    FR-DOD-13   the Definition of Done is not loosened between --base and HEAD
// The repository is read once into a plain context (repoContext), so every rule is a function of data.
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { parse as parseYaml } from 'yaml';
import { ITEM_ID, MODULE_ID } from '../lib/repo.mjs';

export const INDEX_FILE = 'spec/fixtures/OPS-DOD/operations.yaml';
export const CRITERIA = ['functional', 'security', 'privacy', 'a11y', 'latency', 'cost', 'observability', 'recovery'];
const SURFACES = ['web', 'api', 'cli'];
const GATES = ['tracked', 'enforced'];
const OP_ID = /^[a-z]+\.[a-z_]+$/;
const ROUTE = /^(GET|POST|PUT|PATCH|DELETE|WS) \/\S*$/;
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const CELL_KEYS = ['items', 'live', 'budget', 'pages', 'na', 'waived', 'missing'];
const BUDGET_KEYS = {
  latency: ['p95_ms_max', 'p50_ms_max', 'per', 'n_min', 'kind', 'source'],
  cost: ['usd_per_run_max', 'tokens_per_run_max', 'n_min', 'kind', 'source'],
};

// Where each live key's labels come from (FR-DOD-3). `pattern` captures the label from the script's text.
export const LIVE_SOURCES = {
  'stage-1': [
    { file: 'e2e/live/stage-1.sh', pattern: /\bcheck "([^"]+)"/g },
    { file: 'e2e/live/lib/passkey_check.py', pattern: /\breport\("([^"]+)"/g },
  ],
  browser: [
    // literal `browser: <label>` strings; f-strings holding `{` are format strings, not labels
    { file: 'e2e/live/lib/web_live.py', pattern: /"browser: ([^"{]+)"/g },
    { file: 'e2e/web/live/slice.live.ts', pattern: /^test\('([^']+)'/gm },
  ],
};

// Labels whose section does not run (FR-DOD-3): the stage-1 backup-restore drill under `if ((DRILL))`.
export const DISABLED_LABELS = [
  'reader backup',
  'backup files are 0600',
  'fresh stack (volumes recreated)',
  'reader restore onto the fresh stack',
  'stack healthy after restore',
  "restored database holds this run's live-check passkey",
].map((label) => ({ label: `stage-1: ${label}`, reason: 'S1.OPS-CLI-restore-seed' }));

const byCode = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
const sortUnique = (list) => [...new Set(list)].sort(byCode);
const isMap = (x) => x !== null && typeof x === 'object' && !Array.isArray(x);
const isText = (x) => typeof x === 'string' && x.length > 0;
const isPosInt = (x) => Number.isInteger(x) && x > 0;
const isPosNum = (x) => typeof x === 'number' && Number.isFinite(x) && x > 0;
const sameKeys = (obj, keys) => isMap(obj) && Object.keys(obj).length === keys.length && keys.every((k) => k in obj);
const opName = (op, i) => (isMap(op) && isText(op.id) ? op.id : `operations[${i}]`);
const cellName = (op, i, c) => `${opName(op, i)} × ${c}`;
const ops = (index) => (isMap(index) && Array.isArray(index.operations) ? index.operations : []);
const cells = (index) => ops(index).flatMap((op, i) =>
  CRITERIA.filter((c) => isMap(op?.criteria?.[c])).map((c) => ({ op, i, c, cell: op.criteria[c], at: cellName(op, i, c) })));
const listOf = (value) => (Array.isArray(value) ? value : []);

export const parseIndex = (text) => {
  try { return { index: parseYaml(text), error: null }; } catch (e) { return { index: null, error: e.message.split('\n')[0] }; }
};

// ---------------------------------------------------------------- FR-DOD-1/2

const checkList = (at, key, value, each) => {
  if (!Array.isArray(value) || value.length === 0) return [`${at}: ${key} must be a non-empty list`];
  return value.flatMap((v) => (isText(v) ? each?.(v) ?? [] : [`${at}: ${key} must hold non-empty strings`]));
};

const checkBudget = (at, criterion, budget) => {
  if (!isMap(budget)) return [`${at}: budget must be a mapping`];
  const allowed = BUDGET_KEYS[criterion];
  const errors = Object.keys(budget).filter((k) => !allowed.includes(k)).map((k) => `${at}: budget: unknown key "${k}"`);
  const maxima = criterion === 'latency' ? ['p95_ms_max', 'p50_ms_max'] : ['usd_per_run_max', 'tokens_per_run_max'];
  const present = maxima.filter((k) => k in budget);
  if (present.length === 0) errors.push(`${at}: budget needs ${maxima.join(' or ')}`);
  for (const k of present) {
    const ok = criterion === 'latency' || k === 'tokens_per_run_max' ? isPosInt(budget[k]) : isPosNum(budget[k]);
    if (!ok) errors.push(`${at}: budget ${k} must be a positive ${criterion === 'latency' || k === 'tokens_per_run_max' ? 'integer' : 'number'}`);
  }
  if ('per' in budget && budget.per !== 'page') errors.push(`${at}: budget per must be page`);
  if (!isPosInt(budget.n_min)) errors.push(`${at}: budget n_min must be a positive integer`);
  if (!['target', 'baseline'].includes(budget.kind)) errors.push(`${at}: budget kind must be target or baseline`);
  if (!isText(budget.source)) errors.push(`${at}: budget source must be a non-empty string`);
  return errors;
};

const checkCell = (op, at, c, cell) => {
  const keys = Object.keys(cell);
  const errors = keys.filter((k) => !CELL_KEYS.includes(k)).map((k) => `${at}: unknown key "${k}"`);
  const known = keys.filter((k) => CELL_KEYS.includes(k));
  if (keys.length === 0) return [`${at}: empty entry`];
  for (const solo of ['na', 'waived']) {
    if (known.includes(solo) && known.length > 1) errors.push(`${at}: ${solo} excludes every other key`);
  }
  if ('items' in cell) errors.push(...checkList(at, 'items', cell.items, (id) => (ITEM_ID.test(id) ? [] : [`${at}: item "${id}" is not an item id`])));
  if ('live' in cell) errors.push(...checkList(at, 'live', cell.live));
  if ('pages' in cell) {
    errors.push(...checkList(at, 'pages', cell.pages, (p) => (p.startsWith('/') ? [] : [`${at}: page "${p}" must start with /`])));
    if (c !== 'a11y') errors.push(`${at}: pages only on a11y`);
  }
  for (const k of ['na', 'missing']) if (k in cell && !isText(cell[k])) errors.push(`${at}: ${k} must be a non-empty string`);
  if ('waived' in cell) {
    const w = cell.waived;
    if (!(sameKeys(w, ['reason', 'by', 'date']) && isText(w.reason) && w.by === 'owner' && DATE.test(String(w.date)))) {
      errors.push(`${at}: waived needs reason, by: owner and a date YYYY-MM-DD`);
    }
  }
  if ('budget' in cell) {
    if (c === 'latency' || c === 'cost') errors.push(...checkBudget(at, c, cell.budget));
    else errors.push(`${at}: budget only on latency or cost`);
  }
  // FR-DOD-2 placement
  const web = listOf(op.surface).includes('web');
  if ('na' in cell && !((c === 'a11y' && !web) || c === 'cost')) errors.push(`${at}: na is not allowed here`);
  const open = !('na' in cell) && !('waived' in cell);
  if (open && (c === 'latency' || c === 'cost') && !('budget' in cell) && !('missing' in cell)) {
    errors.push(`${at}: needs a budget or missing`);
  }
  if (open && c === 'a11y' && web && !('pages' in cell) && !('missing' in cell)) errors.push(`${at}: needs pages or missing`);
  return errors;
};

const OP_KEYS = ['id', 'title', 'surface', 'specs', 'routes', 'commands', 'gate', 'criteria'];

const checkOperation = (op, i, seen) => {
  if (!isMap(op)) return [`operations[${i}]: must be a mapping`];
  const at = opName(op, i);
  const errors = Object.keys(op).filter((k) => !OP_KEYS.includes(k)).map((k) => `${at}: unknown key "${k}"`);
  if (!isText(op.id) || !OP_ID.test(op.id)) errors.push(`operations[${i}]: id "${op.id}" does not match ${OP_ID.source}`);
  else if (seen.has(op.id)) errors.push(`${at}: duplicate operation id`);
  seen.add(op.id);
  if (!isText(op.title)) errors.push(`${at}: title must be a non-empty string`);
  errors.push(...checkList(at, 'surface', op.surface, (s) => (SURFACES.includes(s) ? [] : [`${at}: surface "${s}" is not one of ${SURFACES.join(', ')}`])));
  if ('specs' in op) errors.push(...checkList(at, 'specs', op.specs));
  if ('routes' in op) errors.push(...checkList(at, 'routes', op.routes, (r) => (ROUTE.test(r) ? [] : [`${at}: route "${r}" must be "<METHOD|WS> <path>"`])));
  if ('commands' in op) errors.push(...checkList(at, 'commands', op.commands));
  if (!GATES.includes(op.gate)) errors.push(`${at}: gate must be tracked or enforced`);
  if (!sameKeys(op.criteria, CRITERIA)) {
    errors.push(`${at}: criteria must hold exactly ${CRITERIA.join(', ')}`);
  }
  for (const c of CRITERIA) {
    const cell = op.criteria?.[c];
    if (cell === undefined) continue;
    if (!isMap(cell)) errors.push(`${cellName(op, i, c)}: must be a mapping`);
    else errors.push(...checkCell(op, cellName(op, i, c), c, cell));
  }
  return errors;
};

export const validateIndex = (index) => {
  if (!isMap(index)) return ['index: must be a mapping'];
  const errors = Object.keys(index).filter((k) => !['version', 'criteria', 'not_user_facing', 'operations'].includes(k))
    .map((k) => `index: unknown key "${k}"`);
  if (index.version !== 1) errors.push('index: version must be 1');
  if (!sameKeys(index.criteria, CRITERIA) || !CRITERIA.every((c) => isText(index.criteria[c]))) {
    errors.push(`criteria: keys must be exactly ${CRITERIA.join(', ')}`);
  }
  if ('not_user_facing' in index) {
    if (!Array.isArray(index.not_user_facing) || index.not_user_facing.length === 0) {
      errors.push('not_user_facing: must be a non-empty list');
    } else {
      index.not_user_facing.forEach((e, i) => {
        const kinds = ['spec', 'route', 'command'].filter((k) => isMap(e) && k in e);
        if (!(isMap(e) && kinds.length === 1 && Object.keys(e).length === 2 && isText(e[kinds[0]]) && isText(e.reason))) {
          errors.push(`not_user_facing[${i}]: needs exactly one of spec, route, command and a reason`);
        }
      });
    }
  }
  if (!Array.isArray(index.operations) || index.operations.length === 0) errors.push('index: operations must be a non-empty list');
  const seen = new Set();
  ops(index).forEach((op, i) => errors.push(...checkOperation(op, i, seen)));
  return sortUnique(errors);
};

// ---------------------------------------------------------------- the repository as data

const readText = (root, rel) => (existsSync(join(root, rel)) ? readFileSync(join(root, rel), 'utf8') : '');

export const liveLabels = (root) => Object.fromEntries(Object.entries(LIVE_SOURCES).map(([key, sources]) =>
  [key, sortUnique(sources.flatMap(({ file, pattern }) => [...readText(root, file).matchAll(pattern)].map((m) => m[1])))]));

// Route registry: `Route("M", "path", …)` and `WsRoute("path", …)` in the modules ROUTERS names (FR-DOD-3).
export const routeRegistry = (root) => {
  const app = readText(root, 'server/src/reader/api/app.py');
  const block = app.match(/ROUTERS[^=]*=\s*\(([\s\S]*?)\n\)/)?.[1] ?? '';
  const modules = [...block.matchAll(/"(reader(?:\.\w+)+)"/g)].map((m) => m[1]);
  return sortUnique(modules.flatMap((mod) => {
    const src = readText(root, `server/src/${mod.replaceAll('.', '/')}.py`);
    return [
      ...[...src.matchAll(/\bRoute\(\s*"(\w+)",\s*"([^"]+)"/g)].map((m) => `${m[1]} ${m[2]}`),
      ...[...src.matchAll(/\bWsRoute\(\s*"([^"]+)"/g)].map((m) => `WS ${m[1]}`),
    ];
  }));
};

// CLI registry (FR-DOD-3): (a) `reader` leaf subcommands of the add_parser tree, with the options each declares;
// (b) every other [project.scripts] name; (c) `<name> <flag>` for each store_true flag on its top-level parser.
const argparseTree = (src) => {
  const pathOf = new Map(); // parser variable -> command path
  const parentOfSub = new Map(); // subparsers variable -> parser variable
  const hasSubs = new Set();
  const leaves = [];
  const flags = new Map(); // parser variable -> options
  const root = src.match(/(\w+)\s*=\s*argparse\.ArgumentParser\(\s*\n?\s*prog=/)?.[1]
    ?? src.match(/(\w+)\s*=\s*argparse\.ArgumentParser\((?![^)]*add_help=False)/)?.[1];
  if (root) pathOf.set(root, []);
  for (const line of src.split('\n')) {
    let m = line.match(/(\w+)\s*=\s*(\w+)\.add_subparsers\(/);
    if (m) { parentOfSub.set(m[1], m[2]); hasSubs.add(m[2]); continue; }
    m = line.match(/(?:(\w+)\s*=\s*)?(\w+)\.add_parser\(\s*"([^"]+)"/);
    if (m && parentOfSub.has(m[2])) {
      const path = [...(pathOf.get(parentOfSub.get(m[2])) ?? []), m[3]];
      const name = m[1] ?? `#${leaves.length}:${path.join(' ')}`;
      pathOf.set(name, path);
      leaves.push(name);
      continue;
    }
    m = line.match(/(\w+)\.add_argument\(\s*"(--[\w-]+)"/);
    if (m) flags.set(m[1], [...(flags.get(m[1]) ?? []), m[2]]);
  }
  return { root, pathOf, flags, leaves: leaves.filter((v) => !hasSubs.has(v)) };
};

export const cliRegistry = (root) => {
  const entries = [];
  const flags = {};
  const tree = argparseTree(readText(root, 'server/src/reader/cli/app.py'));
  for (const v of tree.leaves) {
    const entry = ['reader', ...tree.pathOf.get(v)].join(' ');
    entries.push(entry);
    flags[entry] = sortUnique(tree.flags.get(v) ?? []);
  }
  const scripts = readText(root, 'server/pyproject.toml').match(/\[project\.scripts\]\n([\s\S]*?)(\n\[|$)/)?.[1] ?? '';
  for (const [, name, mod] of scripts.matchAll(/^([\w-]+)\s*=\s*"([\w.]+):\w+"/gm)) {
    if (name === 'reader') continue;
    entries.push(name);
    const src = readText(root, `server/src/${mod.replaceAll('.', '/')}.py`);
    const top = src.match(/(\w+)\s*=\s*argparse\.ArgumentParser\(/)?.[1];
    if (!top) continue;
    const re = new RegExp(`\\b${top}\\.add_argument\\(\\s*"(--[\\w-]+)"[^)]*action="store_true"`, 'g');
    for (const m of src.matchAll(re)) entries.push(`${name} ${m[1]}`);
  }
  return { entries: sortUnique(entries), flags };
};

export const repoContext = (root, repo) => ({
  specs: Object.fromEntries(repo.specs.filter((s) => MODULE_ID.test(s.doc.id ?? ''))
    .map((s) => [s.doc.id, { status: s.doc.status, items: s.items.map((i) => i.id) }])),
  traced: sortUnique(repo.tests.flatMap((t) => t.ids)),
  labels: liveLabels(root),
  disabled: DISABLED_LABELS,
  routes: routeRegistry(root),
  cli: cliRegistry(root),
});

// ---------------------------------------------------------------- FR-DOD-3: T10

export const t10 = (index, ctx) => {
  const shape = validateIndex(index);
  const errors = [...shape];
  const info = [];
  if (!isMap(index)) return { errors: sortUnique(errors), info };
  const ownerOf = new Map(Object.entries(ctx.specs).flatMap(([id, s]) => s.items.map((item) => [item, id])));
  const traced = new Set(ctx.traced);
  const disabled = new Map(ctx.disabled.map((d) => [d.label, d.reason]));
  const emitted = (full) => {
    const at = full.indexOf(': ');
    const key = at < 0 ? full : full.slice(0, at);
    if (!(key in ctx.labels)) return 'unknown';
    return ctx.labels[key].includes(full.slice(at + 2)) ? 'yes' : 'no';
  };
  const registry = new Set(ctx.routes);
  const entries = new Set(ctx.cli.entries);
  const commandKnown = (cmd) => {
    if (entries.has(cmd)) return true;
    const at = cmd.lastIndexOf(' --');
    if (at < 0) return false;
    const entry = cmd.slice(0, at);
    return entries.has(entry) && (ctx.cli.flags[entry] ?? []).includes(cmd.slice(at + 1));
  };
  const nuf = listOf(index.not_user_facing).filter(isMap);
  const named = { spec: new Set(), route: new Set(), command: [] };

  for (const e of nuf) {
    if ('spec' in e) {
      named.spec.add(e.spec);
      if (!(e.spec in ctx.specs)) errors.push(`not_user_facing: spec ${e.spec} is not a module spec`);
    }
    if ('route' in e) {
      named.route.add(e.route);
      if (!registry.has(e.route)) errors.push(`not_user_facing: route "${e.route}" is not in the registry`);
    }
    if ('command' in e) {
      named.command.push(e.command);
      if (!ctx.cli.entries.some((x) => x === e.command || x.startsWith(`${e.command} `))) {
        errors.push(`not_user_facing: command "${e.command}" covers no registry entry`);
      }
    }
  }

  const opCommands = new Set();
  ops(index).forEach((op, i) => {
    if (!isMap(op)) return;
    const at = opName(op, i);
    const specs = listOf(op.specs);
    for (const s of specs) {
      named.spec.add(s);
      if (!(s in ctx.specs)) errors.push(`${at}: spec ${s} is not a module spec`);
    }
    const enforced = op.gate === 'enforced';
    if (enforced && specs.length === 0) errors.push(`${at}: enforced with no specs`);
    for (const r of listOf(op.routes)) {
      named.route.add(r);
      if (ROUTE.test(r) && !registry.has(r)) errors.push(`${at}: route "${r}" is not in the registry`);
    }
    for (const cmd of listOf(op.commands)) {
      opCommands.add(cmd);
      if (!commandKnown(cmd)) errors.push(`${at}: command "${cmd}" is not in the CLI registry`);
    }
    let missing = 0;
    const untraced = [];
    for (const c of CRITERIA) {
      const cell = op.criteria?.[c];
      if (!isMap(cell)) continue;
      const where = cellName(op, i, c);
      if ('missing' in cell) {
        missing += 1;
        if (enforced) errors.push(`${where}: missing on an enforced operation`);
      }
      for (const id of listOf(cell.items)) {
        if (!ITEM_ID.test(id)) continue;
        const owner = ownerOf.get(id);
        if (!owner) { errors.push(`${where}: item ${id} is not declared`); continue; }
        if (!specs.includes(owner)) errors.push(`${where}: item ${id} belongs to ${owner}, which the operation does not name`);
        if (!traced.has(id)) untraced.push(id);
        if (enforced && ctx.specs[owner].status === 'draft') errors.push(`${where}: item ${id} belongs to draft spec ${owner}`);
        if (enforced && !traced.has(id)) errors.push(`${where}: item ${id} is traced by no test`);
      }
      for (const label of listOf(cell.live)) {
        if (!isText(label)) continue;
        const state = emitted(label);
        if (state === 'unknown') errors.push(`${where}: live label "${label}" has an unknown key`);
        else if (state === 'no') errors.push(`${where}: live label "${label}" is not emitted by its scripts`);
        if (enforced && disabled.has(label)) errors.push(`${where}: live label "${label}" is disabled (${disabled.get(label)})`);
      }
    }
    if (!enforced && isText(op.id)) {
      info.push(`${op.id}: ${missing} missing; untraced: ${untraced.length ? sortUnique(untraced).join(', ') : 'none'}`);
    }
  });

  for (const r of ctx.routes) {
    if (!named.route.has(r)) errors.push(`route "${r}" is in no operation and not in not_user_facing`);
  }
  for (const e of ctx.cli.entries) {
    const covered = opCommands.has(e) || named.command.some((c) => e === c || e.startsWith(`${c} `));
    if (!covered) errors.push(`command "${e}" is in no operation and not in not_user_facing`);
  }
  for (const [id, s] of Object.entries(ctx.specs)) {
    if (s.status !== 'draft' && !named.spec.has(id)) {
      errors.push(`spec ${id} (${s.status}) is named by no operation and not in not_user_facing`);
    }
  }
  for (const { label } of ctx.disabled) {
    if (emitted(label) !== 'yes') errors.push(`disabled label "${label}" is not emitted by its scripts`);
  }
  return { errors: sortUnique(errors), info: sortUnique(info) };
};

// T10 as trace-check runs it: the index may be absent while OPS-DOD is draft.
export const t10Repo = (index, ctx, status) => {
  if (index === null || index === undefined) {
    return status === 'draft'
      ? { errors: [], info: [`${INDEX_FILE} absent: T10 skipped (OPS-DOD is draft)`] }
      : { errors: [`${INDEX_FILE} is absent and OPS-DOD is ${status}`], info: [] };
  }
  return t10(index, ctx);
};

// ---------------------------------------------------------------- FR-DOD-12: waivers

export const waivedCells = (index) => new Map(cells(index)
  .filter(({ cell }) => 'waived' in cell)
  .map(({ op, i, c, cell }) => [cellName(op, i, c), JSON.stringify(cell.waived)]));

export const waiverViolations = (before, after) => {
  const old = waivedCells(before);
  return sortUnique([...waivedCells(after)].flatMap(([at, value]) =>
    !old.has(at) ? [`${at}: waiver added`] : old.get(at) !== value ? [`${at}: waiver changed`] : []));
};

// The text a Write, Edit or MultiEdit leaves behind (the hook sees only the tool input).
export const nextText = (current, input) => {
  if (typeof input.content === 'string') return input.content;
  const edits = input.edits ?? [{ old_string: input.old_string, new_string: input.new_string, replace_all: input.replace_all }];
  return edits.reduce((text, e) => (typeof e.old_string !== 'string' ? text
    : e.replace_all ? text.split(e.old_string).join(e.new_string ?? '') : text.replace(e.old_string, () => e.new_string ?? '')), current);
};

// ---------------------------------------------------------------- FR-DOD-13: the P8 ratchet

const nufKey = (e) => ['spec', 'route', 'command'].filter((k) => k in e).map((k) => `${k} "${e[k]}"`).join(' ');

export const ratchet = (base, head) => {
  if (!isMap(base)) return [];
  if (!isMap(head)) return ['index removed'];
  const errors = [];
  const headOps = new Map(ops(head).filter(isMap).map((op) => [op.id, op]));
  for (const b of ops(base).filter(isMap)) {
    const h = headOps.get(b.id);
    if (!h) { errors.push(`${b.id}: operation removed`); continue; }
    const wasEnforced = b.gate === 'enforced';
    if (wasEnforced && h.gate !== 'enforced') errors.push(`${b.id}: enforced → ${h.gate}`);
    for (const c of CRITERIA) {
      const bc = isMap(b.criteria?.[c]) ? b.criteria[c] : {};
      const hc = isMap(h.criteria?.[c]) ? h.criteria[c] : {};
      const at = `${b.id} × ${c}`;
      if (wasEnforced) {
        for (const id of listOf(bc.items)) if (!listOf(hc.items).includes(id)) errors.push(`${at}: item ${id} removed`);
        for (const l of listOf(bc.live)) if (!listOf(hc.live).includes(l)) errors.push(`${at}: live label "${l}" removed`);
      }
      if ((wasEnforced || h.gate === 'enforced') && !('na' in bc) && 'na' in hc) {
        errors.push(`${at}: becomes na on an enforced operation`);
      }
      if (isMap(bc.budget)) {
        if (!isMap(hc.budget)) { errors.push(`${at}: budget removed`); continue; }
        for (const k of ['p95_ms_max', 'p50_ms_max', 'usd_per_run_max', 'tokens_per_run_max']) {
          if (!(k in bc.budget)) continue;
          const to = hc.budget[k];
          if (to === undefined || to > bc.budget[k]) errors.push(`${at}: budget ${k} loosened (${bc.budget[k]} → ${to ?? 'none'})`);
        }
        if (hc.budget.n_min < bc.budget.n_min) errors.push(`${at}: budget n_min loosened (${bc.budget.n_min} → ${hc.budget.n_min})`);
        if (bc.budget.kind === 'target' && hc.budget.kind === 'baseline') errors.push(`${at}: budget kind loosened (target → baseline)`);
      }
    }
  }
  const baseNuf = new Set(listOf(base.not_user_facing).filter(isMap).map(nufKey));
  for (const e of listOf(head.not_user_facing).filter(isMap)) {
    if (!baseNuf.has(nufKey(e))) errors.push(`not_user_facing: new entry ${nufKey(e)}`);
  }
  errors.push(...waiverViolations(base, head));
  return sortUnique(errors);
};

export const OWNER_TRAILER = /^DoD-Owner-Change:\s*\S+/m;

// P8 over the index texts at --base and HEAD and the commit messages between them.
export const p8 = (baseText, headText, messages) => {
  const base = baseText === null ? null : parseIndex(baseText).index;
  const head = headText === null ? null : parseIndex(headText).index;
  const errors = ratchet(base, head);
  return { errors, ok: errors.length === 0 || messages.some((m) => OWNER_TRAILER.test(m)) };
};
