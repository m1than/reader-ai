// WEB-UI FR-UI-18 (SRV-OBS FR-OBS-10): the owner's observability page. Four views of the last 7 days from
// GET /api/obs/summary: compile runs, tutor latency and cost, failures, ingest throughput. Every trace id links to
// the traces dashboard on https://localhost:8443 (owner only, behind the gate). Values render as text only.
import { Button } from '@acme/ui/button';
import { useQuery } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { WarningIcon } from '../../ui/icons';
import { StateBlock } from '../../ui/StateBlock';
import { useServices } from '../context';
import { OBS_KEY, SUMMARY_DAYS, traceUrl, type ObsApi, type ObsSummary } from './api';

const MORE = 'More rows may exist.';

function TraceLink({ id }: { id: string | null }) {
  if (!id) return <span className="muted">-</span>;
  return (
    <a href={traceUrl(id)} target="_blank" rel="noopener noreferrer">
      Trace
    </a>
  );
}

function Traces({ ids }: { ids: string[] }) {
  if (ids.length === 0) return <span className="muted">-</span>;
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

export function ObservabilityPage() {
  const { obs } = useServices();
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
        </>
      )}
    </div>
  );
}
