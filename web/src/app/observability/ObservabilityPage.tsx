// WEB-UI FR-UI-18 (SRV-OBS FR-OBS-10, -16, -17, -19): the owner's observability page. Five views of the last 7
// days from GET /api/obs/summary: compile runs, tutor latency and cost, failures, ingest throughput and cost per
// lesson. Every trace id links to the traces dashboard on https://localhost:8443 and to Jaeger on
// https://localhost:8444 (owner only, behind the gate); the saved dashboard views are listed. Values render as text.
// FR-UI-30: every interface string comes from the `app/observability` catalogue; ids, codes, span, kind, stage and
// model names, service names and URLs are shown as received (the declared exceptions).
import { Button } from '@acme/ui/button';
import { useQuery } from '@tanstack/react-query';
import { useMemo, useState, type ReactNode } from 'react';
import { interleave, useT, type T } from '../../ui/i18n';
import { WarningIcon } from '../../ui/icons';
import { StateBlock } from '../../ui/StateBlock';
import { useServices } from '../context';
import {
  createObsApi,
  DASHBOARDS,
  jaegerUrl,
  LABEL_REASONS,
  OBS_KEY,
  SUMMARY_DAYS,
  traceUrl,
  type LabelBody,
  type LabelReason,
  type ObsApi,
  type ObsSummary,
  type PostResult,
} from './api';
import en from './messages.en';

type Tr = T<typeof en>;
const useObsT = (): Tr => useT('app/observability', en);

// WEB-UI FR-UI-33 (SRV-OBS FR-OBS-24): the owner's good/bad label on a lesson or an answer; the reply's revision
// and mirror (or its error code) are shown as text. Postgres is the record; MLflow gets a mirror.
function LabelControl({ target, id }: { target: LabelBody['target']; id: string }) {
  const t = useObsT();
  const { obs } = useServices();
  const [reason, setReason] = useState<'' | LabelReason>('');
  const [note, setNote] = useState<string | null>(null);
  const send = async (ok: boolean) => {
    const body: LabelBody = reason ? { target, id, ok, reason } : { target, id, ok };
    let r: PostResult;
    try {
      r = await (obs as ObsApi).label(body);
    } catch {
      setNote(t('network'));
      return;
    }
    if (r.status === 200) setNote(t('label.saved', { revision: String(r.body.revision), mirror: t.enum('mirror', String(r.body.mirror)) }));
    else if (r.status === 404) setNote(t('label.not_found'));
    else setNote(typeof r.body.error === 'string' ? r.body.error : `http_${r.status}`);
  };
  return (
    <span className="obs-label">
      <label>
        <span className="visually-hidden">{t('label.reason')}</span>
        <select aria-label={t('label.reason')} value={reason} onChange={(e) => setReason(e.target.value as '' | LabelReason)}>
          <option value="">{t('label.no_reason')}</option>
          {LABEL_REASONS.map((r) => (
            <option key={r} value={r}>
              {t.enum('reason', r)}
            </option>
          ))}
        </select>
      </label>{' '}
      <button type="button" onClick={() => void send(true)}>
        {t('label.good')}
      </button>{' '}
      <button type="button" onClick={() => void send(false)}>
        {t('label.bad')}
      </button>
      {note ? <span className="muted"> {note}</span> : null}
    </span>
  );
}

// WEB-UI FR-UI-33 (SRV-OBS FR-OBS-23): record a bench result with its pinned set file; it never starts a bench.
function BenchUpload() {
  const t = useObsT();
  const { obs } = useServices();
  const [setFile, setSetFile] = useState<File | null>(null);
  const [resultFile, setResultFile] = useState<File | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const upload = async () => {
    if (!setFile || !resultFile) {
      setNote(t('bench.choose_both'));
      return;
    }
    const [setText, resultText] = await Promise.all([setFile.text(), resultFile.text()]);
    let result: unknown;
    try {
      result = JSON.parse(resultText);
    } catch {
      setNote('bench_invalid');
      return;
    }
    try {
      const r = await (obs as ObsApi).bench(setText, result);
      setNote(r.status === 202 ? t('bench.recorded') : typeof r.body.error === 'string' ? r.body.error : `http_${r.status}`);
    } catch {
      setNote(t('network'));
    }
  };
  return (
    <>
      <p className="muted">{t('bench.intro')}</p>
      <label>
        {t('bench.set_file')} <input type="file" onChange={(e) => setSetFile(e.target.files?.[0] ?? null)} />
      </label>{' '}
      <label>
        {t('bench.result_file')} <input type="file" onChange={(e) => setResultFile(e.target.files?.[0] ?? null)} />
      </label>{' '}
      <button type="button" onClick={() => void upload()}>
        {t('bench.upload')}
      </button>
      {note ? <p role="status">{note}</p> : null}
    </>
  );
}

// WEB-UI FR-UI-33 (SRV-OBS FR-OBS-25): one banner per alert; every value renders as text (the code and subject as
// received).
function Alerts({ alerts }: { alerts: ObsSummary['alerts'] }) {
  const t = useObsT();
  if (alerts.length === 0) return null;
  return (
    <div className="obs-alerts">
      {alerts.map((a, i) => {
        const values = { code: a.code, subject: a.subject ?? '', value: String(a.value ?? '-'), limit: String(a.limit ?? '-') };
        return (
          <p key={`${a.code} ${a.subject ?? ''} ${i}`} role="alert" className="obs-alert">
            {a.subject ? t('alert.line_subject', values) : t('alert.line', values)}
          </p>
        );
      })}
    </div>
  );
}

// WEB-UI FR-UI-33 (SRV-OBS FR-OBS-32): the stack's own health from its last probes; Jaeger is reader doctor's.
function StackView({ stack }: { stack: ObsSummary['stack'] }) {
  const t = useObsT();
  return (
    <ul className="obs-stack">
      {['mlflow', 'rotel'].map((name) => {
        const m = stack[name];
        const state = m === undefined ? t('stack.no_probe') : m.ok ? t('stack.healthy') : t('stack.unhealthy');
        return <li key={name}>{m?.checked_at ? t('stack.line_checked', { name, state, at: m.checked_at }) : t('stack.line', { name, state })}</li>;
      })}
      <li>{t('stack.jaeger')}</li>
    </ul>
  );
}

function TraceLink({ id }: { id: string | null }) {
  const t = useObsT();
  // FR-UI-18: the server gives no trace id when no trace can exist (before tracing, tracing off, past retention).
  if (!id) return <span className="muted">{t('no_trace')}</span>;
  return (
    <span className="obs-trace">
      <a href={traceUrl(id)} target="_blank" rel="noopener noreferrer">
        {t('trace')}
      </a>{' '}
      {/* SRV-OBS FR-OBS-19: Jaeger keeps only the newest traces in memory, so its link may lead nowhere. */}
      <a href={jaegerUrl(id)} target="_blank" rel="noopener noreferrer" title={t('jaeger.title')}>
        {t('jaeger')}
      </a>
    </span>
  );
}

function Traces({ ids }: { ids: string[] }) {
  const t = useObsT();
  if (ids.length === 0) return <span className="muted">{t('no_trace')}</span>;
  return (
    <span className="obs-traces">
      {ids.map((id) => (
        <TraceLink key={id} id={id} />
      ))}
    </span>
  );
}

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section className="obs-section" aria-labelledby={id}>
      <h2 id={id}>{title}</h2>
      {children}
    </section>
  );
}

function Table({ caption, head, children }: { caption: string; head: string[]; children: ReactNode }) {
  return (
    <div className="obs-scroll">
      <table className="obs-table">
        <caption className="visually-hidden">{caption}</caption>
        <thead>
          <tr>
            {head.map((h) => (
              <th key={h} scope="col">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

type EmptyKey = 'empty.runs' | 'empty.answers' | 'empty.failed_jobs' | 'empty.failed_questions' | 'empty.imports' | 'empty.lessons';
const empty = (t: Tr, key: EmptyKey) => <p className="muted">{t(key, { count: SUMMARY_DAYS })}</p>;
const num = (n: number | null): string => (n === null ? '-' : String(n));

function CompileRuns({ runs }: { runs: ObsSummary['compile_runs'] }) {
  const t = useObsT();
  if (runs.length === 0) return empty(t, 'empty.runs');
  return (
    <>
      {runs.map((run) => (
        <div key={run.run_id} className="obs-run">
          <p>
            {interleave(t(run.finished_at ? 'run.line' : 'run.line_open'), {
              id: <code>{run.run_id}</code>,
              started: <time dateTime={run.started_at ?? undefined}>{run.started_at ?? '-'}</time>,
              spent: <span data-metric="spent">{num(run.spent_usd)}</span>,
            })}
          </p>
          <Table
            caption={t('run.steps', { id: run.run_id })}
            head={[t('head.step'), t('head.stage'), t('head.cost'), t('head.seconds'), t('head.model'), t('head.trace')]}
          >
            {run.steps.map((s) => (
              <tr key={s.step}>
                <td>{s.step}</td>
                <td>{s.stage}</td>
                <td>{num(s.cost_usd)}</td>
                <td>{num(s.duration_s)}</td>
                <td>{s.model ?? '-'}</td>
                <td>
                  <TraceLink id={s.trace_id} />
                </td>
              </tr>
            ))}
          </Table>
        </div>
      ))}
    </>
  );
}

function TutorView({ tutor }: { tutor: ObsSummary['tutor'] }) {
  const t = useObsT();
  return (
    <>
      <dl className="obs-metrics">
        <dt>{t('tutor.answers')}</dt>
        <dd data-metric="count">{tutor.count}</dd>
        <dt>{t('tutor.p50')}</dt>
        <dd data-metric="p50">{num(tutor.p50_ms)}</dd>
        <dt>{t('tutor.p95')}</dt>
        <dd data-metric="p95">{num(tutor.p95_ms)}</dd>
        <dt>{t('tutor.cost')}</dt>
        <dd data-metric="cost">{num(tutor.cost_usd_sum)}</dd>
        <dt>{t('tutor.mean')}</dt>
        <dd data-metric="mean">{num(tutor.cost_usd_mean_per_settled_call)}</dd>
      </dl>
      {tutor.slowest.length === 0 ? (
        empty(t, 'empty.answers')
      ) : (
        <Table caption={t('tutor.slowest')} head={[t('head.question'), t('head.ms'), t('head.trace'), t('head.label')]}>
          {tutor.slowest.map((q) => (
            <tr key={q.question_id}>
              <td>
                <code>{q.question_id}</code>
              </td>
              <td>{q.ms}</td>
              <td>
                <TraceLink id={q.trace_id} />
              </td>
              <td>
                <LabelControl target="answer" id={q.question_id} />
              </td>
            </tr>
          ))}
        </Table>
      )}
    </>
  );
}

function FailuresView({ failures }: { failures: ObsSummary['failures'] }) {
  const t = useObsT();
  return (
    <>
      {failures.jobs.length === 0 ? (
        empty(t, 'empty.failed_jobs')
      ) : (
        <Table caption={t('failures.jobs')} head={[t('head.kind'), t('head.code'), t('head.count'), t('head.scope'), t('head.traces')]}>
          {failures.jobs.map((g) => (
            <tr key={`${g.kind} ${g.error_code} ${g.system}`}>
              <td>{g.kind}</td>
              <td>{g.error_code}</td>
              <td>{g.count}</td>
              <td>{g.system ? t('scope.system') : t('scope.yours')}</td>
              <td>
                <Traces ids={g.trace_ids} />
              </td>
            </tr>
          ))}
        </Table>
      )}
      <h3>{t('failures.tutor_heading')}</h3>
      {failures.tutor.length === 0 ? (
        empty(t, 'empty.failed_questions')
      ) : (
        <Table caption={t('failures.tutor')} head={[t('head.code'), t('head.count'), t('head.traces')]}>
          {failures.tutor.map((g) => (
            <tr key={g.error_code}>
              <td>{g.error_code}</td>
              <td>{g.count}</td>
              <td>
                <Traces ids={g.trace_ids} />
              </td>
            </tr>
          ))}
        </Table>
      )}
      {failures.truncated ? <p className="muted">{t('more')}</p> : null}
    </>
  );
}

function IngestView({ ingest }: { ingest: ObsSummary['ingest'] }) {
  const t = useObsT();
  return (
    <>
      {ingest.rows.length === 0 ? (
        empty(t, 'empty.imports')
      ) : (
        <Table
          caption={t('ingest.caption')}
          head={[t('head.material'), t('head.pages'), t('head.import_seconds'), t('head.ppm'), t('head.trace')]}
        >
          {ingest.rows.map((r) => (
            <tr key={`${r.material_sha256} ${r.trace_id ?? ''}`}>
              <td>
                <code>{r.material_sha256.slice(0, 12)}</code>
              </td>
              <td>{r.page_count}</td>
              <td>{num(r.import_s)}</td>
              <td>{r.pages_per_min_import === null ? '-' : r.pages_per_min_import.toFixed(1)}</td>
              <td>
                <TraceLink id={r.trace_id} />
              </td>
            </tr>
          ))}
        </Table>
      )}
      {ingest.truncated ? <p className="muted">{t('more')}</p> : null}
    </>
  );
}

type LessonRow = ObsSummary['lessons']['rows'][number];
type SortKey = 'title' | 'course_title' | 'compile_usd' | 'runs' | 'last_compile_usd' | 'tutor_usd' | 'total_usd' | 'usd_per_1k_chars';
const LESSON_COLUMNS: ReadonlyArray<{ key: SortKey; label: keyof typeof en }> = [
  { key: 'title', label: 'head.lesson' },
  { key: 'course_title', label: 'head.course' },
  { key: 'compile_usd', label: 'head.compile_usd' },
  { key: 'runs', label: 'head.runs' },
  { key: 'last_compile_usd', label: 'head.last_compile_usd' },
  { key: 'tutor_usd', label: 'head.tutor_usd' },
  { key: 'total_usd', label: 'head.total_usd' },
  { key: 'usd_per_1k_chars', label: 'head.usd_per_1k' },
];

/** Rows by `key` in `sign` order (1 up, -1 down); a missing value sorts last either way; ties by lesson id. */
const compare = (a: LessonRow, b: LessonRow, key: SortKey, sign: number): number => {
  const x = a[key];
  const y = b[key];
  if (x === y) return a.lesson_id.localeCompare(b.lesson_id);
  if (x === null) return 1;
  if (y === null) return -1;
  return sign * (typeof x === 'string' ? x.localeCompare(String(y)) : x - Number(y));
};

// SRV-OBS FR-OBS-17: one row per lesson, a course total per course, sortable by every column (client-side).
function LessonsView({ lessons }: { lessons: ObsSummary['lessons'] }) {
  const t = useObsT();
  const [sort, setSort] = useState<{ key: SortKey; dir: 'ascending' | 'descending' }>({ key: 'total_usd', dir: 'descending' });
  if (lessons.rows.length === 0) return empty(t, 'empty.lessons');
  const sign = sort.dir === 'ascending' ? 1 : -1;
  const rows = [...lessons.rows].sort((a, b) => compare(a, b, sort.key, sign));
  const toggle = (key: SortKey) =>
    setSort((s) => ({ key, dir: s.key === key && s.dir === 'descending' ? 'ascending' : 'descending' }));
  return (
    <>
      <p className="muted">
        {lessons.open_reserves > 0 ? t('lessons.note_reserves', { count: lessons.open_reserves }) : t('lessons.note')}
      </p>
      <div className="obs-scroll">
        <table className="obs-table">
          <caption className="visually-hidden">{t('section.lessons')}</caption>
          <thead>
            <tr>
              {LESSON_COLUMNS.map((c) => (
                <th key={c.key} scope="col" aria-sort={sort.key === c.key ? sort.dir : undefined}>
                  <button type="button" className="obs-sort" onClick={() => toggle(c.key)}>
                    {t(c.label)}
                  </button>
                </th>
              ))}
              <th scope="col">{t('head.traces')}</th>
              <th scope="col">{t('head.label')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.lesson_id}>
                <td>{r.deleted ? <span className="muted">{t('lessons.deleted')}</span> : r.title}</td>
                <td>{r.course_title}</td>
                <td>{num(r.compile_usd)}</td>
                <td>{r.runs}</td>
                <td>{num(r.last_compile_usd)}</td>
                <td>{num(r.tutor_usd)}</td>
                <td>{num(r.total_usd)}</td>
                <td>{num(r.usd_per_1k_chars)}</td>
                <td>
                  <a href={r.mlflow_url} target="_blank" rel="noopener noreferrer">
                    {t('head.traces')}
                  </a>
                </td>
                <td>{r.deleted ? null : <LabelControl target="lesson" id={r.lesson_id} />}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h3>{t('lessons.totals')}</h3>
      <Table
        caption={t('lessons.courses_caption')}
        head={[t('head.course'), t('head.compile_usd'), t('head.tutor_usd'), t('head.total_usd'), t('head.usd_per_1k')]}
      >
        {lessons.courses.map((c) => (
          <tr key={c.course_id}>
            <td>{c.title}</td>
            <td>{num(c.compile_usd)}</td>
            <td>{num(c.tutor_usd)}</td>
            <td>{num(c.total_usd)}</td>
            <td>{num(c.usd_per_1k_chars)}</td>
          </tr>
        ))}
      </Table>
      {lessons.truncated ? <p className="muted">{t('more')}</p> : null}
    </>
  );
}

function Dashboards() {
  const t = useObsT();
  return (
    <ul className="obs-dashboards">
      {DASHBOARDS.map((d) => (
        <li key={d.href}>
          <a href={d.href} target="_blank" rel="noopener noreferrer">
            {t.maybe(`dashboard.${d.label}`, undefined, d.label)}
          </a>
        </li>
      ))}
    </ul>
  );
}

export function ObservabilityPage() {
  const t = useObsT();
  // WEB-UI FR-UI-30 merge: AppRoot no longer builds this client; the lazy chunk builds it from `Services.http`.
  const { obs: given, http } = useServices();
  const obs = useMemo(() => given ?? (http ? createObsApi(http) : undefined), [given, http]);
  const q = useQuery({
    queryKey: OBS_KEY,
    queryFn: () => (obs as ObsApi).summary(),
    enabled: obs !== undefined,
  });
  return (
    <div className="page">
      <h1>{t('title')}</h1>
      <p className="muted">
        {interleave(t('intro', { count: SUMMARY_DAYS }), {
          dashboard: (
            <a href="https://localhost:8443/" target="_blank" rel="noopener noreferrer">
              {t('dashboard')}
            </a>
          ),
        })}
      </p>
      {q.isPending ? (
        <p role="status">{t('loading')}</p>
      ) : q.isError ? (
        <StateBlock
          tone="danger"
          icon={<WarningIcon size={20} />}
          title={t('error.title')}
          actions={
            <Button variant="outline" color="neutral" onClick={() => void q.refetch()}>
              {t('retry')}
            </Button>
          }
        >
          <p className="muted">{t('error.body')}</p>
        </StateBlock>
      ) : (
        <>
          <Alerts alerts={q.data.alerts} />
          <Section id="obs-runs" title={t('section.runs')}>
            <CompileRuns runs={q.data.compile_runs} />
          </Section>
          <Section id="obs-tutor" title={t('section.tutor')}>
            <TutorView tutor={q.data.tutor} />
          </Section>
          <Section id="obs-failures" title={t('section.failures')}>
            <FailuresView failures={q.data.failures} />
          </Section>
          <Section id="obs-ingest" title={t('section.ingest')}>
            <IngestView ingest={q.data.ingest} />
          </Section>
          <Section id="obs-lessons" title={t('section.lessons')}>
            <LessonsView lessons={q.data.lessons} />
          </Section>
          <Section id="obs-dashboards" title={t('section.dashboards')}>
            <Dashboards />
          </Section>
          <Section id="obs-bench" title={t('section.bench')}>
            <BenchUpload />
          </Section>
          <Section id="obs-stack" title={t('section.stack')}>
            <StackView stack={q.data.stack} />
          </Section>
        </>
      )}
    </div>
  );
}
