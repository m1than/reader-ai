// The "Source freshness" card (WEB-FRESH): the bibliography the check searches for (editable, with confirmation of a
// scan-read one), Run with the server's estimate and cap, progress while a check runs, the verdict badge, reasons,
// flagged claims with page links and recommendations with safe links. Report strings are text nodes only
// (INV-FRESHUI-2); web links follow the link rule of links.ts (INV-FRESHUI-1). Money is shown as the server wrote it.
import { Button } from '@acme/ui/button';
import { Skeleton } from '@acme/ui/skeleton';
import { Fragment, useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { estimateOf, type Estimate, type FreshApi, type Freshness, type LinkRef, type Report } from './api';
import { FIELDS, formField, fromForm, MESSAGES, toForm, type Field, type FormValues } from './bibliography';
import { EXTERNAL, pageHref, safeHref } from './links';
import './fresh.css';

/** Poll interval while a check runs (chosen: a check takes minutes; not measured). */
export const POLL_MS = 3000;

const VERDICTS = {
  current: { label: 'Current', className: 'fresh-badge fresh-badge-current', icon: 'M3 8.5l3 3 7-7' },
  partly_outdated: { label: 'Partly outdated', className: 'fresh-badge fresh-badge-partly', icon: 'M8 3v6M8 12v1' },
  outdated: { label: 'Outdated', className: 'fresh-badge fresh-badge-outdated', icon: 'M4 4l8 8M12 4l-8 8' },
} as const;

const STALE = { ttl: 'older than 90 days', reingested: 'the material was re-imported', bibliography_changed: 'the details changed' } as const;

const REC_KINDS = { book_edition: 'New edition', book: 'Book', official_docs: 'Official documentation', course: 'Course', other: 'Other' } as const;

const DROPPED: Readonly<Record<string, string>> = {
  source_unknown: "not found in this check's search results",
  url_unsafe: 'not a web link',
};

const dailyMessage = (cap: string, spent?: unknown) =>
  `Daily limit for freshness checks reached ($${cap}); try again tomorrow.${typeof spent === 'string' && /^\d+(\.\d+)?$/.test(spent) ? ` Spent today: $${spent}.` : ''}`;

const reportMessage = (code: string | null, data: Freshness): string => {
  switch (code) {
    case 'no_results':
      return 'The web searches returned no results.';
    case 'daily_budget_exhausted':
      return dailyMessage(data.daily_cap_usd);
    case 'budget_exhausted':
      return `This check reached its $${data.estimate.cap_usd} cap and stopped.`;
    case 'output_invalid':
      return "The model's answer could not be read.";
    default:
      return 'The check failed.';
  }
};

const progressText = (report: Report): string => {
  const p = report.progress;
  if (p?.stage === 'search') return `Searching the web (${p.done} of ${p.total})`;
  if (p?.stage === 'report') return 'Writing the report';
  return 'Reading the book';
};

/** A web link by the link rule, else its title and URL as text. */
function WebLink({ link }: { link: LinkRef }) {
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
        <span className="visually-hidden"> (opens in a new tab)</span>
      </a>{' '}
      <code className="fresh-host">{link.host}</code>
    </span>
  );
}

function Citations({ links }: { links: LinkRef[] }) {
  if (links.length === 0) return null;
  return (
    <span className="fresh-cites">
      {' Sources: '}
      {links.map((l, i) => (
        <Fragment key={i}>
          {i > 0 ? '; ' : ''}
          <WebLink link={l} />
        </Fragment>
      ))}
    </span>
  );
}

function Badge({ verdict }: { verdict: keyof typeof VERDICTS }) {
  const v = VERDICTS[verdict];
  return (
    <p className={v.className}>
      <svg aria-hidden="true" focusable="false" viewBox="0 0 16 16" width="16" height="16">
        <path d={v.icon} fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
      </svg>
      <span>{v.label}</span>
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
}

function BibliographySection({ data, materialId, api, confirmed, onConfirm, onSaved }: BibliographyProps) {
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
    const { body, errors: found } = fromForm(values, { original_authors: shown?.original_authors ?? null }, new Date().getUTCFullYear());
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
      if (field) setErrors({ [field]: MESSAGES[field] });
      else setFormError('These details could not be saved. Check them and try again.');
    } finally {
      setSaving(false);
    }
  };

  const rows: Array<[string, string | number | null | undefined]> = shown
    ? [
        ['Title', shown.title],
        ['Authors', shown.authors.join(', ')],
        ['Edition', shown.edition],
        ['Year', shown.year],
        ['Publisher', shown.publisher],
        ['ISBN', shown.isbn],
        ['Original title', shown.original_title],
        ['Original edition', shown.original_edition],
        ['Original year', shown.original_year],
      ]
    : [];

  return (
    <Section title="Bibliography">
      {editing ? (
        <form
          className="fresh-form"
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          {FIELDS.map(({ field, label, multiline }) => {
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
                <label htmlFor={id}>{label}</label>
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
              Save
            </Button>
            <Button type="button" variant="ghost" color="neutral" onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </div>
        </form>
      ) : (
        <>
          {shown ? (
            <dl className="fresh-bib">
              {rows
                .filter(([, v]) => v !== null && v !== undefined && v !== '')
                .map(([k, v]) => (
                  <div key={k}>
                    <dt>{k}</dt>
                    <dd>{v}</dd>
                  </div>
                ))}
            </dl>
          ) : (
            <p className="muted">No details yet: enter them, or run a check to read them from the book's first pages.</p>
          )}
          {shown && <p className="muted">Source: {fromOwner ? 'from you' : "read from the book's first pages"}</p>}
          {needsConfirm && (
            <div className="fresh-confirm">
              <p>Check these details before the web search: the check searches the web only for details you confirm or enter.</p>
              <label>
                <input type="checkbox" checked={confirmed} onChange={(e) => onConfirm(e.target.checked)} /> These details are right
              </label>
            </div>
          )}
          <div className="state-actions">
            <Button variant="outline" color="neutral" onClick={edit}>
              Edit
            </Button>
          </div>
        </>
      )}
    </Section>
  );
}

function ReportSections({ report, materialId }: { report: Report; materialId: string }) {
  const status = new Map((report.claim_status ?? []).map((s) => [s.id, s]));
  const topics = new Map((report.topics ?? []).map((t) => [t.id, t.title]));
  const dropped = Object.entries(report.dropped ?? {}).filter(([k, n]) => k in DROPPED && n > 0);
  return (
    <>
      {report.reasons && report.reasons.length > 0 && (
        <Section title="Reasons">
          <ul className="fresh-list">
            {report.reasons.map((r, i) => (
              <li key={i}>
                {r.text}
                <Citations links={r.citations} />
              </li>
            ))}
          </ul>
        </Section>
      )}
      {report.claims && report.claims.length > 0 && (
        <Section title="Flagged claims">
          <ul className="fresh-list">
            {report.claims.map((c) => {
              const s = status.get(c.id);
              return (
                <li key={c.id}>
                  <p>
                    <span className="fresh-subject">{c.subject}</span>
                    {` (${c.kind}, ${c.confidence} confidence), `}
                    <a href={pageHref(materialId, c.page, c.cell)}>p. {c.page}</a>
                  </p>
                  {s && (
                    <p className="muted">
                      {`On the web: ${s.status}`}
                      {s.note !== '' && ` — ${s.note}`}
                      <Citations links={s.citations} />
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
        </Section>
      )}
      {report.recommendations && report.recommendations.length > 0 && (
        <Section title="Recommendations">
          <ul className="fresh-recs">
            {report.recommendations.map((r, i) => {
              const href = safeHref(r.url, r.host);
              const covers = r.covers.map((id) => topics.get(id)).filter((t): t is string => t !== undefined);
              return (
                <li key={i} className="fresh-rec">
                  <h3>
                    {href === null ? (
                      r.title
                    ) : (
                      <a href={href} rel={EXTERNAL.rel} target={EXTERNAL.target}>
                        {r.title}
                        <span className="visually-hidden"> (opens in a new tab)</span>
                      </a>
                    )}
                  </h3>
                  <p>
                    <code className="fresh-host">{r.host}</code>
                  </p>
                  {href === null && <p className="fresh-url">{r.url}</p>}
                  <p className="muted">
                    {REC_KINDS[r.kind]}
                    {r.date !== null && ` · ${r.date} (as reported)`}
                  </p>
                  <p>{r.why}</p>
                  {covers.length > 0 && <p className="muted">Covers: {covers.join(', ')}</p>}
                </li>
              );
            })}
          </ul>
        </Section>
      )}
      {dropped.length > 0 && (
        <p className="muted">Removed from this report: {dropped.map(([k, n]) => `${n} ${DROPPED[k]}`).join('; ')}.</p>
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
    const t = window.setTimeout(() => void reload(), pollMs);
    return () => window.clearTimeout(t);
  }, [running, data, pollMs, reload]);

  if (data === null) {
    if (loadFailed)
      return (
        <div className="fresh">
          <div className="error state-actions">
            <p>The freshness check could not be loaded.</p>
            <Button
              variant="outline"
              color="neutral"
              onClick={() => {
                setLoadFailed(false);
                void reload();
              }}
            >
              Retry
            </Button>
          </div>
        </div>
      );
    return (
      <div className="fresh">
        <p role="status" className="visually-hidden">
          Loading the freshness check
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
        setNotice('The estimate changed. Check it and run again.');
      } else if (code === 'freshness_running') {
        setConfirming(false);
        await reload();
      } else if (code === 'bibliography_unconfirmed' || code === 'nothing_to_confirm') {
        setConfirming(false);
        setConfirmed(false);
        await reload();
        setNotice('The details changed: check the details again before the web search.');
      } else if (code === 'daily_budget_exhausted') {
        setConfirming(false);
        const cap = typeof err.body?.daily_cap_usd === 'string' ? err.body.daily_cap_usd : data.daily_cap_usd;
        setNotice(dailyMessage(cap, err.body?.spent_usd));
      } else {
        setConfirming(false);
        setNotice('The check could not be started. Try again.');
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
      />
      <Section title="Freshness check">
        {report === null && <p className="muted">No check yet.</p>}
        {running && report && (
          <p role="status" className="fresh-progress">
            {progressText(report)}
          </p>
        )}
        {verdict && <Badge verdict={verdict} />}
        {report?.status === 'done' && (
          <p className="muted">Checked {(report.finished_at ?? report.created_at).slice(0, 10)}</p>
        )}
        {report?.status === 'done' && verdict === null && (
          <p>The bibliography needs a check before the web search: confirm or correct the details above and run again.</p>
        )}
        {report?.status === 'done' && data.stale && (
          <p className="fresh-stale-line">
            <span className="fresh-stale">Stale</span>
            {data.stale_reason && <span>{` — ${STALE[data.stale_reason]}`}</span>}
          </p>
        )}
        {(report?.status === 'failed' || report?.status === 'budget_refused') && <p>{reportMessage(report.error, data)}</p>}
        {notice && <p role="alert">{notice}</p>}
        {!running &&
          (confirming ? (
            <div className="fresh-estimate">
              <p>{`About $${shownEstimate.low_usd}–$${shownEstimate.high_usd}, at most $${shownEstimate.cap_usd}`}</p>
              <div className="state-actions">
                <Button disabled={posting} onClick={() => void run()}>
                  {`Run for up to $${shownEstimate.high_usd}`}
                </Button>
                <Button variant="ghost" color="neutral" onClick={() => setConfirming(false)}>
                  Cancel
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
                {report === null ? 'Run check' : 'Run again'}
              </Button>
            </div>
          ))}
      </Section>
      {report?.status === 'done' && <ReportSections report={report} materialId={materialId} />}
    </div>
  );
}
