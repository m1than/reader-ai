#!/usr/bin/env node
// Checks every trace id in summary.yaml (VEC-OBS-3) against SRV-OBS FR-OBS-5's derivation:
//   trace_id = first 16 bytes of sha256(UTF-8 "reader.job" + NUL + <job_id as decimal ASCII> + NUL + <key>), as hex;
//   an all-zero result is replaced by bytes 16..31.
// Every expected entry that carries `trace_id` or `trace_ids` also names its `job` (or `jobs`); the key comes from
// the seeded job_keys row with that job_id. Prints `job key-prefix trace_id` for every seeded row and exits 1 on a
// mismatch. Run: node spec/fixtures/SRV-OBS/trace-ids.mjs (needs tools/node_modules: npm ci --prefix tools).
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(join(here, '../../../tools/package.json'));
const { parse } = require('yaml');

export const deriveTraceId = (jobId, key) => {
  const h = createHash('sha256').update(Buffer.concat([
    Buffer.from('reader.job', 'utf8'), Buffer.from([0]), Buffer.from(String(jobId), 'ascii'), Buffer.from([0]),
    Buffer.from(key, 'utf8'),
  ])).digest();
  const first = h.subarray(0, 16);
  return (first.every((b) => b === 0) ? h.subarray(16, 32) : first).toString('hex');
};

const doc = parse(readFileSync(join(here, 'summary.yaml'), 'utf8'));
const keyOf = new Map(doc.seed.job_keys.map((r) => [r.job_id, r.key]));
for (const [job, key] of keyOf) console.log(job, key.slice(0, 24), deriveTraceId(job, key));

const errors = [];
const walk = (node, path) => {
  if (Array.isArray(node)) { node.forEach((n, i) => walk(n, `${path}[${i}]`)); return; }
  if (!node || typeof node !== 'object') return;
  const pairs = [];
  if ('trace_id' in node) pairs.push([node.job, node.trace_id]);
  if ('trace_ids' in node) (node.jobs ?? []).forEach((j, i) => pairs.push([j, node.trace_ids[i]]));
  if ('trace_ids' in node && (node.jobs ?? []).length !== node.trace_ids.length) errors.push(`${path}: jobs and trace_ids differ in length`);
  for (const [job, id] of pairs) {
    if (!keyOf.has(job)) errors.push(`${path}: job ${job} has no seeded job_keys row`);
    else if (deriveTraceId(job, keyOf.get(job)) !== id) errors.push(`${path}: job ${job}: ${id} != ${deriveTraceId(job, keyOf.get(job))}`);
  }
  for (const [k, v] of Object.entries(node)) walk(v, `${path}.${k}`);
};
walk(doc.expected, 'expected');
if (errors.length) { console.error(errors.join('\n')); process.exit(1); }
console.log('ok: every expected trace id matches FR-OBS-5');
