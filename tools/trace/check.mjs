#!/usr/bin/env node
// trace-check: end-to-end traceability spec ↔ tests ↔ code ↔ ADRs (ADR-0001).
// Each rule is a function repo -> list of errors. All empty = green CI.
import { resolve } from 'node:path';
import {
  loadRepo, isImplFile, claimsOf, MODULE_ID, ITEM_ID, ADR_ID,
  SPEC_STATUSES, LOCKED_SPEC_STATUSES, ADR_STATUSES,
} from '../lib/repo.mjs';

const root = resolve(process.argv[2] ?? '.');
const repo = loadRepo(root);

const duplicates = (ids) => ids.filter((id, i) => ids.indexOf(id) !== i);
const adrIds = new Set(repo.adrs.map((a) => a.meta?.id).filter(Boolean));

const rules = {
  // T1: module spec is well-formed
  'T1 spec-shape': ({ specs }) =>
    specs.flatMap(({ file, doc, items }) => [
      ...(MODULE_ID.test(doc.id ?? '') ? [] : [`${file}: id "${doc.id}" does not match ${MODULE_ID}`]),
      ...(SPEC_STATUSES.includes(doc.status) ? [] : [`${file}: status "${doc.status}" is not one of ${SPEC_STATUSES.join('|')}`]),
      ...(doc.title ? [] : [`${file}: missing title`]),
      ...((doc.normative ?? []).length > 0 ? [] : [`${file}: no normative source`]),
      ...((doc.paths ?? []).length > 0 ? [] : [`${file}: no paths (globs of the files this module owns)`]),
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
};

const report = Object.entries(rules)
  .map(([name, rule]) => ({ name, errors: rule(repo) }));

const lines = report.flatMap(({ name, errors }) => [
  `${errors.length ? '✗' : '✓'} ${name}`,
  ...errors.map((e) => `    ${e}`),
]);
console.log(lines.join('\n'));

const total = report.reduce((n, r) => n + r.errors.length, 0);
console.log(`\nspecs: ${repo.specs.length}, tests: ${repo.tests.length}, ADRs: ${repo.adrs.length}, errors: ${total}`);
process.exit(total ? 1 : 0);
