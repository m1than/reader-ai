// WEB-UI FR-UI-18 (SRV-OBS FR-OBS-10, -16, -17, -19): the owner's observability page. Five views of the last 7
// days from GET /api/obs/summary: compile runs, tutor latency and cost, failures, ingest throughput and cost per
// lesson. Every trace id links to the traces dashboard on https://localhost:8443 and to Jaeger on
// https://localhost:8444 (owner only, behind the gate); the saved dashboard views are listed. Values render as text.
import { Button } from '@acme/ui/button';
import { useQuery } from '@tanstack/react-query';
import { useMemo, useState, type ReactNode } from 'react';
import { WarningIcon } from '../../ui/icons';
import { StateBlock } from '../../ui/StateBlock';
import { useServices } from '../context';
import { createObsApi, DASHBOARDS, jaegerUrl, OBS_KEY, SUMMARY_DAYS, traceUrl, type ObsApi, type ObsSummary } from './api';

const MORE = 'More rows may exist.';
// FR-UI-18: the server gives no trace id when no trace can exist (before tracing, tracing off, past retention).
const NO_TRACE = 'no trace';
// SRV-OBS FR-OBS-19: Jaeger keeps only the newest traces in memory, so its link may lead nowhere.
const JAEGER_TITLE =
  'Jaeger keeps the newest 450 traces and starts empty after a restart; an older trace may have rotated out';

function TraceLink({ id }: { id: string | null }) {
  if (!id) return <span className="muted">{NO_TRACE}</span>;
  return (
    <span className="obs-trace">
      <a href={traceUrl(id)} target="_blank" rel="noopener noreferrer">
        Trace
      </a>{' '}
      <a href={jaegerUrl(id)} target="_blank" rel="noopener noreferrer" title={JAEGER_TITLE}>
        Jaeger (recent traces only)
      </a>
    </span>
  );
}

function Traces({ ids }: { ids: string[] }) {
  if (ids.length === 0) return <span className="muted">{NO_TRACE}</span>;
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

const empty = (what: string) => <p className="muted">No {what} in the last {SUMMARY_DAYS} days.</p>;
const num = (n: number | null): string => (n === null ? '-' : String(n));

function CompileRuns({ runs }: { runs: ObsSummary['compile_runs'] }) {
  if (runs.length === 0) return empty('compile runs');
  return (
    <>
      {runs.map((run) => (
        <div key={run.run_id} className="obs-run">
          <p>
            Run <code>{run.run_id}</code>, started <time dateTime={run.started_at ?? undefined}>{run.started_at ?? '-'}</time>
            {run.finished_at ? '' : ' (not finished)'}, spent USD <span data-metric="spent">{num(run.spent_usd)}</span>
          </p>
          <Table caption={`Steps of run ${run.run_id}`} head={['Step', 'Stage', 'Cost USD', 'Seconds', 'Model', 'Trace']}>
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
  return (
    <>
      <dl className="obs-metrics">
        <dt>Answers</dt>
        <dd data-metric="count">{tutor.count}</dd>
        <dt>p50 ms</dt>
        <dd data-metric="p50">{num(tutor.p50_ms)}</dd>
        <dt>p95 ms</dt>
        <dd data-metric="p95">{num(tutor.p95_ms)}</dd>
        <dt>Cost USD</dt>
        <dd data-metric="cost">{num(tutor.cost_usd_sum)}</dd>
        <dt>Mean USD per settled call</dt>
        <dd data-metric="mean">{num(tutor.cost_usd_mean_per_settled_call)}</dd>
      </dl>
      {tutor.slowest.length === 0 ? (
        empty('answers')
      ) : (
        <Table caption="Slowest answers" head={['Question', 'ms', 'Trace']}>
          {tutor.slowest.map((q) => (
            <tr key={q.question_id}>
              <td>
                <code>{q.question_id}</code>
              </td>
              <td>{q.ms}</td>
              <td>
                <TraceLink id={q.trace_id} />
              </td>
            </tr>
          ))}
        </Table>
      )}
    </>
  );
}

function FailuresView({ failures }: { failures: ObsSummary['failures'] }) {
  return (
    <>
      {failures.jobs.length === 0 ? (
        empty('failed jobs')
      ) : (
        <Table caption="Failed jobs by kind and code" head={['Kind', 'Code', 'Count', 'Scope', 'Traces']}>
          {failures.jobs.map((g) => (
            <tr key={`${g.kind} ${g.error_code} ${g.system}`}>
              <td>{g.kind}</td>
              <td>{g.error_code}</td>
              <td>{g.count}</td>
              <td>{g.system ? 'system' : 'yours'}</td>
              <td>
                <Traces ids={g.trace_ids} />
              </td>
            </tr>
          ))}
        </Table>
      )}
      <h3>Tutor failures</h3>
      {failures.tutor.length === 0 ? (
        empty('failed questions')
      ) : (
        <Table caption="Failed questions by code" head={['Code', 'Count', 'Traces']}>
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
      {failures.truncated ? <p className="muted">{MORE}</p> : null}
    </>
  );
}

function IngestView({ ingest }: { ingest: ObsSummary['ingest'] }) {
  return (
    <>
      {ingest.rows.length === 0 ? (
        empty('imports')
      ) : (
        <Table caption="Imports" head={['Material', 'Pages', 'Import seconds', 'Pages per minute', 'Trace']}>
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
      {ingest.truncated ? <p className="muted">{MORE}</p> : null}
    </>
  );
}

type LessonRow = ObsSummary['lessons']['rows'][number];
type SortKey = 'title' | 'course_title' | 'compile_usd' | 'runs' | 'last_compile_usd' | 'tutor_usd' | 'total_usd' | 'usd_per_1k_chars';
const LESSON_COLUMNS: ReadonlyArray<{ key: SortKey; label: string }> = [
  { key: 'title', label: 'Lesson' },
  { key: 'course_title', label: 'Course' },
  { key: 'compile_usd', label: 'Compile USD' },
  { key: 'runs', label: 'Runs' },
  { key: 'last_compile_usd', label: 'Last compile USD' },
  { key: 'tutor_usd', label: 'Tutor USD' },
  { key: 'total_usd', label: 'Total USD' },
  { key: 'usd_per_1k_chars', label: 'USD per 1k characters' },
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
  const [sort, setSort] = useState<{ key: SortKey; dir: 'ascending' | 'descending' }>({ key: 'total_usd', dir: 'descending' });
  if (lessons.rows.length === 0) return empty('spend on lessons');
  const sign = sort.dir === 'ascending' ? 1 : -1;
  const rows = [...lessons.rows].sort((a, b) => compare(a, b, sort.key, sign));
  const toggle = (key: SortKey) =>
    setSort((s) => ({ key, dir: s.key === key && s.dir === 'descending' ? 'ascending' : 'descending' }));
  return (
    <>
      <p className="muted">
        From the ledger: failed attempts and open reserves count
        {lessons.open_reserves > 0 ? ` (includes ${lessons.open_reserves} open reserves)` : ''}. MLflow&apos;s Cost
        column shows each call&apos;s settled amount.
      </p>
      <div className="obs-scroll">
        <table className="obs-table">
          <caption className="visually-hidden">Cost per lesson</caption>
          <thead>
            <tr>
              {LESSON_COLUMNS.map((c) => (
                <th key={c.key} scope="col" aria-sort={sort.key === c.key ? sort.dir : undefined}>
                  <button type="button" className="obs-sort" onClick={() => toggle(c.key)}>
                    {c.label}
                  </button>
                </th>
              ))}
              <th scope="col">Traces</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.lesson_id}>
                <td>{r.deleted ? <span className="muted">deleted lesson</span> : r.title}</td>
                <td>{r.course_title}</td>
                <td>{num(r.compile_usd)}</td>
                <td>{r.runs}</td>
                <td>{num(r.last_compile_usd)}</td>
                <td>{num(r.tutor_usd)}</td>
                <td>{num(r.total_usd)}</td>
                <td>{num(r.usd_per_1k_chars)}</td>
                <td>
                  <a href={r.mlflow_url} target="_blank" rel="noopener noreferrer">
                    Traces
                  </a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h3>Course totals</h3>
      <Table caption="Cost per course" head={['Course', 'Compile USD', 'Tutor USD', 'Total USD', 'USD per 1k characters']}>
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
      {lessons.truncated ? <p className="muted">{MORE}</p> : null}
    </>
  );
}

function Dashboards() {
  return (
    <ul className="obs-dashboards">
      {DASHBOARDS.map((d) => (
        <li key={d.href}>
          <a href={d.href} target="_blank" rel="noopener noreferrer">
            {d.label}
          </a>
        </li>
      ))}
    </ul>
  );
}

export function ObservabilityPage() {
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
      <h1>Observability</h1>
      <p className="muted">
        The last {SUMMARY_DAYS} days. Open a trace for its timeline in the{' '}
        <a href="https://localhost:8443/" target="_blank" rel="noopener noreferrer">
          traces dashboard
        </a>
        .
      </p>
      {q.isPending ? (
        <p role="status">Loading the summary</p>
      ) : q.isError ? (
        <StateBlock
          tone="danger"
          icon={<WarningIcon size={20} />}
          title="The summary could not be loaded."
          actions={
            <Button variant="outline" color="neutral" onClick={() => void q.refetch()}>
              Retry
            </Button>
          }
        >
          <p className="muted">Check the connection, then retry.</p>
        </StateBlock>
      ) : (
        <>
          <Section id="obs-runs" title="Compile runs">
            <CompileRuns runs={q.data.compile_runs} />
          </Section>
          <Section id="obs-tutor" title="Tutor">
            <TutorView tutor={q.data.tutor} />
          </Section>
          <Section id="obs-failures" title="Failures">
            <FailuresView failures={q.data.failures} />
          </Section>
          <Section id="obs-ingest" title="Ingest">
            <IngestView ingest={q.data.ingest} />
          </Section>
          <Section id="obs-lessons" title="Cost per lesson">
            <LessonsView lessons={q.data.lessons} />
          </Section>
          <Section id="obs-dashboards" title="Dashboards">
            <Dashboards />
          </Section>
        </>
      )}
    </div>
  );
}
