// WEB-MANAGE pure rules (FR-MANAGE-1, -2, -4, -11): titles, typed confirmation, the response mapping, the download
// file name, the plan's week conversion and the editable settings' bounds. Data plus functions; components render them.
import { englishT, type T } from '../ui/i18n';
import en from './messages.en';

type Tr = T<typeof en>;
const EN: Tr = englishT(en);

const CONTROL = /\p{Cc}/u;

/** A title as the server checks it: trimmed, NFC, 1..max code points, no control characters (FR-LESSON-22). */
export const cleanTitle = (v: string, max = 200): { ok: true; value: string } | { ok: false } => {
  const value = v.trim().normalize('NFC');
  const n = [...value].length;
  return n >= 1 && n <= max && !CONTROL.test(value) ? { ok: true, value } : { ok: false };
};

/** FR-MANAGE-2: the stored title NFC-normalised, cut to its first 40 code points, then trimmed (the server's rule,
 * SRV-LESSON FR-LESSON-25, SRV-INGEST FR-INGEST-31). */
export const confirmTarget = (title: string): string => [...title.normalize('NFC')].slice(0, 40).join('').trim();

/** The request's `confirm`: the typed text NFC-normalised and trimmed. */
export const normaliseConfirm = (typed: string): string => typed.normalize('NFC').trim();

/** The typed text, normalised, equals the target (the server compares the same way). An empty target matches
 * nothing, the empty input included (FR-MANAGE-2). */
export const matchesConfirm = (typed: string, title: string): boolean => {
  const target = confirmTarget(title);
  return target !== '' && normaliseConfirm(typed) === target;
};

export type ObjectKind = 'course' | 'lesson' | 'source' | 'note' | 'passkey' | 'session' | 'person' | 'setting' | 'compile';

export interface Problem {
  text: string;
  /** A sign-in link that returns to /settings (FR-AUTH-47). */
  signIn?: string;
  /** The object is gone: refetch the lists. */
  refetch?: boolean;
}

export const SIGN_IN_AGAIN = '/api/auth/signin?next=/settings';

const RUNNING = new Set(['course', 'lesson', 'source']);

/** FR-MANAGE-1's response table; no server text is ever shown. */
export const problem = (code: string, object: ObjectKind, t: Tr = EN): Problem => {
  switch (code) {
    case 'reauth_required':
      return { text: t('problem.reauth'), signIn: SIGN_IN_AGAIN };
    case 'not_found':
    case 'unknown_revision':
      return { text: t.maybe(`gone.${object}`), refetch: true };
    case 'stale':
      return { text: t('problem.stale') };
    case 'title_taken':
      return { text: t('problem.title_taken') };
    case 'compile_running':
      return { text: t.maybe(`problem.compile_running.${RUNNING.has(object) ? object : 'lesson'}`) };
    case 'material_deleted':
      return { text: t('problem.material_deleted') };
    case 'material_busy':
      return { text: t('problem.material_busy') };
    case 'lesson_inactive':
      return { text: t('problem.lesson_inactive') };
    case 'notes_cell_removed':
      return { text: t('revert.notes_removed') };
    case 'limit':
      return { text: t('notes.limit') };
    case 'note_invalid':
      return { text: t('notes.invalid') };
    case 'cell_unknown':
      return { text: t('notes.cell_unknown') };
    case 'last_passkey':
      return { text: t('passkey.last') };
    case 'register_failed':
      return { text: t('passkey.add_failed') };
    case 'user_has_data':
      return { text: t('person.has_data') };
    case 'file_narrower':
      return { text: t('instance.signup_off') };
    case 'value_invalid':
    case 'not_overridable':
      return { text: t('instance.value_invalid') };
    case 'title_invalid':
    case 'label_invalid':
      return { text: t('title.invalid', { max: object === 'passkey' ? 64 : 200 }) };
    case 'description_invalid':
      return { text: t('course.description_invalid') };
    default:
      return code === 'network' || /^http_5\d\d$/.test(code) || code === 'bad_response' ? { text: t('problem.network') } : { text: t('problem.other') };
  }
};

/** FR-MANAGE-4: a lesson's file name segment from its title. */
export const slugify = (title: string): string => {
  const s = title
    .normalize('NFC')
    .replace(/[/\\:.\p{Cc}]|\s+/gu, '-')
    .replace(/-+/g, '-')
    .replace(/^-+|-+$/g, '');
  const cut = [...s].slice(0, 120).join('').replace(/-+$/g, '');
  return cut === '' ? 'lesson' : cut;
};

export const downloadName = (position: number, title: string): string => `${position}-${slugify(title)}.ipynb`;

/** SRV-PLAN FR-PLAN-20: a target date as whole weeks from the plan's start, clamped to 1..104. */
export const weeksFor = (start: string, target: string): number => {
  const days = Math.round((Date.parse(`${target}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 86_400_000);
  return Math.min(104, Math.max(1, Math.ceil((days + 1) / 7)));
};

/** FR-MANAGE-11: the schema's bounds of each editable number (server/src/reader/config/model.py). */
export interface Bound {
  min: number;
  max: number;
  step: number;
  integer: boolean;
}
export const boundOf = (key: string): Bound | null => {
  if (key === 'observability.retention_days') return { min: 1, max: 365, step: 1, integer: true };
  if (key === 'budgets.warn_at') return { min: 0.01, max: 1, step: 0.01, integer: false };
  if (key === 'budgets.monthly_usd' || /^tasks\.[^.]+\.budget_usd_per_lesson$/.test(key)) return { min: 0.01, max: 100000, step: 0.01, integer: false };
  return null;
};

export const validNumber = (raw: string, b: Bound): number | null => {
  if (!/^\d+(\.\d+)?$/.test(raw.trim())) return null;
  const n = Number(raw);
  if (!Number.isFinite(n) || n < b.min || n > b.max) return null;
  if (b.integer && !Number.isInteger(n)) return null;
  return n;
};

/** The visible position of each lesson of a course, 1..n, never the stored ordinal (SRV-LESSON FR-LESSON-28). */
export const positions = <L extends { lesson_id: string; course_id: string; ordinal: number }>(lessons: readonly L[]): Map<string, number> => {
  const out = new Map<string, number>();
  const byCourse = new Map<string, L[]>();
  for (const l of lessons) byCourse.set(l.course_id, [...(byCourse.get(l.course_id) ?? []), l]);
  for (const list of byCourse.values()) [...list].sort((a, b) => a.ordinal - b.ordinal).forEach((l, i) => out.set(l.lesson_id, i + 1));
  return out;
};
