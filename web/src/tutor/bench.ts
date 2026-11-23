// The tutor bench (SRV-TUTOR VEC-TUTOR-7, NFR-TUTOR-1..3): the question set (questions-v2, 40 independent
// questions over all 23 chapters, with v03 replaced by t01), the merge of runs over several UTC days, and the
// summary. Used by the owner slice suite's `tutor-bench` step (e2e/web/live/owner.slice.ts), which asks over the
// HTTP API. Pure functions; nothing here sees an answer's text.

export const GATE_N = 40; // exactly the 40: a 0.9 tutor passes the 0.8 bar 98.5 % of the time, a 0.7 tutor 11 % (iid)
export const REPLACE: Readonly<Record<string, string>> = { v03: 't01' }; // v03's premise is not in the book
export const THRESHOLDS = { p95_s: 30, mean_usd: 0.06, grounded_share: 0.8 } as const; // NFR-TUTOR-1, -2, -3

export class BenchError extends Error {
  override name = 'BenchError';
}

export interface BenchQuestion {
  id: string;
  chapter: number;
  gold_pages: number[];
  question: string;
}

/** questions-v2 in its order, each replaced id swapped for its fixed question; exactly GATE_N unique ids. */
export const benchSet = (v2: BenchQuestion[], fixed: BenchQuestion[]): BenchQuestion[] => {
  const byId = new Map(fixed.map((q) => [q.id, q]));
  const set = v2.map((q) => {
    const replacement = REPLACE[q.id];
    if (replacement === undefined) return q;
    const f = byId.get(replacement);
    if (f === undefined) throw new BenchError(`fixed question ${replacement} missing`);
    return f;
  });
  if (set.length !== GATE_N || new Set(set.map((q) => q.id)).size !== GATE_N) {
    throw new BenchError(`the bench set must be exactly ${GATE_N} unique questions`);
  }
  return set;
};

/** Nearest rank: rank ceil(0.95 n) of the samples sorted ascending. */
export const p95 = (xs: number[]): number => {
  const s = [...xs].sort((a, b) => a - b);
  return s[Math.ceil(0.95 * s.length) - 1]!;
};

export interface BenchRecord {
  id: string;
  chapter: number;
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
  gold_in_top8: boolean; // a gold page among the question's top 8 search hits (the retrieval predictor)
  gold_cited: boolean; // a page the answer cites is a gold page
}

/** Whether any of `pages` is one of the question's gold pages. */
export const hitsGold = (pages: number[], gold: number[]): boolean => pages.some((p) => gold.includes(p));

/** The records of several runs (oldest first), one per question id: a later run's record replaces an earlier one. */
export const mergeRuns = (runs: BenchRecord[][]): BenchRecord[] => {
  const byId = new Map<string, BenchRecord>();
  for (const run of runs) {
    for (const r of run) {
      byId.delete(r.id);
      byId.set(r.id, r);
    }
  }
  return [...byId.values()];
};

const round = (x: number, digits: number) => +x.toFixed(digits);
// A question's cost: answered ones, and expired ones that spent something; failed, refused and unspent expired
// records do not describe it (NFR-TUTOR-2).
const costed = (r: BenchRecord) => r.state === 'answered' || (r.state === 'expired' && r.usd > 0);
const split = (rs: BenchRecord[]) => {
  const grounded = rs.filter((r) => r.grounded).length;
  return { n: rs.length, grounded, share: rs.length ? round(grounded / rs.length, 4) : null };
};

/** The numbers of the merged runs; NFRs are decided only when the records are exactly the bench set's ids. */
export const summary = (records: BenchRecord[], setIds: string[]) => {
  const n = records.length;
  const ids = new Set(records.map((r) => r.id));
  const gate_n_met =
    n === GATE_N && ids.size === GATE_N && setIds.length === GATE_N && setIds.every((id) => ids.has(id));
  const blocks = records.reduce((a, r) => a + r.blocks, 0);
  const verified = records.reduce((a, r) => a + r.blocks_with_verified_quote, 0);
  const spent = records.filter(costed);
  const p95_s = n ? p95(records.map((r) => r.wall_s)) : null;
  const mean_usd = spent.length ? round(spent.reduce((a, r) => a + r.usd, 0) / spent.length, 6) : null;
  const grounded_share = n ? round(records.filter((r) => r.grounded).length / n, 4) : null;
  const per_chapter: Record<string, { n: number; grounded: number }> = {};
  for (const r of records) {
    const c = (per_chapter[String(r.chapter)] ??= { n: 0, grounded: 0 });
    c.n += 1;
    if (r.grounded) c.grounded += 1;
  }
  return {
    n,
    p95_s,
    mean_usd,
    grounded_share,
    insufficient_share: n ? round(records.filter((r) => r.insufficient).length / n, 4) : null,
    verified_block_share: blocks ? round(verified / blocks, 4) : null,
    gold_cited_share: n ? round(records.filter((r) => r.gold_cited).length / n, 4) : null,
    // A miss with the gold page retrieved is the tutor's; without it, retrieval's (NFR-TUTOR-3).
    grounded_given_gold_in_top8: {
      in_top8: split(records.filter((r) => r.gold_in_top8)),
      not_in_top8: split(records.filter((r) => !r.gold_in_top8)),
    },
    per_chapter,
    gate_n_met,
    pass: {
      'NFR-TUTOR-1': gate_n_met && p95_s !== null && p95_s <= THRESHOLDS.p95_s,
      'NFR-TUTOR-2': gate_n_met && mean_usd !== null && mean_usd <= THRESHOLDS.mean_usd,
      'NFR-TUTOR-3': gate_n_met && grounded_share !== null && grounded_share >= THRESHOLDS.grounded_share,
    },
  };
};
