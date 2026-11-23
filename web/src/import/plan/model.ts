// Pure helpers of the planning part of /import (WEB-PLAN FR-IMPORT-4..6): field checks, the estimate's lines and
// the local date. Every line is plain text; numbers and dates use Intl in the user's locale.
import type { Preview } from './api';

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
export const check = (f: Fields): { errors: FieldErrors; value: Valid | null } => {
  const errors: FieldErrors = {};
  const weeks = Number(f.weeks);
  if (!INT.test(f.weeks.trim()) || weeks < WEEKS.min || weeks > WEEKS.max) errors.weeks = 'Enter whole weeks from 1 to 104.';
  const minutes = Number(f.minutes);
  if (!INT.test(f.minutes.trim()) || minutes < MINUTES.min || minutes > MINUTES.max || minutes % MINUTES.step !== 0)
    errors.minutes = 'Enter minutes from 10 to 240, in steps of 5.';
  const rate = Number(f.rate);
  if (!DECIMAL.test(f.rate.trim()) || rate < RATE.min || rate > RATE.max)
    errors.rate = 'Enter minutes per 1,000 characters from 0.10 to 20.00.';
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
export const estimateLines = (p: Preview, locale: string): EstimateLines => {
  const s = p.summary;
  const e = p.estimate;
  const num = new Intl.NumberFormat(locale);
  const count = s.fixed_count > 0 ? `${num.format(s.fixed_count)} started + ${num.format(s.new_count)} new` : `${num.format(s.new_count)}`;
  const lessonsWord = s.fixed_count === 0 && s.new_count === 1 ? 'lesson' : 'lessons';
  const finish = s.finish_by ? `, finish by ${longDate(s.finish_by, locale)}` : '';
  const each = s.minutes_mean !== null && s.minutes_max !== null ? `, about ${num.format(s.minutes_mean)} min each (at most ${num.format(s.minutes_max)})` : '';
  const headline = `${count} ${lessonsWord}, one a day${each}${finish}`;
  let extrapolation: string | null = null;
  if (e.extrapolated && e.mean_lesson_kchars !== null) {
    const mean = Number(e.mean_lesson_kchars);
    const basis = Number(e.basis_kchars);
    const fmt = new Intl.NumberFormat(locale, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
    extrapolation =
      mean < basis
        ? `Lessons are ${fmt.format(basis / mean)}× shorter than the measured one; real cost per lesson is likely higher than this range.`
        : `Lessons are ${fmt.format(mean / basis)}× longer than the measured one; real cost per lesson may differ from this range.`;
  }
  const notices: string[] = [];
  if (!s.fits) {
    for (const reason of s.reasons) {
      if (reason === 'minutes')
        notices.push(`A day's lesson would take ${num.format(s.minutes_max ?? 0)} min, more than your ${num.format(p.inputs.minutes_per_day)} min.`);
      else if (reason === 'days')
        notices.push(s.days_left < 1 ? 'The deadline has passed.' : 'This needs more lessons than the days left before the deadline.');
      else notices.push("A day's lesson would be too long to compile.");
    }
  }
  return {
    headline,
    reading: 'Reading time only; labs and review are not included.',
    deadline: s.fixed_count > 0 ? `Deadline kept: ${longDate(s.end, locale)}` : null,
    fills: s.spare_days > 0 ? `This source fills ${num.format(s.new_count)} of ${num.format(s.days_left)} days.` : null,
    course: `Compile: ${usd(e.course_low_usd)}–${usd(e.course_high_usd)} for the whole course, an estimate from one measured lesson`,
    first: e.first_lesson ? `Lesson 1: ${usd(e.first_lesson.low_usd)}–${usd(e.first_lesson.high_usd)}, stops at ${usd(e.run_cap_usd)}` : null,
    extrapolation,
    notices,
  };
};
