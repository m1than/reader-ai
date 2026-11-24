// The "Source freshness" card (WEB-FRESH): the bibliography the check searches for (editable, with confirmation of a
// scan-read one), Run with the server's estimate and cap, progress while a check runs, the verdict badge, reasons,
// flagged claims with page links and recommendations with safe links. Report strings are text nodes only
// (INV-FRESHUI-2); web links follow the link rule of links.ts (INV-FRESHUI-1). Money is shown as the server wrote it.
// Interface copy and the server's enums come from this area's catalogue (WEB-UI FR-UI-30); an unknown enum value is
// shown as received.
import { Button } from '@acme/ui/button';
import { Skeleton } from '@acme/ui/skeleton';
import { Fragment, useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { textLang, useT } from '../ui/i18n';
import { estimateOf, type Estimate, type FreshApi, type Freshness, type LinkRef, type Report } from './api';
import { FIELDS, fieldLabel, fieldMessage, formField, fromForm, toForm, type Field, type FormValues, type FreshT } from './bibliography';
import { EXTERNAL, pageHref, safeHref } from './links';
import en from './messages.en';
import './fresh.css';

/** Poll interval while a check runs (chosen: a check takes minutes; not measured). */
export const POLL_MS = 3000;

const VERDICTS = {
  current: { className: 'fresh-badge fresh-badge-current', icon: 'M3 8.5l3 3 7-7' },
  partly_outdated: { className: 'fresh-badge fresh-badge-partly', icon: 'M8 3v6M8 12v1' },
  outdated: { className: 'fresh-badge fresh-badge-outdated', icon: 'M4 4l8 8M12 4l-8 8' },
} as const;

const DROPPED = ['source_unknown', 'url_unsafe'] as const;
type Dropped = (typeof DROPPED)[number];

const dailyMessage = (t: FreshT, cap: string, spent?: unknown) =>
  typeof spent === 'string' && /^\d+(\.\d+)?$/.test(spent) ? t('daily.limit_spent', { cap, spent }) : t('daily.limit', { cap });

const reportMessage = (t: FreshT, code: string | null, data: Freshness): string => {
  switch (code) {
    case 'no_results':
      return t('report.no_results');
    case 'daily_budget_exhausted':
      return dailyMessage(t, data.daily_cap_usd);
    case 'budget_exhausted':
      return t('report.budget_exhausted', { cap: data.estimate.cap_usd });
    case 'output_invalid':
      return t('report.output_invalid');
    default:
      return t('report.failed');
  }
};

const progressText = (t: FreshT, report: Report): string => {
  const p = report.progress;
  if (p?.stage === 'search') return t('progress.search', { done: p.done, total: p.total });
  if (p?.stage === 'report') return t('progress.report');
  return t('progress.scan');
};

/** A web link by the link rule, else its title and URL as text. */
function WebLink({ link, t }: { link: LinkRef; t: FreshT }) {
  const href = safeHref(link.url, link.host);
  if (href === null)
    return (
      <span>
        {link.title} <span className="fresh-url">{link.url}</span>
      </span>
    );
  return (
    <span>
      <a href={href} rel={EXTERNAL.rel} target={EXTERNAL.target}>
        {link.title}
        <span className="visually-hidden">{t('link.new_tab')}</span>
      </a>{' '}
      <code className="fresh-host">{link.host}</code>
    </span>
  );
}

function Citations({ links, t }: { links: LinkRef[]; t: FreshT }) {
  if (links.length === 0) return null;
  return (
    <span className="fresh-cites">
      {t('cites.label')}
      {links.map((l, i) => (
        <Fragment key={i}>
          {i > 0 ? '; ' : ''}
          <WebLink link={l} t={t} />
        </Fragment>
      ))}
    </span>
  );
}

function Badge({ verdict, t }: { verdict: keyof typeof VERDICTS; t: FreshT }) {
  const v = VERDICTS[verdict];
  return (
    <p className={v.className}>
      <svg aria-hidden="true" focusable="false" viewBox="0 0 16 16" width="16" height="16">
        <path d={v.icon} fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
      </svg>
      <span>{t.enum('verdict', verdict)}</span>
    </p>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  const id = useId();
  return (
    <section className="fresh-section" aria-labelledby={id}>
      <h2 id={id}>{title}</h2>
      {children}
    </section>
  );
}

interface BibliographyProps {
  data: Freshness;
  materialId: string;
  api: FreshApi;
  confirmed: boolean;
  onConfirm: (v: boolean) => void;
  onSaved: () => void;
  t: FreshT;
}

function BibliographySection({ data, materialId, api, confirmed, onConfirm, onSaved, t }: BibliographyProps) {
  const base = useId();
  const [editing, setEditing] = useState(false);
  const [values, setValues] = useState<FormValues>(() => toForm(null));
  const [errors, setErrors] = useState<Partial<Record<Field, string>>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const shown = data.bibliography ?? data.scan_bibliography;
  const fromOwner = data.bibliography?.source === 'owner';
  const needsConfirm = data.scan_bibliography !== null && !fromOwner;

  const edit = () => {
    setValues(toForm(shown));
    setErrors({});
    setFormError(null);
    setEditing(true);
  };

  const save = async () => {
    const { body, errors: found } = fromForm(values, { original_authors: shown?.original_authors ?? null }, new Date().getUTCFullYear(), t);
    setErrors(found);
    setFormError(null);
    if (Object.keys(found).length > 0) return;
    setSaving(true);
    try {
      await api.saveBibliography(materialId, body);
      setEditing(false);
      onSaved();
    } catch (e) {
      const err = e as { code?: unknown; body?: { field?: unknown } };
      const field = err.code === 'bibliography_invalid' ? formField(err.body?.field) : null;
      if (field) setErrors({ [field]: fieldMessage(field, t) });
      else setFormError(t('bib.save_failed'));
    } finally {
      setSaving(false);
    }
  };

  // The book's own details are content: their language is the Cyrillic rule's (WEB-UI FR-UI-32).
  const rows: Array<[string, string | number | null | undefined, boolean]> = shown
    ? [
        [t('field.title'), shown.title, true],
        [t('row.authors'), shown.authors.join(', '), true],
        [t('field.edition'), shown.edition, true],
        [t('field.year'), shown.year, false],
        [t('field.publisher'), shown.publisher, true],
        [t('field.isbn'), shown.isbn, false],
        [t('field.original_title'), shown.original_title, true],
        [t('field.original_edition'), shown.original_edition, true],
        [t('field.original_year'), shown.original_year, false],
      ]
    : [];

  return (
    <Section title={t('bib.heading')}>
      {editing ? (
        <form
          className="fresh-form"
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          {FIELDS.map(({ field, multiline }) => {
            const id = `${base}-${field}`;
            const errorId = `${id}-error`;
            const invalid = errors[field] !== undefined;
            const common = {
              id,
              value: values[field],
              'aria-invalid': invalid,
              ...(invalid ? { 'aria-describedby': errorId } : {}),
              onChange: (e: { target: { value: string } }) => setValues((v) => ({ ...v, [field]: e.target.value })),
            };
            return (
              <div className="fresh-field" key={field}>
                <label htmlFor={id}>{fieldLabel(field, t)}</label>
                {multiline ? <textarea rows={3} {...common} /> : <input type="text" {...common} />}
                {invalid && (
                  <p id={errorId} className="fresh-field-error">
                    {errors[field]}
                  </p>
                )}
              </div>
            );
          })}
          {formError && <p role="alert">{formError}</p>}
          <div className="state-actions">
            <Button type="submit" disabled={saving}>
              {t('save')}
            </Button>
            <Button type="button" variant="ghost" color="neutral" onClick={() => setEditing(false)}>
              {t('cancel')}
            </Button>
          </div>
        </form>
      ) : (
        <>
          {shown ? (
            <dl className="fresh-bib">
              {rows
                .filter(([, v]) => v !== null && v !== undefined && v !== '')
                .map(([k, v, text]) => (
                  <div key={k}>
                    <dt>{k}</dt>
                    <dd lang={text ? textLang(String(v)) : undefined}>{v}</dd>
                  </div>
                ))}
            </dl>
          ) : (
            <p className="muted">{t('bib.none')}</p>
          )}
          {shown && <p className="muted">{fromOwner ? t('bib.from_owner') : t('bib.from_scan')}</p>}
          {needsConfirm && (
            <div className="fresh-confirm">
              <p>{t('bib.confirm_hint')}</p>
              <label>
                <input type="checkbox" checked={confirmed} onChange={(e) => onConfirm(e.target.checked)} /> {t('bib.confirm')}
              </label>
            </div>
          )}
          <div className="state-actions">
            <Button variant="outline" color="neutral" onClick={edit}>
              {t('edit')}
            </Button>
          </div>
        </>
      )}
    </Section>
  );
}

function ReportSections({ report, materialId, t }: { report: Report; materialId: string; t: FreshT }) {
  const status = new Map((report.claim_status ?? []).map((s) => [s.id, s]));
  const topics = new Map((report.topics ?? []).map((tp) => [tp.id, tp.title]));
  const dropped = Object.entries(report.dropped ?? {}).filter((e): e is [Dropped, number] => (DROPPED as readonly string[]).includes(e[0]) && e[1] > 0);
  return (
    <>
      {report.reasons && report.reasons.length > 0 && (
        <Section title={t('report.reasons')}>
          <ul className="fresh-list">
            {report.reasons.map((r, i) => (
              <li key={i}>
                <span lang={textLang(r.text)}>{r.text}</span>
                <Citations links={r.citations} t={t} />
              </li>
            ))}
          </ul>
        </Section>
      )}
      {report.claims && report.claims.length > 0 && (
        <Section title={t('report.claims')}>
          <ul className="fresh-list">
            {report.claims.map((c) => {
              const s = status.get(c.id);
              return (
                <li key={c.id}>
                  <p>
                    <span className="fresh-subject" lang={textLang(c.subject)}>
                      {c.subject}
                    </span>
                    {t('claim.meta', { kind: t.enum('kind', c.kind), confidence: t.enum('confidence', c.confidence) })}
                    <a href={pageHref(materialId, c.page, c.cell)}>{t('claim.page', { page: String(c.page) })}</a>
                  </p>
                  {s && (
                    <p className="muted">
                      {t('claim.web', { status: t.enum('status', s.status) })}
                      {s.note !== '' && ` — ${s.note}`}
                      <Citations links={s.citations} t={t} />
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
        </Section>
      )}
      {report.recommendations && report.recommendations.length > 0 && (
        <Section title={t('report.recommendations')}>
          <ul className="fresh-recs">
            {report.recommendations.map((r, i) => {
              const href = safeHref(r.url, r.host);
              const covers = r.covers.map((id) => topics.get(id)).filter((tp): tp is string => tp !== undefined);
              return (
                <li key={i} className="fresh-rec">
                  <h3 lang={textLang(r.title)}>
                    {href === null ? (
                      r.title
                    ) : (
                      <a href={href} rel={EXTERNAL.rel} target={EXTERNAL.target}>
                        {r.title}
                        <span className="visually-hidden">{t('link.new_tab')}</span>
                      </a>
                    )}
                  </h3>
                  <p>
                    <code className="fresh-host">{r.host}</code>
                  </p>
                  {href === null && <p className="fresh-url">{r.url}</p>}
                  <p className="muted">
                    {t.enum('rec_kind', r.kind)}
                    {r.date !== null && t('rec.date', { date: r.date })}
                  </p>
                  <p lang={textLang(r.why)}>{r.why}</p>
                  {covers.length > 0 && <p className="muted">{t('rec.covers', { topics: covers.join(', ') })}</p>}
                </li>
              );
            })}
          </ul>
        </Section>
      )}
      {dropped.length > 0 && (
        <p className="muted">{t('dropped.line', { list: dropped.map(([k, n]) => t(`dropped.${k}`, { count: n })).join('; ') })}</p>
      )}
    </>
  );
}

export interface FreshnessCardProps {
  materialId: string;
  api: FreshApi;
  /** Poll interval while a check runs; tests pass a short one. */
  pollMs?: number;
}

export function FreshnessCard({ materialId, api, pollMs = POLL_MS }: FreshnessCardProps) {
  const t = useT('fresh', en);
  const [data, setData] = useState<Freshness | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [estimate, setEstimate] = useState<Estimate | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [posting, setPosting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const live = useRef(true);

  const reload = useCallback(async () => {
    try {
      const d = await api.get(materialId);
      if (!live.current) return;
      setData(d);
      setLoadFailed(false);
    } catch {
      if (live.current) setLoadFailed(true);
    }
  }, [api, materialId]);

  useEffect(() => {
    live.current = true;
    void reload();
    return () => {
      live.current = false;
    };
  }, [reload]);

  const running = data?.report?.status === 'running';
  useEffect(() => {
    if (!running) return;
    const timer = window.setTimeout(() => void reload(), pollMs);
    return () => window.clearTimeout(timer);
  }, [running, data, pollMs, reload]);

  if (data === null) {
    if (loadFailed)
      return (
        <div className="fresh">
          <div className="error state-actions">
            <p>{t('card.load_failed')}</p>
            <Button
              variant="outline"
              color="neutral"
              onClick={() => {
                setLoadFailed(false);
                void reload();
              }}
            >
              {t('retry')}
            </Button>
          </div>
        </div>
      );
    return (
      <div className="fresh">
        <p role="status" className="visually-hidden">
          {t('card.loading')}
        </p>
        <div aria-hidden="true">
          <Skeleton.Root>
            <Skeleton.Item variant="text" width="50%" />
            <Skeleton.Item variant="text" width="80%" />
            <Skeleton.Item variant="text" width="65%" />
          </Skeleton.Root>
        </div>
      </div>
    );
  }

  const report = data.report;
  const shownEstimate = estimate ?? data.estimate;
  const confirmable = data.scan_bibliography !== null && data.bibliography?.source !== 'owner';

  const run = async () => {
    setPosting(true);
    setNotice(null);
    const body =
      confirmable && confirmed && data.scan_bibliography
        ? { high_usd: shownEstimate.high_usd, bibliography_sha256: data.scan_bibliography.sha256 }
        : { high_usd: shownEstimate.high_usd };
    try {
      await api.run(materialId, body);
      setConfirming(false);
      setEstimate(null);
      await reload();
    } catch (e) {
      const err = e as { code?: unknown; body?: Record<string, unknown> };
      const code = typeof err.code === 'string' ? err.code : '';
      if (code === 'estimate_changed') {
        const next = estimateOf(err.body?.estimate);
        if (next) setEstimate(next);
        setNotice(t('run.estimate_changed'));
      } else if (code === 'freshness_running') {
        setConfirming(false);
        await reload();
      } else if (code === 'bibliography_unconfirmed' || code === 'nothing_to_confirm') {
        setConfirming(false);
        setConfirmed(false);
        await reload();
        setNotice(t('run.details_changed'));
      } else if (code === 'daily_budget_exhausted') {
        setConfirming(false);
        const cap = typeof err.body?.daily_cap_usd === 'string' ? err.body.daily_cap_usd : data.daily_cap_usd;
        setNotice(dailyMessage(t, cap, err.body?.spent_usd));
      } else {
        setConfirming(false);
        setNotice(t('run.failed'));
      }
    } finally {
      setPosting(false);
    }
  };

  const verdict = report?.status === 'done' ? report.verdict : null;

  return (
    <div className="fresh">
      <BibliographySection
        data={data}
        materialId={materialId}
        api={api}
        confirmed={confirmed}
        onConfirm={setConfirmed}
        onSaved={() => {
          setConfirmed(false);
          void reload();
        }}
        t={t}
      />
      <Section title={t('card.heading')}>
        {report === null && <p className="muted">{t('card.none')}</p>}
        {running && report && (
          <p role="status" className="fresh-progress">
            {progressText(t, report)}
          </p>
        )}
        {verdict && <Badge verdict={verdict} t={t} />}
        {report?.status === 'done' && <p className="muted">{t('card.checked', { date: (report.finished_at ?? report.created_at).slice(0, 10) })}</p>}
        {report?.status === 'done' && verdict === null && <p>{t('card.needs_bib')}</p>}
        {report?.status === 'done' && data.stale && (
          <p className="fresh-stale-line">
            <span className="fresh-stale">{t('card.stale')}</span>
            {data.stale_reason && <span>{` — ${t.enum('stale_reason', data.stale_reason)}`}</span>}
          </p>
        )}
        {(report?.status === 'failed' || report?.status === 'budget_refused') && <p>{reportMessage(t, report.error, data)}</p>}
        {notice && <p role="alert">{notice}</p>}
        {!running &&
          (confirming ? (
            <div className="fresh-estimate">
              <p>{t('card.estimate', { low: shownEstimate.low_usd, high: shownEstimate.high_usd, cap: shownEstimate.cap_usd })}</p>
              <div className="state-actions">
                <Button disabled={posting} onClick={() => void run()}>
                  {t('card.run_up_to', { high: shownEstimate.high_usd })}
                </Button>
                <Button variant="ghost" color="neutral" onClick={() => setConfirming(false)}>
                  {t('cancel')}
                </Button>
              </div>
            </div>
          ) : (
            <div className="state-actions">
              <Button
                onClick={() => {
                  setNotice(null);
                  setConfirming(true);
                }}
              >
                {report === null ? t('card.run') : t('card.run_again')}
              </Button>
            </div>
          ))}
      </Section>
      {report?.status === 'done' && <ReportSections report={report} materialId={materialId} t={t} />}
    </div>
  );
}
