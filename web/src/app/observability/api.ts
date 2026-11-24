// WEB-UI FR-UI-18: the client of SRV-OBS FR-OBS-10's GET /api/obs/summary (owner only). The body is validated
// before use; a 401 goes to the shell's sign-in redirect (FR-UI-5).
import * as v from 'valibot';
import { ApiError, type ApiDeps } from '../api';

const TraceId = v.pipe(v.string(), v.regex(/^[0-9a-f]{32}$/));
const Step = v.object({
  step: v.number(),
  stage: v.string(),
  cost_usd: v.number(),
  duration_s: v.number(),
  model: v.nullable(v.string()),
  trace_id: v.nullable(TraceId),
});
const Run = v.object({
  run_id: v.string(),
  lesson_id: v.string(),
  started_at: v.nullable(v.string()),
  finished_at: v.nullable(v.string()),
  spent_usd: v.number(),
  steps: v.array(Step),
});
const Tutor = v.object({
  count: v.number(),
  p50_ms: v.nullable(v.number()),
  p95_ms: v.nullable(v.number()),
  cost_usd_sum: v.number(),
  cost_usd_mean_per_settled_call: v.nullable(v.number()),
  slowest: v.array(v.object({ question_id: v.string(), ms: v.number(), trace_id: v.nullable(TraceId) })),
});
const Failures = v.object({
  truncated: v.boolean(),
  jobs: v.array(
    v.object({ kind: v.string(), error_code: v.string(), count: v.number(), system: v.boolean(), trace_ids: v.array(TraceId) }),
  ),
  tutor: v.array(v.object({ error_code: v.string(), count: v.number(), trace_ids: v.array(TraceId) })),
});
const Ingest = v.object({
  truncated: v.boolean(),
  rows: v.array(
    v.object({
      material_sha256: v.string(),
      page_count: v.number(),
      import_s: v.number(),
      pages_per_min_import: v.nullable(v.number()),
      trace_id: v.nullable(TraceId),
    }),
  ),
});
// SRV-OBS FR-OBS-17: cost per lesson from the ledger (titles shown here only; MLflow never gets them).
const LessonRow = v.object({
  lesson_id: v.string(),
  // null with `deleted` true: spend on a lesson that no longer exists (it stays, so totals equal the ledger)
  title: v.nullable(v.string()),
  course_id: v.nullable(v.string()),
  course_title: v.nullable(v.string()),
  deleted: v.boolean(),
  compile_usd: v.number(),
  runs: v.number(),
  last_compile_usd: v.nullable(v.number()),
  tutor_usd: v.number(),
  total_usd: v.number(),
  open_reserves: v.number(),
  chars: v.nullable(v.number()),
  usd_per_1k_chars: v.nullable(v.number()),
  mlflow_url: v.pipe(v.string(), v.startsWith('https://localhost:8443/')),
});
const CourseTotal = v.object({
  course_id: v.string(),
  title: v.string(),
  compile_usd: v.number(),
  tutor_usd: v.number(),
  total_usd: v.number(),
  chars: v.nullable(v.number()),
  usd_per_1k_chars: v.nullable(v.number()),
});
const Lessons = v.object({
  truncated: v.boolean(),
  open_reserves: v.number(),
  rows: v.array(LessonRow),
  courses: v.array(CourseTotal),
});
// SRV-OBS FR-OBS-25 / FR-OBS-32 (WEB-UI FR-UI-33): alerts from Postgres and the stack's last probes.
const Alert = v.object({
  code: v.string(),
  subject: v.nullable(v.string()),
  value: v.nullable(v.union([v.number(), v.string()])),
  limit: v.nullable(v.union([v.number(), v.string()])),
});
const Member = v.object({ ok: v.boolean(), checked_at: v.nullable(v.string()) });
const Summary = v.object({
  days: v.number(),
  compile_runs: v.array(Run),
  tutor: Tutor,
  failures: Failures,
  ingest: Ingest,
  lessons: Lessons,
  alerts: v.optional(v.array(Alert), []),
  stack: v.optional(v.record(v.string(), Member), {}),
});

export type ObsSummary = v.InferOutput<typeof Summary>;

export const SUMMARY_DAYS = 7;
export const SUMMARY_PATH = `/api/obs/summary?days=${SUMMARY_DAYS}`;
export const OBS_KEY = ['obs', 'summary', SUMMARY_DAYS] as const;
/** The dashboard's deep link for one trace (measured in spikes/obs-stack, ADR-0032). */
export const traceUrl = (traceId: string): string =>
  `https://localhost:8443/#/experiments/0/traces?selectedEvaluationId=tr-${traceId}`;
/** The same trace in Jaeger's waterfall (SRV-OBS FR-OBS-19, ADR-0033; owner only, behind the same gate). */
export const jaegerUrl = (traceId: string): string => `https://localhost:8444/trace/${traceId}`;
/** FR-OBS-16's saved views: the traces tab keeps only `q`, `groupBy` and the time range in its URL (measured). */
export const DASHBOARDS: ReadonlyArray<{ label: string; href: string }> = [
  { label: 'Dead jobs', href: 'https://localhost:8443/#/experiments/0/traces?q=dead' },
  { label: 'Tutor answers', href: 'https://localhost:8443/#/experiments/0/traces?q=job%20tutor.answer' },
  { label: 'Sessions', href: 'https://localhost:8443/#/experiments/0/traces?groupBy=session' },
  { label: 'Usage', href: 'https://localhost:8443/#/experiments/0/dashboard/usage' },
  { label: 'Quality', href: 'https://localhost:8443/#/experiments/0/dashboard/quality' },
  { label: 'Tool calls', href: 'https://localhost:8443/#/experiments/0/dashboard/tool-calls' },
  { label: 'Jaeger search', href: 'https://localhost:8444/search' },
];

// FR-OBS-24's reason enum (VEC-OBS-5 `labels.reasons`).
export const LABEL_REASONS = ['wrong_fact', 'unclear', 'off_topic', 'broken_lab', 'bad_citation', 'other'] as const;
export type LabelReason = (typeof LABEL_REASONS)[number];
export interface LabelBody {
  target: 'lesson' | 'answer';
  id: string;
  ok: boolean;
  reason?: LabelReason;
}
/** A write's outcome: the status and the JSON body (or {} when there is none). */
export interface PostResult {
  status: number;
  body: Record<string, unknown>;
}

export const createObsApi = (deps: ApiDeps) => {
  const post = async (path: string, body: unknown): Promise<PostResult> => {
    let res: Response;
    try {
      res = await deps.fetch(path, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    } catch {
      throw new ApiError(0, 'network');
    }
    if (res.status === 401) deps.onUnauthorized();
    const parsed: unknown = await res.json().catch(() => ({}));
    return { status: res.status, body: parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : {} };
  };
  return {
  /** FR-OBS-23: record a bench result with its pinned set file (never starts a bench). */
  bench: (set: string, result: unknown): Promise<PostResult> => post('/api/obs/bench', { set, result }),
  /** FR-OBS-24: the owner's label on a lesson or an answer. */
  label: (body: LabelBody): Promise<PostResult> => post('/api/obs/labels', body),
  summary: async (): Promise<ObsSummary> => {
    let res: Response;
    try {
      res = await deps.fetch(SUMMARY_PATH, { method: 'GET', credentials: 'same-origin' });
    } catch {
      throw new ApiError(0, 'network');
    }
    if (!res.ok) {
      if (res.status === 401) deps.onUnauthorized();
      throw new ApiError(res.status, `http_${res.status}`);
    }
    const parsed = v.safeParse(Summary, await res.json().catch(() => undefined));
    if (!parsed.success) throw new ApiError(res.status, 'bad_response');
    return parsed.output;
  },
  };
};

export type ObsApi = ReturnType<typeof createObsApi>;
