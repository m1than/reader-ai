// Pure helpers of the planning part of /import (WEB-PLAN FR-IMPORT-4..6): field checks, the estimate's lines and
// the local date. Every line is plain text; numbers and dates use Intl in the user's locale. The copy comes from this
// area's catalogue (WEB-UI FR-UI-30): English by default, or the translator the component passes.
import { englishT } from '../../ui/i18n';
import type { Preview } from './api';
import en from './messages.en';
import type { PlanT } from './messages';

const english: PlanT = englishT(en);

export const WEEKS = { min: 1, max: 104 } as const;
export const MINUTES = { min: 10, max: 240, step: 5, default: 60 } as const;
export const RATE = { min: 0.1, max: 20, default: '2.64' } as const;

export interface Fields {
  weeks: string;
  minutes: string;
  rate: string;
}

export type FieldErrors = Partial<Record<keyof Fields, string>>;

export interface Valid {
  total_weeks: number;
  minutes_per_day: number;
  rate: string;
}

const INT = /^\d{1,4}$/;
const DECIMAL = /^\d{1,2}(\.\d{1,2})?$/;

/** FR-IMPORT-4: the same checks as the native constraint attributes. */
export const check = (f: Fields, t: PlanT = english): { errors: FieldErrors; value: Valid | null } => {
  const errors: FieldErrors = {};
  const weeks = Number(f.weeks);
  if (!INT.test(f.weeks.trim()) || weeks < WEEKS.min || weeks > WEEKS.max) errors.weeks = t('field.weeks_error');
  const minutes = Number(f.minutes);
  if (!INT.test(f.minutes.trim()) || minutes < MINUTES.min || minutes > MINUTES.max || minutes % MINUTES.step !== 0)
    errors.minutes = t('field.minutes_error');
  const rate = Number(f.rate);
  if (!DECIMAL.test(f.rate.trim()) || rate < RATE.min || rate > RATE.max) errors.rate = t('field.rate_error');
  const ok = Object.keys(errors).length === 0;
  return { errors, value: ok ? { total_weeks: weeks, minutes_per_day: minutes, rate: rate.toFixed(2) } : null };
};

/** The browser's local date as YYYY-MM-DD (SRV-PLAN `today`). */
export const localDate = (d: Date): string =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

const longDate = (iso: string, locale: string): string =>
  new Intl.DateTimeFormat(locale, { year: 'numeric', month: 'long', day: 'numeric', timeZone: 'UTC' }).format(
    new Date(`${iso}T00:00:00Z`),
  );

const usd = (value: string): string => `$${value}`;

export interface EstimateLines {
  headline: string;
  reading: string;
  deadline: string | null;
  fills: string | null;
  course: string;
  first: string | null;
  extrapolation: string | null;
  notices: string[];
}

/** FR-IMPORT-5: the estimate card's text from a preview body. */
export const estimateLines = (p: Preview, locale: string, t: PlanT = english): EstimateLines => {
  const s = p.summary;
  const e = p.estimate;
  const num = new Intl.NumberFormat(locale);
  const finish = s.finish_by ? t('est.finish', { date: longDate(s.finish_by, locale) }) : '';
  const each = s.minutes_mean !== null && s.minutes_max !== null ? t('est.each', { mean: num.format(s.minutes_mean), max: num.format(s.minutes_max) }) : '';
  // Numbers are formatted here in the page's locale and passed as text; `count` only selects the plural form.
  const headline =
    s.fixed_count > 0
      ? t('est.headline_fixed', { fixed: num.format(s.fixed_count), new: num.format(s.new_count), each, finish })
      : t('est.headline', { count: s.new_count, n: num.format(s.new_count), each, finish });
  let extrapolation: string | null = null;
  if (e.extrapolated && e.mean_lesson_kchars !== null) {
    const mean = Number(e.mean_lesson_kchars);
    const basis = Number(e.basis_kchars);
    const fmt = new Intl.NumberFormat(locale, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
    extrapolation = mean < basis ? t('est.shorter', { ratio: fmt.format(basis / mean) }) : t('est.longer', { ratio: fmt.format(mean / basis) });
  }
  const notices: string[] = [];
  if (!s.fits) {
    for (const reason of s.reasons) {
      if (reason === 'minutes')
        notices.push(t('enum.reason.minutes', { max: num.format(s.minutes_max ?? 0), minutes: num.format(p.inputs.minutes_per_day) }));
      else if (reason === 'days') notices.push(s.days_left < 1 ? t('reason.deadline_passed') : t.enum('reason', 'days'));
      else notices.push(t.enum('reason', reason));
    }
  }
  return {
    headline,
    reading: t('est.reading'),
    deadline: s.fixed_count > 0 ? t('est.deadline', { date: longDate(s.end, locale) }) : null,
    fills: s.spare_days > 0 ? t('est.fills', { new: num.format(s.new_count), days: num.format(s.days_left) }) : null,
    course: t('est.course', { low: usd(e.course_low_usd), high: usd(e.course_high_usd) }),
    first: e.first_lesson ? t('est.first', { low: usd(e.first_lesson.low_usd), high: usd(e.first_lesson.high_usd), cap: usd(e.run_cap_usd) }) : null,
    extrapolation,
    notices,
  };
};

/** FR-IMPORT-5 (design pass): the figures as definition-list rows, each only when its value exists. */
export const estimateRows = (p: Preview, locale: string, t: PlanT = english): Array<[string, string]> => {
  const s = p.summary;
  const e = p.estimate;
  const num = new Intl.NumberFormat(locale);
  const rows: Array<[string, string]> = [
    [t('row.lessons'), s.fixed_count > 0 ? t('row.lessons_fixed', { fixed: num.format(s.fixed_count), new: num.format(s.new_count) }) : num.format(s.new_count)],
  ];
  if (s.minutes_mean !== null && s.minutes_max !== null)
    rows.push([t('row.each'), t('row.each_value', { mean: num.format(s.minutes_mean), max: num.format(s.minutes_max) })]);
  if (s.finish_by) rows.push([t('row.finish'), longDate(s.finish_by, locale)]);
  if (s.fixed_count > 0) rows.push([t('row.deadline'), longDate(s.end, locale)]);
  rows.push([t('row.compile'), `${usd(e.course_low_usd)}–${usd(e.course_high_usd)}`]);
  if (e.first_lesson)
    rows.push([t('row.first'), t('row.first_value', { low: usd(e.first_lesson.low_usd), high: usd(e.first_lesson.high_usd), cap: usd(e.run_cap_usd) })]);
  return rows;
};

/** FR-IMPORT-5 (design pass): the muted notes under the figures, in order. */
export const estimateNotes = (lines: EstimateLines, t: PlanT = english): string[] =>
  [lines.reading, lines.fills, t('est.costs'), lines.extrapolation].filter((n): n is string => n !== null);
