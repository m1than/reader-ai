// The study-time form, the live estimate and confirm for one imported material (WEB-PLAN FR-IMPORT-4, -5, -6).
import { Button } from '@acme/ui/button';
import { useEffect, useId, useRef, useState } from 'react';
import { ApiError } from '../../app/api';
import { textLang, useT } from '../../ui/i18n';
import { ChevronIcon, WarningIcon } from '../../ui/icons';
import type { Confirmed, PlanApi, PlanBody, Preview, StoredPlan } from './api';
import en from './messages.en';
import { message } from './messages';
import { check, estimateLines, estimateNotes, estimateRows, localDate, MINUTES, RATE, WEEKS, type Fields } from './model';

export const PREVIEW_DEBOUNCE_MS = 300;

export interface PlanSectionProps {
  api: PlanApi;
  materialId: string;
  stored: StoredPlan | null;
  locale: string;
  today?: () => string;
}

type Field = keyof Fields;

const codeOf = (e: unknown): string => (e instanceof ApiError ? e.code : 'unknown');

function NumberField(props: {
  id: string;
  label: string;
  value: string;
  error: string | undefined;
  shown: boolean;
  onChange: (value: string) => void;
  onBlur: () => void;
  min: number;
  max: number;
  step: number | string;
  inputMode: 'numeric' | 'decimal';
  hint?: string;
}) {
  const errorId = `${props.id}-error`;
  const hintId = `${props.id}-hint`;
  const invalid = props.shown && props.error !== undefined;
  const described = [props.hint ? hintId : null, invalid ? errorId : null].filter(Boolean).join(' ') || undefined;
  return (
    <div className="plan-field">
      <label htmlFor={props.id}>{props.label}</label>
      <input
        id={props.id}
        type="number"
        required
        min={props.min}
        max={props.max}
        step={props.step}
        inputMode={props.inputMode}
        value={props.value}
        aria-invalid={invalid ? 'true' : undefined}
        aria-describedby={described}
        onChange={(e) => props.onChange(e.target.value)}
        onBlur={props.onBlur}
      />
      {props.hint ? (
        <p id={hintId} className="plan-hint">
          {props.hint}
        </p>
      ) : null}
      {invalid ? (
        <p id={errorId} className="plan-error">
          {props.error}
        </p>
      ) : null}
    </div>
  );
}

export function PlanSection({ api, materialId, stored, locale, today = () => localDate(new Date()) }: PlanSectionProps) {
  const ids = useId();
  const t = useT('import/plan', en);
  const [fields, setFields] = useState<Fields>({
    weeks: stored ? String(stored.inputs.total_weeks) : '',
    minutes: stored ? String(stored.inputs.minutes_per_day) : String(MINUTES.default),
    rate: stored ? stored.inputs.rate : RATE.default,
  });
  const [touched, setTouched] = useState<Partial<Record<Field, boolean>>>({});
  // The shown preview together with the exact body that produced it: confirm posts that body, never the live fields.
  const [shown, setShown] = useState<{ preview: Preview; body: PlanBody } | null>(null);
  const preview = shown?.preview ?? null;
  const [loading, setLoading] = useState(false);
  const [pending, setPending] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  // A course with a started lesson already has a run: compile-first would be refused (already_started).
  const [compileFirst, setCompileFirst] = useState(!(stored && stored.started > 0));
  const [sending, setSending] = useState(false);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const [done, setDone] = useState<Confirmed | null>(null);
  const latest = useRef(0);
  const { errors, value } = check(fields, t);

  useEffect(() => {
    if (!value) return undefined;
    const seq = ++latest.current;
    setPending(true);
    const timer = window.setTimeout(() => {
      setPending(false);
      setLoading(true);
      const body: PlanBody = { material_id: materialId, ...value, today: today() };
      api
        .preview(body)
        .then((p) => {
          if (seq !== latest.current) return; // only the latest request's answer is shown
          setShown({ preview: p, body });
          setPreviewError(null);
        })
        .catch((e: unknown) => {
          if (seq !== latest.current) return;
          setPreviewError(codeOf(e));
        })
        .finally(() => {
          if (seq === latest.current) setLoading(false);
        });
    }, PREVIEW_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
    // `value` is derived from `fields`; `refresh` asks again after a plan_changed conflict.
  }, [api, materialId, fields.weeks, fields.minutes, fields.rate, refresh]);

  const set = (field: Field, v: string) => {
    setFields((f) => ({ ...f, [field]: v }));
    setDone(null);
    setConfirmError(null);
  };
  const blur = (field: Field) => setTouched((t) => ({ ...t, [field]: true }));

  const lines = preview ? estimateLines(preview, locale, t) : null;
  // Confirm only the plan on screen: its inputs equal the current valid fields, nothing is pending or failed.
  const current =
    shown !== null &&
    value !== null &&
    shown.body.total_weeks === value.total_weeks &&
    shown.body.minutes_per_day === value.minutes_per_day &&
    shown.body.rate === value.rate;
  const confirmable = current && shown.preview.summary.fits && !pending && !loading && previewError === null;

  const confirm = async () => {
    setTouched({ weeks: true, minutes: true, rate: true });
    if (!confirmable || !shown || sending) return;
    setSending(true);
    setConfirmError(null);
    try {
      setDone(
        await api.confirm({ ...shown.body, plan_sha256: shown.preview.plan_sha256, compile_first: compileFirst }),
      );
    } catch (e) {
      const code = codeOf(e);
      setConfirmError(code);
      if (code === 'plan_changed') setRefresh((n) => n + 1);
    } finally {
      setSending(false);
    }
  };

  const suggest = preview?.summary.suggest ?? null;
  // FR-IMPORT-6 (design pass): why "Create the course" is disabled, the first reason that applies.
  const blocked: string | null = sending
    ? t('why.creating')
    : confirmable
      ? null
      : fields.weeks.trim() === ''
        ? t('why.weeks')
        : value === null
          ? t('why.fields')
          : previewError !== null && !pending && !loading
            ? t('why.failed')
            : pending || loading || !current
              ? t('why.updating')
              : t('why.unfit');
  return (
    <section className="plan" aria-labelledby={`${ids}-h`}>
      <h2 id={`${ids}-h`} tabIndex={-1}>
        {t('plan.heading')}
      </h2>
      {stored && stored.started > 0 ? <p className="muted">{t('plan.started', { count: stored.started, n: String(stored.started) })}</p> : null}
      <form
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          void confirm();
        }}
      >
        <div className="plan-fields">
          <NumberField
            id={`${ids}-weeks`}
            label={t('plan.weeks')}
            value={fields.weeks}
            error={errors.weeks}
            shown={touched.weeks === true}
            onChange={(v) => set('weeks', v)}
            onBlur={() => blur('weeks')}
            min={WEEKS.min}
            max={WEEKS.max}
            step={1}
            inputMode="numeric"
          />
          <NumberField
            id={`${ids}-minutes`}
            label={t('plan.minutes')}
            value={fields.minutes}
            error={errors.minutes}
            shown={touched.minutes === true}
            onChange={(v) => set('minutes', v)}
            onBlur={() => blur('minutes')}
            min={MINUTES.min}
            max={MINUTES.max}
            step={MINUTES.step}
            inputMode="numeric"
          />
        </div>
        <details className="plan-pace">
          <summary>
            <ChevronIcon className="disclosure-chevron" />
            {t('plan.pace')}
          </summary>
          <NumberField
            id={`${ids}-rate`}
            label={t('plan.rate')}
            value={fields.rate}
            error={errors.rate}
            shown={touched.rate === true}
            onChange={(v) => set('rate', v)}
            onBlur={() => blur('rate')}
            min={RATE.min}
            max={RATE.max}
            step="0.01"
            inputMode="decimal"
            hint={t('plan.rate_hint')}
          />
        </details>

        <div className={`plan-estimate${loading && preview ? ' is-updating' : ''}`}>
          <p role="status" className="visually-hidden">
            {loading ? t('plan.updating') : lines ? t('plan.updated', { headline: lines.headline }) : ''}
          </p>
          {previewError ? (
            <p className="plan-error">{message(previewError, t)}</p>
          ) : lines && preview ? (
            <>
              {lines.notices.length > 0 ? (
                <div className="plan-notice callout" data-tone="warning">
                  <WarningIcon className="callout-icon" />
                  {lines.notices.map((n) => (
                    <p key={n}>{n}</p>
                  ))}
                  <div className="state-actions">
                    {suggest?.minutes_per_day != null ? (
                      <Button variant="outline" color="neutral" size="sm" onClick={() => set('minutes', String(suggest.minutes_per_day))}>
                        {t('plan.use_minutes', { n: String(suggest.minutes_per_day) })}
                      </Button>
                    ) : null}
                    {suggest?.total_weeks != null ? (
                      <Button variant="outline" color="neutral" size="sm" onClick={() => set('weeks', String(suggest.total_weeks))}>
                        {t('plan.use_weeks', { n: String(suggest.total_weeks) })}
                      </Button>
                    ) : null}
                    {suggest?.minutes_per_day == null && suggest?.total_weeks == null ? <p>{t('plan.choose_more')}</p> : null}
                  </div>
                </div>
              ) : null}
              <dl className={lines.notices.length > 0 ? 'plan-figures is-unfit' : 'plan-figures'}>
                {estimateRows(preview, locale, t).map(([term, value]) => (
                  <div key={term} className="plan-figure">
                    <dt>{term}</dt>
                    <dd>{value}</dd>
                  </div>
                ))}
              </dl>
              <div className="plan-notes">
                {estimateNotes(lines, t).map((n) => (
                  <p key={n}>{n}</p>
                ))}
              </div>
              <details className="plan-lessons">
                <summary>
                  <ChevronIcon className="disclosure-chevron" />
                  {t('plan.lessons')}
                </summary>
                <ol>
                  {preview?.lessons.map((l) => (
                    <li key={l.id}>
                      <span className="plan-lesson-title" lang={textLang(l.title)}>
                        {l.title}
                      </span>
                      <span className="plan-lesson-meta">
                        {t('plan.lesson_meta', {
                          pages:
                            l.pages[0] === l.pages[1]
                              ? t('plan.page', { page: String(l.pages[0]) })
                              : t('plan.pages', { from: String(l.pages[0]), to: String(l.pages[1]) }),
                          minutes: String(l.minutes),
                        })}
                        {l.fixed ? t('plan.fixed') : ''}
                      </span>
                    </li>
                  ))}
                </ol>
              </details>
            </>
          ) : (
            <p className="muted">{t('plan.enter_weeks')}</p>
          )}
        </div>

        <div className="plan-confirm">
          {stored && stored.started > 0 ? null : (
            <label className="plan-check">
              <input type="checkbox" checked={compileFirst} onChange={(e) => setCompileFirst(e.target.checked)} />
              {preview?.estimate.first_lesson
                ? t('plan.compile_first_cost', {
                    low: `$${preview.estimate.first_lesson.low_usd}`,
                    high: `$${preview.estimate.first_lesson.high_usd}`,
                    cap: `$${preview.estimate.run_cap_usd}`,
                  })
                : t('plan.compile_first')}
            </label>
          )}
          <Button type="submit" disabled={!confirmable || sending} aria-describedby={blocked ? `${ids}-why` : undefined}>
            {t('plan.create')}
          </Button>
          {blocked ? (
            <p id={`${ids}-why`} className="plan-why">
              {blocked}
            </p>
          ) : null}
          {confirmError ? <p className="plan-error">{message(confirmError, t)}</p> : null}
          {done ? (
            <div role="status" className="plan-done">
              <p>{t('done.created', { count: done.lessons.length, n: String(done.lessons.length) })}</p>
              {done.compile && 'run_id' in done.compile ? <p>{t('done.compiling')}</p> : null}
              {done.compile && 'error' in done.compile ? <p>{message(done.compile.error, t)}</p> : null}
              <p>
                <a href="/">{t('done.list')}</a>
              </p>
            </div>
          ) : null}
        </div>
      </form>
    </section>
  );
}
