// @trace VEC-TUTOR-7 NFR-TUTOR-1 NFR-TUTOR-2 NFR-TUTOR-3
import { describe, expect, test } from 'vitest';
import { readYaml } from '../app/testing/fixtures';
import { BenchError, benchSet, GATE_N, mergeRuns, p95, summary, THRESHOLDS, type BenchQuestion, type BenchRecord } from './bench';

type Bench = { set: { source: string; replace: Record<string, string> }; fixed: { source: string; id: string }[]; gate_n: number };
type Spec = { nfr: { id: string; threshold: number }[] };
const bench = () => readYaml<Bench>('spec/fixtures/SRV-TUTOR/bench.yaml');
const nfr = (id: string) => readYaml<Spec>('spec/modules/SRV-TUTOR.yaml').nfr.find((n) => n.id === id)!.threshold;
const v2 = () => readYaml<{ questions: BenchQuestion[] }>('spikes/embeddings-local/questions-v2.yaml').questions;
const fixed = () => readYaml<{ questions: BenchQuestion[] }>('spec/fixtures/SRV-TUTOR/bench-fixed.yaml').questions;
const ids = () => benchSet(v2(), fixed()).map((q) => q.id);

const record = (over: Partial<BenchRecord> = {}): BenchRecord => ({
  id: 'v01', chapter: 1, question_id: null, state: 'answered', wall_s: 5, usd: 0.05, output_tokens: 300, thinking_tokens: 0,
  grounded: true, insufficient: false, blocks: 2, blocks_with_verified_quote: 2, ...over,
});
const forSet = (over: (i: number) => Partial<BenchRecord> = () => ({})) =>
  benchSet(v2(), fixed()).map((q, i) => record({ id: q.id, chapter: q.chapter, ...over(i) }));

describe('bench set (VEC-TUTOR-7)', () => {
  test('is questions-v2 with v03 replaced by t01: 40 independent questions over all 23 chapters', () => {
    const b = bench();
    expect(b.set).toEqual({ source: 'spikes/embeddings-local/questions-v2.yaml', replace: { v03: 't01' } });
    expect(b.fixed).toEqual([{ source: 'spec/fixtures/SRV-TUTOR/bench-fixed.yaml', id: 't01' }]);
    expect(GATE_N).toBe(b.gate_n);
    const set = benchSet(v2(), fixed());
    expect(set).toHaveLength(40);
    expect(new Set(set.map((q) => q.id)).size).toBe(40);
    expect(set.map((q) => q.id)).not.toContain('v03');
    expect(set[2]!.id).toBe('t01'); // in v03's place
    expect(new Set(set.map((q) => q.chapter)).size).toBe(23);
    expect(set.every((q) => q.question.length > 0 && q.gold_pages.length > 0)).toBe(true);
  });

  test('refuses a set that is not exactly the 40', () => {
    expect(() => benchSet(v2().slice(1), fixed())).toThrow(BenchError);
    expect(() => benchSet(v2(), [])).toThrow(BenchError);
    expect(() => benchSet([...v2(), v2()[0]!], fixed())).toThrow(BenchError);
  });
});

describe('bench runs and summary (NFR-TUTOR-1..3)', () => {
  test('p95 is nearest rank', () => {
    expect(p95([1])).toBe(1);
    expect(p95(Array.from({ length: 20 }, (_, i) => 20 - i))).toBe(19);
    expect(p95(Array.from({ length: 40 }, (_, i) => i + 1))).toBe(38);
  });

  test('runs merge by question id, a later run replacing an earlier record', () => {
    const all = forSet();
    const day1 = all.slice(0, 25);
    const day2 = [...all.slice(24), { ...day1[0]!, grounded: false }];
    const merged = mergeRuns([day1, day2]);
    expect(merged).toHaveLength(40);
    expect(new Set(merged.map((r) => r.id))).toEqual(new Set(ids()));
    expect(merged.find((r) => r.id === day1[0]!.id)!.grounded).toBe(false);
    expect(summary(merged, ids()).gate_n_met).toBe(true);
    expect(summary(day1, ids()).gate_n_met).toBe(false); // day 1 alone decides nothing
    expect(mergeRuns([])).toEqual([]);
  });

  test('thresholds come from the spec and the gate is exactly the 40', () => {
    expect(THRESHOLDS).toEqual({ p95_s: nfr('NFR-TUTOR-1'), mean_usd: nfr('NFR-TUTOR-2'), grounded_share: nfr('NFR-TUTOR-3') });
    const all = forSet((i) => ({ grounded: i >= 4, insufficient: i < 3, blocks_with_verified_quote: i < 4 ? 1 : 2 }));
    const s = summary(all, ids());
    expect(s).toMatchObject({
      n: 40, p95_s: 5, mean_usd: 0.05, grounded_share: 0.9, insufficient_share: 0.075, verified_block_share: 0.95,
      gate_n_met: true, pass: { 'NFR-TUTOR-1': true, 'NFR-TUTOR-2': true, 'NFR-TUTOR-3': true },
    });
    expect(Object.keys(s.per_chapter)).toHaveLength(23);
    expect(Object.values(s.per_chapter).reduce((a, c) => a + c.n, 0)).toBe(40);
    const ch1 = all.filter((r) => r.chapter === 1);
    expect(s.per_chapter['1']).toEqual({ n: ch1.length, grounded: ch1.filter((r) => r.grounded).length });
    const partial = summary(all.slice(0, 39), ids());
    expect(partial.gate_n_met).toBe(false);
    expect(partial.pass).toEqual({ 'NFR-TUTOR-1': false, 'NFR-TUTOR-2': false, 'NFR-TUTOR-3': false });
    expect(summary([...all.slice(0, 39), record({ id: 'v99' })], ids()).gate_n_met).toBe(false);
  });

  test('mean cost counts answered and expired questions only', () => {
    const all = forSet((i) => (i < 4 ? { state: i < 2 ? 'failed' : 'refused', usd: 0 } : { usd: 0.06 }));
    expect(summary(all, ids()).mean_usd).toBe(0.06);
    const failing = summary(forSet((i) => ({ wall_s: i === 39 ? 99 : 40, usd: 0.07, grounded: i % 2 === 0 })), ids());
    expect(failing.pass).toEqual({ 'NFR-TUTOR-1': false, 'NFR-TUTOR-2': false, 'NFR-TUTOR-3': false });
    expect(summary([], ids()).n).toBe(0);
  });
});
