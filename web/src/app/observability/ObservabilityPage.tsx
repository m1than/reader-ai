// WEB-UI FR-UI-18 (SRV-OBS FR-OBS-10): the owner's observability page. Four views of the last 7 days from
// GET /api/obs/summary: compile runs, tutor latency and cost, failures, ingest throughput. Every trace id links to
// the traces dashboard on https://localhost:8443 (owner only, behind the gate). Values render as text only; span,
// kind, stage and code values stay untranslated (FR-UI-30's declared exceptions).
import { Button } from '@acme/ui/button';
import { useQuery } from '@tanstack/react-query';
import { useMemo, type ReactNode } from 'react';
import { interleave, useT, type T } from '../../ui/i18n';
import { WarningIcon } from '../../ui/icons';
import { StateBlock } from '../../ui/StateBlock';
import { useServices } from '../context';
import { createObsApi, OBS_KEY, SUMMARY_DAYS, traceUrl, type ObsApi, type ObsSummary } from './api';
import en from './messages.en';

type Tr = T<typeof en>;

function TraceLink({ id, t }: { id: string | null; t: Tr }) {
  // FR-UI-18: the server gives no trace id when no trace can exist (before tracing, tracing off, past retention).
  if (!id) return <span className="muted">{t('no_trace')}</span>;
  return (
    <a href={traceUrl(id)} target="_blank" rel="noopener noreferrer">
      {t('trace')}
    </a>
  );
}

function Traces({ ids, t }: { ids: string[]; t: Tr }) {
  if (ids.length === 0) return <span className="muted">{t('no_trace')}</span>;
  return (
    <span className="obs-traces">
      {ids.map((id) => (
        <TraceLink key={id} id={id} t={t} />
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

type EmptyKey = 'empty.runs' | 'empty.answers' | 'empty.failed_jobs' | 'empty.failed_questions' | 'empty.imports';
const empty = (t: Tr, key: EmptyKey) => <p className="muted">{t(key, { days: SUMMARY_DAYS })}</p>;
const num = (n: number | null): string => (n === null ? '-' : String(n));

function CompileRuns({ runs, t }: { runs: ObsSummary['compile_runs']; t: Tr }) {
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
                  <TraceLink id={s.trace_id} t={t} />
                </td>
              </tr>
            ))}
          </Table>
        </div>
      ))}
    </>
  );
}

function TutorView({ tutor, t }: { tutor: ObsSummary['tutor']; t: Tr }) {
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
        <Table caption={t('tutor.slowest')} head={[t('head.question'), t('head.ms'), t('head.trace')]}>
          {tutor.slowest.map((q) => (
            <tr key={q.question_id}>
              <td>
                <code>{q.question_id}</code>
              </td>
              <td>{q.ms}</td>
              <td>
                <TraceLink id={q.trace_id} t={t} />
              </td>
            </tr>
          ))}
        </Table>
      )}
    </>
  );
}

function FailuresView({ failures, t }: { failures: ObsSummary['failures']; t: Tr }) {
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
                <Traces ids={g.trace_ids} t={t} />
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
                <Traces ids={g.trace_ids} t={t} />
              </td>
            </tr>
          ))}
        </Table>
      )}
      {failures.truncated ? <p className="muted">{t('more')}</p> : null}
    </>
  );
}

function IngestView({ ingest, t }: { ingest: ObsSummary['ingest']; t: Tr }) {
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
                <TraceLink id={r.trace_id} t={t} />
              </td>
            </tr>
          ))}
        </Table>
      )}
      {ingest.truncated ? <p className="muted">{t('more')}</p> : null}
    </>
  );
}

export function ObservabilityPage() {
  // NFR-UI-1: the summary client is built in this lazy chunk from the shell's fetch and 401 hook.
  const { obs: given, http } = useServices();
  const obs = useMemo(() => given ?? (http ? createObsApi(http) : undefined), [given, http]);
  const t = useT('app/observability', en);
  const q = useQuery({
    queryKey: OBS_KEY,
    queryFn: () => (obs as ObsApi).summary(),
    enabled: obs !== undefined,
  });
  return (
    <div className="page">
      <h1>{t('title')}</h1>
      <p className="muted">
        {interleave(t('intro', { days: SUMMARY_DAYS }), {
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
          <Section id="obs-runs" title={t('section.runs')}>
            <CompileRuns runs={q.data.compile_runs} t={t} />
          </Section>
          <Section id="obs-tutor" title={t('section.tutor')}>
            <TutorView tutor={q.data.tutor} t={t} />
          </Section>
          <Section id="obs-failures" title={t('section.failures')}>
            <FailuresView failures={q.data.failures} t={t} />
          </Section>
          <Section id="obs-ingest" title={t('section.ingest')}>
            <IngestView ingest={q.data.ingest} t={t} />
          </Section>
        </>
      )}
    </div>
  );
}
