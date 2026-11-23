#!/usr/bin/env node
// trace-check: end-to-end traceability spec ↔ tests ↔ code ↔ ADRs (ADR-0001).
// Each rule is a function repo -> list of errors. All empty = green CI.
import { existsSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { parse as parseYaml } from 'yaml';
import {
  loadRepo, isImplFile, claimsOf, MODULE_ID, ITEM_ID, ADR_ID,
  SPEC_STATUSES, LOCKED_SPEC_STATUSES, ADR_STATUSES, TRACKS,
} from '../lib/repo.mjs';
import { INDEX_FILE, parseIndex, repoContext, t10Repo } from '../dod/index.mjs';

const TASK_STATUSES = ['todo', 'in_progress', 'blocked', 'done'];
const dodInfo = []; // T10 information lines (tracked operations), printed after the rules

const root = resolve(process.argv[2] ?? '.');
const repo = loadRepo(root);

const duplicates = (ids) => ids.filter((id, i) => ids.indexOf(id) !== i);
const adrIds = new Set(repo.adrs.map((a) => a.meta?.id).filter(Boolean));

const rules = {
  // T0: every spec and ADR front matter parses as YAML
  'T0 parses': ({ specs, adrs }) =>
    [...specs, ...adrs].filter((f) => f.parseError).map((f) => `${f.file}: does not parse: ${f.parseError}`),

  // T1: module spec is well-formed
  'T1 spec-shape': ({ specs }) =>
    specs.flatMap(({ file, doc, items }) => [
      ...(MODULE_ID.test(doc.id ?? '') ? [] : [`${file}: id "${doc.id}" does not match ${MODULE_ID}`]),
      ...(SPEC_STATUSES.includes(doc.status) ? [] : [`${file}: status "${doc.status}" is not one of ${SPEC_STATUSES.join('|')}`]),
      ...(doc.title ? [] : [`${file}: missing title`]),
      ...((doc.normative ?? []).length > 0 ? [] : [`${file}: no normative source`]),
      ...((doc.paths ?? []).length > 0 ? [] : [`${file}: no paths (globs of the files this module owns)`]),
      ...(TRACKS.includes(doc.track) ? [] : [`${file}: track must be one of ${TRACKS.join(' | ')}`]),
      ...items
        .filter((item) => !ITEM_ID.test(item.id ?? ''))
        .map((item) => `${file}: ${item.section}: id "${item.id}" does not match ${ITEM_ID}`),
      ...items
        .filter((item) => item.section === 'nfr')
        .filter((item) => item.threshold === undefined || !item.verified_by)
        .map((item) => `${file}: ${item.id}: NFR needs a numeric threshold and verified_by`),
    ]),

  // T2: IDs are unique across the repository
  'T2 unique-ids': ({ specs }) =>
    duplicates(specs.flatMap((s) => [s.doc.id, ...s.items.map((i) => i.id)]))
      .map((id) => `ID ${id} is declared more than once`),

  // T3: every test file has @trace
  'T3 test-has-trace': ({ tests }) =>
    tests.filter((t) => t.ids.length === 0)
      .map((t) => `${t.file}: test has no @trace tag`),

  // T4: @trace references only declared IDs
  'T4 trace-known': ({ tests, ownerOf }) =>
    tests.flatMap((t) => t.ids.filter((id) => !ownerOf.has(id))
      .map((id) => `${t.file}: @trace ${id} is not declared in any spec`)),

  // T5: in an approved spec every FR / INV / VEC is covered by a test
  'T5 approved-covered': ({ specs, tests }) => {
    const traced = new Set(tests.flatMap((t) => t.ids));
    return specs
      .filter((s) => LOCKED_SPEC_STATUSES.includes(s.doc.status))
      .flatMap((s) => s.items
        .filter((i) => ['requirements', 'invariants', 'vectors'].includes(i.section)
          || (i.section === 'nfr' && i.verified_by === 'test'))
        .filter((i) => !traced.has(i.id))
        .map((i) => `${s.file}: ${i.id} in a ${s.doc.status} spec is not covered by any test`));
  },

  // T6: ADRs are well-formed; accepted requires verified_by and revisit_when
  'T6 adr-shape': ({ adrs }) =>
    adrs.flatMap(({ file, meta }) => !meta ? [`${file}: missing YAML frontmatter`] : [
      ...(ADR_ID.test(meta.id ?? '') ? [] : [`${file}: id "${meta.id}" does not match ${ADR_ID}`]),
      ...(ADR_STATUSES.includes(meta.status) ? [] : [`${file}: status "${meta.status}" is not one of ${ADR_STATUSES.join('|')}`]),
      ...(meta.status === 'accepted' && !meta.verified_by ? [`${file}: accepted without verified_by`] : []),
      ...(meta.status === 'accepted' && !meta.revisit_when ? [`${file}: accepted without revisit_when`] : []),
    ]),

  // T7: specs reference only existing ADRs
  'T7 spec-adr-known': ({ specs }) =>
    specs.flatMap((s) => (s.doc.adr ?? []).filter((id) => !adrIds.has(id))
      .map((id) => `${s.file}: references ${id}, which does not exist`)),

  // T8: spec-first — every implementation file is owned by an approved/implemented spec
  'T8 impl-claimed': ({ files, specs }) =>
    files.filter(isImplFile).flatMap((file) => {
      const owners = claimsOf(specs, file);
      return owners.length === 0
        ? [`${file}: implementation file is not claimed by any spec's paths`]
        : owners.some((s) => LOCKED_SPEC_STATUSES.includes(s.doc.status))
          ? []
          : [`${file}: owned by ${owners.map((s) => s.doc.id).join(', ')}, none approved`];
    }),

  // T9: run state (ADR-0019) — docs/progress.yaml parses, task ids are unique, statuses are known,
  // every `needs` names a task, blocked tasks say why and what the owner must do
  'T9 progress-state': () => {
    const file = 'docs/progress.yaml';
    const path = join(root, file);
    if (!existsSync(path)) return [];
    let doc;
    try { doc = parseYaml(readFileSync(path, 'utf8')); } catch (e) { return [`${file}: does not parse: ${e.message.split('\n')[0]}`]; }
    const tasks = doc?.tasks ?? [];
    const ids = tasks.map((t) => t.id);
    const known = new Set(ids);
    return [
      ...duplicates(ids).map((id) => `${file}: duplicate task id ${id}`),
      ...tasks.flatMap((t) => [
        ...(TASK_STATUSES.includes(t.status) ? [] : [`${file}: ${t.id}: status "${t.status}" is not one of ${TASK_STATUSES.join('|')}`]),
        ...(t.needs ?? []).filter((n) => !known.has(n)).map((n) => `${file}: ${t.id}: needs unknown task ${n}`),
        ...(t.status === 'blocked' && !(t.reason && t.ask) ? [`${file}: ${t.id}: blocked without reason and ask`] : []),
      ]),
    ];
  },

  // T10: the Definition-of-Done index matches the specs, tests, live labels and registries (OPS-DOD FR-DOD-3)
  'T10 dod-index': (r) => {
    const path = join(root, INDEX_FILE);
    const status = r.specs.find((s) => s.doc.id === 'OPS-DOD')?.doc.status ?? 'draft';
    let index = null;
    if (existsSync(path)) {
      const parsed = parseIndex(readFileSync(path, 'utf8'));
      if (parsed.error) return [`${INDEX_FILE}: does not parse: ${parsed.error}`];
      index = parsed.index;
    }
    const { errors, info } = t10Repo(index, repoContext(root, r), status);
    dodInfo.push(...info);
    return errors.map((e) => `${INDEX_FILE}: ${e}`);
  },
};

const report = Object.entries(rules)
  .map(([name, rule]) => ({ name, errors: rule(repo) }));

const lines = report.flatMap(({ name, errors }) => [
  `${errors.length ? '✗' : '✓'} ${name}`,
  ...errors.map((e) => `    ${e}`),
]);
console.log(lines.join('\n'));
if (dodInfo.length) console.log(['\nT10 information (tracked operations):', ...dodInfo.map((l) => `    ${l}`)].join('\n'));

const total = report.reduce((n, r) => n + r.errors.length, 0);
console.log(`\nspecs: ${repo.specs.length}, tests: ${repo.tests.length}, ADRs: ${repo.adrs.length}, errors: ${total}`);
process.exit(total ? 1 : 0);
