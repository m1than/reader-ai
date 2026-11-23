// @trace VEC-TUTOR-7 NFR-TUTOR-1 NFR-TUTOR-2 NFR-TUTOR-3
import { describe, expect, test } from 'vitest';
import { readYaml } from '../app/testing/fixtures';
import { BenchError, benchQuestions, GATE_N, p95, summary, TAKE, TEMPLATES, THRESHOLDS, type BenchRecord } from './bench';

type Bench = { generator: { cells: string; templates: string[]; take: number }; gate_n: number; fixed: { source: string; id: string }[] };
type Spec = { nfr: { id: string; threshold: number }[] };
const bench = () => readYaml<Bench>('spec/fixtures/SRV-TUTOR/bench.yaml');
const nfr = (id: string) => readYaml<Spec>('spec/modules/SRV-TUTOR.yaml').nfr.find((n) => n.id === id)!.threshold;

const cell = (id: string, kind: string, source: string) => ({ id, cell_type: 'markdown', source, metadata: { reader: { kind } } });
const notebook = (explain: number, extra: ReturnType<typeof cell>[] = []) => ({
  cells: [cell('route', 'route', '## Route'), ...Array.from({ length: explain }, (_, i) => cell(`e${i}`, 'explain', `## Title ${i}\n\nBody.`)), ...extra],
});

const record = (over: Partial<BenchRecord> = {}): BenchRecord => ({
  id: 'x', question_id: null, state: 'answered', wall_s: 5, usd: 0.05, output_tokens: 300, thinking_tokens: 0,
  grounded: true, insufficient: false, blocks: 2, blocks_with_verified_quote: 2, ...over,
});

describe('bench generator (VEC-TUTOR-7)', () => {
  test('follows the fixture', () => {
    const b = bench();
    expect(TEMPLATES).toEqual(b.generator.templates);
    expect(TAKE).toBe(b.generator.take);
    expect(GATE_N).toBe(b.gate_n);
    expect(b.fixed).toEqual([{ source: 'spec/fixtures/SRV-TUTOR/bench-fixed.yaml', id: 't01' }]);
    const fixed = readYaml<{ questions: { id: string; gold_pages: number[]; question: string }[] }>(b.fixed[0]!.source).questions;
    expect(fixed.map((q) => q.id)).toEqual(['t01']);
    expect(fixed[0]!.gold_pages.every((p) => p >= 60 && p <= 84)).toBe(true); // L02's pages
  });

  test('every template for each explain cell in notebook order, the first 40', () => {
    const qs = benchQuestions(notebook(11, [cell('lab', 'lab', '## Not this')]));
    expect(qs).toHaveLength(40);
    expect(qs.slice(0, 5)).toEqual([
      { cellId: 'e0', question: 'Explain in your own words: Title 0' },
      { cellId: 'e0', question: 'Why does it matter: Title 0?' },
      { cellId: 'e0', question: 'Give a concrete example for: Title 0' },
      { cellId: 'e0', question: 'What is easy to get wrong about: Title 0?' },
      { cellId: 'e1', question: 'Explain in your own words: Title 1' },
    ]);
    expect(qs.at(-1)!.cellId).toBe('e9');
    expect(benchQuestions({ cells: notebook(10).cells.map((c) => ({ ...c, source: [String(c.source)] })) })).toHaveLength(40);
  });

  test('stops before any question when fewer than 40 result or a cell has no title', () => {
    expect(() => benchQuestions(notebook(9))).toThrow(BenchError);
    expect(() => benchQuestions({ cells: [] })).toThrow(BenchError);
    const untitled = notebook(10);
    untitled.cells[3] = cell('e2', 'explain', 'No heading here.');
    expect(() => benchQuestions(untitled)).toThrow(BenchError);
  });
});

describe('bench summary (NFR-TUTOR-1..3)', () => {
  test('p95 is nearest rank', () => {
    expect(p95([1])).toBe(1);
    expect(p95(Array.from({ length: 20 }, (_, i) => 20 - i))).toBe(19);
    expect(p95(Array.from({ length: 41 }, (_, i) => i + 1))).toBe(39);
  });

  test('thresholds come from the spec and the gate needs n >= 40', () => {
    expect(THRESHOLDS).toEqual({ p95_s: nfr('NFR-TUTOR-1'), mean_usd: nfr('NFR-TUTOR-2'), grounded_share: nfr('NFR-TUTOR-3') });
    const passing = Array.from({ length: 41 }, (_, i) => record({ grounded: i >= 4, insufficient: i < 3, blocks_with_verified_quote: i < 4 ? 1 : 2 }));
    const s = summary(passing);
    expect(s).toMatchObject({
      n: 41, p95_s: 5, mean_usd: 0.05, grounded_share: 0.9024, insufficient_share: 0.0732,
      verified_block_share: 0.9512, gate_n_met: true,
      pass: { 'NFR-TUTOR-1': true, 'NFR-TUTOR-2': true, 'NFR-TUTOR-3': true },
    });
    const small = summary(passing.slice(0, 39));
    expect(small.gate_n_met).toBe(false);
    expect(small.pass['NFR-TUTOR-3']).toBe(false); // not decided below the gate
    const failing = summary(Array.from({ length: 40 }, (_, i) => record({ wall_s: i === 39 ? 99 : 40, usd: 0.07, grounded: i % 2 === 0 })));
    expect(failing.pass).toEqual({ 'NFR-TUTOR-1': false, 'NFR-TUTOR-2': false, 'NFR-TUTOR-3': false });
    expect(summary([]).n).toBe(0);
  });
});
