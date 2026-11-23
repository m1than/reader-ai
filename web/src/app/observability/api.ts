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
const Summary = v.object({ days: v.number(), compile_runs: v.array(Run), tutor: Tutor, failures: Failures, ingest: Ingest });

export type ObsSummary = v.InferOutput<typeof Summary>;

export const SUMMARY_DAYS = 7;
export const SUMMARY_PATH = `/api/obs/summary?days=${SUMMARY_DAYS}`;
export const OBS_KEY = ['obs', 'summary', SUMMARY_DAYS] as const;
/** The dashboard's deep link for one trace (measured in spikes/obs-stack, ADR-0032). */
export const traceUrl = (traceId: string): string =>
  `https://localhost:8443/#/experiments/0/traces?selectedEvaluationId=tr-${traceId}`;

export const createObsApi = (deps: ApiDeps) => ({
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
});

export type ObsApi = ReturnType<typeof createObsApi>;
