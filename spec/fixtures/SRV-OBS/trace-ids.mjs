#!/usr/bin/env node
// Fixture helper for VEC-OBS-3 (summary.yaml). It is NOT the gate: SRV-OBS's [unit] test recomputes the same keys and
// ids in Python with reader.jobs.model.idempotency_key and FR-OBS-5's derivation, and that test fails CI. This script
// exists so the fixture's keys and ids are generated, never hand-written.
//
// For every seeded job_keys row:
//   key = SRV-EVENTS FR-EVENTS-2 = `<kind>:` + sha256 hex of canonical JSON of the kind's key fields taken from args,
//         canonical JSON = Python json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False) encoded UTF-8
//         (server/src/reader/jobs/model.py). Ported for strings, integers, booleans and null only; a float or a nested
//         value in a key field is refused here, because JS and Python format them differently.
//   trace_id = FR-OBS-5: first 16 bytes of sha256("reader.job" NUL <job_id as decimal ASCII> NUL <key UTF-8>), hex;
//         an all-zero result is replaced by bytes 16..31.
// Checks: each row's `key` equals the computed key; each expected entry's `trace_id`/`trace_ids` equals the id
// derived from the row its `job`/`jobs` names. Prints `job kind key trace_id` per row; exits 1 on any mismatch.
// Run: node spec/fixtures/SRV-OBS/trace-ids.mjs (needs tools/node_modules: npm ci --prefix tools).
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(join(here, '../../../tools/package.json'));
const { parse } = require('yaml');

// JobKind.key per kind, copied from the kind modules (pipeline/jobs.py KEY, tutor/jobs.py, ingest/jobs.py,
// search/jobs.py). The [unit] test reads them from the catalog instead.
export const KIND_KEYS = {
  'pipeline.stage': ['run_id', 'step', 'stage', 'input_sha256'],
  'pipeline.verify': ['run_id', 'step', 'stage', 'input_sha256'],
  'tutor.answer': ['question_id'],
  'ingest.import': ['material_sha256', 'converter_version'],
  'search.embed': ['ingest_id', 'model', 'params_sha256'],
};

const canonical = (value) => {
  if (value === null || typeof value === 'boolean' || typeof value === 'string') return JSON.stringify(value);
  if (Number.isInteger(value)) return String(value);
  if (typeof value === 'object' && !Array.isArray(value)) {
    return `{${Object.keys(value).sort().map((k) => `${JSON.stringify(k)}:${canonical(value[k])}`).join(',')}}`;
  }
  throw new Error(`canonical: unsupported value ${JSON.stringify(value)}`);
};

export const idempotencyKey = (kind, args) => {
  const fields = Object.fromEntries(KIND_KEYS[kind].map((f) => {
    if (!(f in args)) throw new Error(`${kind}: args lack key field ${f}`);
    const v = args[f];
    if (v !== null && typeof v === 'object') throw new Error(`${kind}.${f}: nested values are not ported`);
    return [f, v];
  }));
  return `${kind}:${createHash('sha256').update(Buffer.from(canonical(fields), 'utf8')).digest('hex')}`;
};

export const deriveTraceId = (jobId, key) => {
  const h = createHash('sha256').update(Buffer.concat([
    Buffer.from('reader.job', 'utf8'), Buffer.from([0]), Buffer.from(String(jobId), 'ascii'), Buffer.from([0]),
    Buffer.from(key, 'utf8'),
  ])).digest();
  const first = h.subarray(0, 16);
  return (first.every((b) => b === 0) ? h.subarray(16, 32) : first).toString('hex');
};

const doc = parse(readFileSync(join(here, 'summary.yaml'), 'utf8'));
const errors = [];
const keyOf = new Map();
for (const row of doc.seed.job_keys) {
  const key = idempotencyKey(row.kind, row.args);
  if (row.key !== key) errors.push(`job ${row.job_id}: key ${row.key} != ${key}`);
  keyOf.set(row.job_id, key);
  console.log(row.job_id, row.kind, key, deriveTraceId(row.job_id, key));
}

const walk = (node, path) => {
  if (Array.isArray(node)) { node.forEach((n, i) => walk(n, `${path}[${i}]`)); return; }
  if (!node || typeof node !== 'object') return;
  const pairs = [];
  if ('trace_id' in node) pairs.push([node.job, node.trace_id]);
  if ('trace_ids' in node) {
    if ((node.jobs ?? []).length !== node.trace_ids.length) errors.push(`${path}: jobs and trace_ids differ in length`);
    (node.jobs ?? []).forEach((j, i) => pairs.push([j, node.trace_ids[i]]));
  }
  for (const [job, id] of pairs) {
    if (!keyOf.has(job)) errors.push(`${path}: job ${job} has no seeded job_keys row`);
    else if (deriveTraceId(job, keyOf.get(job)) !== id) errors.push(`${path}: job ${job}: ${id} != ${deriveTraceId(job, keyOf.get(job))}`);
  }
  for (const [k, v] of Object.entries(node)) walk(v, `${path}.${k}`);
};
walk(doc.expected, 'expected');
if (errors.length) { console.error(errors.join('\n')); process.exit(1); }
console.log('ok: every key matches FR-EVENTS-2 and every expected trace id matches FR-OBS-5');
