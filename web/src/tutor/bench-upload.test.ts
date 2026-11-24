// @trace FR-TUTOR-15 VEC-TUTOR-8 VEC-OBS-5
// SRV-TUTOR FR-TUTOR-15 (paired SRV-OBS phase 2): the bench's records file is exactly VEC-OBS-5
// `bench_results.tutor` (a header above the records) and its summary exactly VEC-TUTOR-8 `tutor_summary`; the
// committed 2026-11-23 summary validates against VEC-TUTOR-8.
import { describe, expect, test } from 'vitest';
import { readJson, readYaml } from '../app/testing/fixtures';
import * as bench from './bench';

type Shape = { required: Record<string, unknown>; optional?: Record<string, unknown> };
const vec5 = () => readYaml<{ bench_results: { tutor: Shape & { tutor_record: Record<string, unknown> } }; patterns: Record<string, string> }>(
  'spec/fixtures/SRV-OBS/phase2.yaml',
);
const vec8 = () => readYaml<{ tutor_summary: Shape }>('spec/fixtures/SRV-TUTOR/bench-summary.yaml').tutor_summary;

const record = (): bench.BenchRecord => ({
  id: 'v01', chapter: 1, question_id: null, state: 'answered', wall_s: 5, usd: 0.05, output_tokens: 300, thinking_tokens: 0,
  grounded: true, insufficient: false, blocks: 2, blocks_with_verified_quote: 2, gold_in_top8: true, gold_cited: true,
});
const HEADER = { date: '2026-11-25', branch: 'V/obs-mlflow-phase2-impl', commit: 'ffa821f', lesson_id: '20000000-0000-4000-8000-0000000000bb' };

describe('records file (FR-TUTOR-15, VEC-OBS-5 bench_results.tutor)', () => {
  test('a header above the records, exactly the declared keys', () => {
    const build = (bench as Record<string, unknown>).recordsFile as (h: typeof HEADER, r: bench.BenchRecord[]) => Record<string, unknown>;
    expect(build).toBeTypeOf('function');
    const file = build(HEADER, [record()]);
    expect(Object.keys(file).sort()).toEqual(Object.keys(vec5().bench_results.tutor.required).sort());
    expect(file.kind).toBe('tutor');
    expect(file.set).toBe('tutor-bench-v2');
    expect(String(file.commit)).toMatch(new RegExp(vec5().patterns.commit ?? '^$'));
    const rows = file.records as Record<string, unknown>[];
    expect(Object.keys(rows[0] ?? {}).sort()).toEqual(Object.keys(vec5().bench_results.tutor.tutor_record).sort());
  });

  test('a commit that is not a bare sha is refused', () => {
    const build = (bench as Record<string, unknown>).recordsFile as (h: typeof HEADER, r: bench.BenchRecord[]) => unknown;
    expect(() => build({ ...HEADER, commit: 'ffa821f fix: something' }, [record()])).toThrow();
  });
});

describe('summary (FR-TUTOR-15, VEC-TUTOR-8)', () => {
  const allowed = () => new Set([...Object.keys(vec8().required), ...Object.keys(vec8().optional ?? {})]);

  test('the built summary has every required key and nothing undeclared', () => {
    const build = (bench as Record<string, unknown>).summaryFile as (h: typeof HEADER, r: bench.BenchRecord[], ids: string[]) => Record<string, unknown>;
    expect(build).toBeTypeOf('function');
    const s = build(HEADER, [record()], ['v01']);
    for (const k of Object.keys(vec8().required)) expect(s).toHaveProperty(k);
    for (const k of Object.keys(s)) expect(allowed().has(k)).toBe(true);
  });

  test('the committed 2026-11-23 summary is migrated to the numbers-only shape', () => {
    const committed = readJson<Record<string, unknown>>('e2e/live/results/2026-11-23-tutor-bench.json');
    for (const k of Object.keys(vec8().required)) expect(committed).toHaveProperty(k);
    for (const k of Object.keys(committed)) expect(allowed().has(k)).toBe(true);
    expect(String(committed.commit)).toMatch(/^[0-9a-f]{7,40}$/);
  });
});
