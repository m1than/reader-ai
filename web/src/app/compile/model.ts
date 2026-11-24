// Compile state mapping and copy (WEB-UI FR-UI-15): data plus pure functions; the component only renders them. The
// copy is the app catalogue's `compile.*` and `enum.stage.*` messages (FR-UI-30); each function takes the active
// translator and defaults to English.
import { englishT, type T } from '../../ui/i18n';
import type { CompileStatus, LessonEstimate } from '../api';
import en from '../messages.en';

type Tr = T<typeof en>;
const EN: Tr = englishT(en);

/** SRV-PIPELINE bounds.run_cap_usd, pinned by a unit test against the spec. */
export const RUN_CAP_USD = '8.00';
/** A stage takes tens of seconds; L02's whole compile took 210 s. */
export const COMPILE_POLL_MS = 5_000;
/** Polling stops this long after the query was last started or refreshed. */
export const COMPILE_POLL_MAX_MS = 30 * 60 * 1000;

const REFUSALS = new Set(['budget_exhausted', 'task_budget_exhausted', 'monthly_budget_exhausted', 'daily_budget_exhausted']);
const STOP_REASONS = new Set(['provider_missing']);

export const money = (value: string): string => {
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(2) : value;
};

/** A stage through the catalogue's `enum.stage.*`; an unknown stage reads with spaces for underscores. */
export const stageLabel = (stage: string, t: Tr = EN): string => {
  const shown = t.enum('stage', stage);
  return shown === stage ? stage.replaceAll('_', ' ') : shown;
};

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

export const describeRun = (run: CompileStatus | null, t: Tr = EN): Described => {
  if (run === null) return { text: t('compile.not_compiled'), action: 'compile' };
  const stage = currentStage(run);
  const label = stage === null ? '' : stageLabel(stage, t);
  switch (run.state) {
    case 'running':
    case 'stalled':
      return { text: t('compile.compiling', { stage: label }), action: null };
    case 'stage_failed': {
      const reason = run.error != null && STOP_REASONS.has(run.error) ? t.maybe(`compile.reason.${run.error}`) : null;
      return {
        text: reason ? t('compile.stopped_at_reason', { stage: label, reason }) : t('compile.stopped_at', { stage: label }),
        action: 'retry',
      };
    }
    case 'budget_exhausted': {
      const code = run.refusals[0];
      const why = code !== undefined && REFUSALS.has(code) ? t.maybe(`compile.refusal.${code}`, { cap: money(run.cap_usd) }) : t('compile.refusal.other');
      return { text: t('compile.stopped_budget', { why, spent: money(run.spent_usd) }), action: 'compile' };
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
const ALERTS = new Set(['lesson_unknown', 'not_found', 'no_material', 'no_source_spans', 'source_too_large', 'network']);

export const postOutcome = (code: string, t: Tr = EN): PostOutcome =>
  NOTICE_CODES.has(code)
    ? { kind: 'notice', code }
    : {
        kind: 'alert',
        text: ALERTS.has(code) ? t.maybe(`compile.alert.${code}`) : t('compile.alert.other'),
        reloadList: code === 'lesson_unknown' || code === 'not_found',
      };

export const NO_TEXT = EN('compile.alert.no_source_spans');

/** The confirm's estimate lines (FR-UI-15). */
export const estimateLines = (e: LessonEstimate, t: Tr = EN): { lines: string[]; empty: boolean } => {
  if (e.kchars === '0.000') return { lines: [t('compile.alert.no_source_spans')], empty: true };
  const k = Number(e.kchars);
  const basis = Number(e.basis_kchars);
  const one = (n: number) => new Intl.NumberFormat(t.locale, { minimumFractionDigits: 1, maximumFractionDigits: 1, useGrouping: false }).format(n);
  const lines = [
    t('compile.estimate', { low: e.low_usd, high: e.high_usd }),
    t('compile.estimate_basis', { basis: e.basis, kchars: one(k) }),
  ];
  if (e.extrapolated && k > 0 && basis > 0) lines.push(k < basis ? t('compile.shorter', { ratio: one(basis / k) }) : t('compile.longer', { ratio: one(k / basis) }));
  return { lines, empty: false };
};
