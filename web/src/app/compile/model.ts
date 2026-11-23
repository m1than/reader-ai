// Compile state mapping and copy (WEB-UI FR-UI-15): data plus pure functions; the component only renders them.
import type { CompileStatus, LessonEstimate } from '../api';

/** SRV-PIPELINE bounds.run_cap_usd, pinned by a unit test against the spec. */
export const RUN_CAP_USD = '8.00';
/** A stage takes tens of seconds; L02's whole compile took 210 s. */
export const COMPILE_POLL_MS = 5_000;
/** Polling stops this long after the query was last started or refreshed. */
export const COMPILE_POLL_MAX_MS = 30 * 60 * 1000;

const STAGES: Record<string, string> = {
  draft: 'drafting',
  fact_check: 'checking facts',
  adversarial_review: 'reviewing',
  revise: 'revising',
  findings: 'collecting findings',
  lab_verification: 'verifying labs',
};

const REFUSALS: Record<string, (cap: string) => string> = {
  budget_exhausted: (cap) => `it reached its $${cap} cap`,
  task_budget_exhausted: () => 'a stage reached its own budget',
  monthly_budget_exhausted: () => 'the monthly budget is used up',
  daily_budget_exhausted: () => 'the daily budget is used up',
};

const STOP_REASONS: Record<string, string> = {
  provider_missing: 'No AI provider is connected for your account. Connect one, then retry.',
};

const own = <T,>(table: Record<string, T>, key: string | null | undefined): T | undefined =>
  key != null && Object.hasOwn(table, key) ? table[key] : undefined;

export const money = (value: string): string => {
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(2) : value;
};

export const stageLabel = (stage: string): string => own(STAGES, stage) ?? stage.replaceAll('_', ' ');

/** The stage of the last step that carries a `status` (the pending one), else of the last step. */
export const currentStage = (run: CompileStatus): string | null => {
  const pending = [...run.steps].reverse().find((s) => s.status !== undefined);
  return (pending ?? run.steps[run.steps.length - 1])?.stage ?? null;
};

export type Action = 'compile' | 'retry' | null;

export interface Described {
  text: string | null;
  action: Action;
}

const OPEN = new Set(['running', 'stalled', 'stage_failed', 'budget_exhausted']);
const POLLING = new Set(['running', 'stalled']);

/** States in which a compile has not finished (the draft chip). */
export const isOpen = (state: string): boolean => OPEN.has(state);

export const describeRun = (run: CompileStatus | null): Described => {
  if (run === null) return { text: 'not compiled yet', action: 'compile' };
  const stage = currentStage(run);
  const label = stage === null ? '' : stageLabel(stage);
  switch (run.state) {
    case 'running':
    case 'stalled':
      return { text: `Compiling: ${label}`, action: null };
    case 'stage_failed': {
      const reason = own(STOP_REASONS, run.error);
      return { text: reason ? `Compile stopped at ${label}. ${reason}` : `Compile stopped at ${label}`, action: 'retry' };
    }
    case 'budget_exhausted': {
      const refusal = own(REFUSALS, run.refusals[0]);
      const why = refusal ? refusal(money(run.cap_usd)) : 'a budget was reached';
      return { text: `Compile stopped: ${why}. Spent $${money(run.spent_usd)}.`, action: 'compile' };
    }
    default:
      return { text: null, action: null };
  }
};

export const pollInterval = (state: string | null, startedAt: number, now: number): number | false =>
  state !== null && POLLING.has(state) && now - startedAt < COMPILE_POLL_MAX_MS ? COMPILE_POLL_MS : false;

/** What the dialog's confirm does (FR-UI-15): nothing while closed; resume with no run closes, never compiles. */
export type Submit = 'skip' | 'close' | 'resume' | 'compile';
export const submitAction = (open: boolean, mode: 'compile' | 'resume', run: CompileStatus | null | undefined): Submit => {
  if (!open) return 'skip';
  if (mode === 'resume') return run ? 'resume' : 'close';
  return 'compile';
};

/** Whether a run state polls at all (before the 30-minute window is applied). */
export const polls = (state: string | null): boolean => state !== null && POLLING.has(state);

/** A POST's error: closes the dialog with a notice (the row shows the run), or stays as an alert line. */
export type PostOutcome = { kind: 'notice'; code: string } | { kind: 'alert'; text: string; reloadList: boolean };

const NOTICE_CODES = new Set(['compile_running', 'already_started', 'not_failed']);
const ALERTS: Record<string, string> = {
  lesson_unknown: 'This lesson no longer exists. Reload the list.',
  not_found: 'This lesson no longer exists. Reload the list.',
  no_material: 'The source of this lesson is not imported.',
  no_source_spans: "This lesson's pages have no text to compile.",
  source_too_large: 'This lesson is too long to compile in one run.',
  network: 'The server could not be reached. Check the connection and try again.',
};

export const postOutcome = (code: string): PostOutcome =>
  NOTICE_CODES.has(code)
    ? { kind: 'notice', code }
    : {
        kind: 'alert',
        text: own(ALERTS, code) ?? 'Something went wrong. Try again.',
        reloadList: code === 'lesson_unknown' || code === 'not_found',
      };

export const NO_TEXT = "This lesson's pages have no text to compile.";

/** The confirm's estimate lines (FR-UI-15). */
export const estimateLines = (e: LessonEstimate): { lines: string[]; empty: boolean } => {
  if (e.kchars === '0.000') return { lines: [NO_TEXT], empty: true };
  const k = Number(e.kchars);
  const basis = Number(e.basis_kchars);
  const lines = [
    `Estimate: $${e.low_usd}–$${e.high_usd}`,
    `An estimate from one measured lesson (${e.basis}), scaled by this lesson's ${k.toFixed(1)} thousand characters; not a bound.`,
  ];
  if (e.extrapolated && k > 0 && basis > 0) {
    lines.push(
      k < basis
        ? `This lesson is ${(basis / k).toFixed(1)}× shorter than the measured one; the real cost is likely higher than this range.`
        : `This lesson is ${(k / basis).toFixed(1)}× longer than the measured one; the real cost may differ from this range.`,
    );
  }
  return { lines, empty: false };
};
