// The tutor bench (SRV-TUTOR VEC-TUTOR-7, NFR-TUTOR-1..3): the question generator over a compiled revision and the
// summary of a run. Used by the owner slice suite's `tutor-bench` step (e2e/web/live/owner.slice.ts), which asks
// over the HTTP API. Pure functions; nothing here sees an answer's text.

export const TEMPLATES: readonly string[] = [
  'Explain in your own words: {cell_title}',
  'Why does it matter: {cell_title}?',
  'Give a concrete example for: {cell_title}',
  'What is easy to get wrong about: {cell_title}?',
];
export const TAKE = 40;
export const GATE_N = 40; // n >= 40: a 0.9 tutor passes the 0.8 bar 98.5 % of the time, a 0.7 tutor 11 %
export const THRESHOLDS = { p95_s: 30, mean_usd: 0.06, grounded_share: 0.8 } as const; // NFR-TUTOR-1, -2, -3

export class BenchError extends Error {
  override name = 'BenchError';
}

interface NbCell {
  id?: unknown;
  source?: unknown;
  metadata?: unknown;
}

const sourceOf = (c: NbCell): string =>
  Array.isArray(c.source) ? c.source.map(String).join('') : typeof c.source === 'string' ? c.source : '';

const kindOf = (c: NbCell): unknown => {
  const reader = (c.metadata as { reader?: { kind?: unknown } } | undefined)?.reader;
  return reader?.kind;
};

/** The first markdown heading of the cell source, without its #s. */
export const titleOf = (c: NbCell): string | null => {
  for (const line of sourceOf(c).split('\n')) {
    if (line.startsWith('#')) return line.replace(/^#+/, '').trim() || null;
  }
  return null;
};

/** For each explain cell in notebook order, every template in order; the first TAKE, or BenchError before any. */
export const benchQuestions = (notebook: { cells?: NbCell[] }): { cellId: string; question: string }[] => {
  const out: { cellId: string; question: string }[] = [];
  for (const c of notebook.cells ?? []) {
    if (kindOf(c) !== 'explain' || out.length >= TAKE) continue;
    const title = titleOf(c);
    if (title === null || typeof c.id !== 'string') throw new BenchError('an explain cell has no title or id');
    for (const t of TEMPLATES) out.push({ cellId: c.id, question: t.replace('{cell_title}', title) });
  }
  if (out.length < TAKE) throw new BenchError(`only ${out.length} questions; the gate needs ${TAKE}`);
  return out.slice(0, TAKE);
};

/** Nearest rank: rank ceil(0.95 n) of the samples sorted ascending. */
export const p95 = (xs: number[]): number => {
  const s = [...xs].sort((a, b) => a - b);
  return s[Math.ceil(0.95 * s.length) - 1]!;
};

export interface BenchRecord {
  id: string;
  question_id: string | null;
  state: string | null;
  wall_s: number;
  usd: number;
  output_tokens: number;
  thinking_tokens: number | null;
  grounded: boolean;
  insufficient: boolean;
  blocks: number;
  blocks_with_verified_quote: number;
}

const round = (x: number, digits: number) => +x.toFixed(digits);

export const summary = (records: BenchRecord[]) => {
  const n = records.length;
  const blocks = records.reduce((a, r) => a + r.blocks, 0);
  const verified = records.reduce((a, r) => a + r.blocks_with_verified_quote, 0);
  const p95_s = n ? p95(records.map((r) => r.wall_s)) : null;
  const mean_usd = n ? round(records.reduce((a, r) => a + r.usd, 0) / n, 6) : null;
  const grounded_share = n ? round(records.filter((r) => r.grounded).length / n, 4) : null;
  const insufficient_share = n ? round(records.filter((r) => r.insufficient).length / n, 4) : null;
  const gate_n_met = n >= GATE_N;
  return {
    n,
    p95_s,
    mean_usd,
    grounded_share,
    insufficient_share,
    verified_block_share: blocks ? round(verified / blocks, 4) : null,
    gate_n_met,
    pass: {
      'NFR-TUTOR-1': gate_n_met && p95_s !== null && p95_s <= THRESHOLDS.p95_s,
      'NFR-TUTOR-2': gate_n_met && mean_usd !== null && mean_usd <= THRESHOLDS.mean_usd,
      'NFR-TUTOR-3': gate_n_met && grounded_share !== null && grounded_share >= THRESHOLDS.grounded_share,
    },
  };
};
