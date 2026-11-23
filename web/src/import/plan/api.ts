// Plan routes client (WEB-PLAN FR-IMPORT-4..6, -8; SRV-PLAN FR-PLAN-9, SRV-INGEST FR-INGEST-15): same-origin
// requests, every response validated at run time, errors as ApiError codes.
import * as v from 'valibot';
import { ApiError } from '../../app/api';

const Material = v.object({
  id: v.string(),
  status: v.string(),
  page_count: v.nullable(v.number()),
});
const Materials = v.object({ materials: v.array(Material) });

const Inputs = v.object({
  material_id: v.string(),
  total_weeks: v.number(),
  minutes_per_day: v.number(),
  rate: v.string(),
  start_date: v.string(),
});
const StoredPlan = v.object({
  course_id: v.string(),
  plan_course_id: v.string(),
  inputs: Inputs,
  plan_sha256: v.string(),
  started: v.number(),
});

const Lesson = v.object({
  id: v.string(),
  pages: v.tuple([v.number(), v.number()]),
  minutes: v.number(),
  fixed: v.boolean(),
  title: v.string(),
});
const Summary = v.object({
  lesson_count: v.number(),
  fixed_count: v.number(),
  new_count: v.number(),
  study_days: v.number(),
  start_date: v.string(),
  end: v.string(),
  days_left: v.number(),
  spare_days: v.number(),
  minutes_mean: v.nullable(v.number()),
  minutes_max: v.nullable(v.number()),
  required_minutes: v.number(),
  finish_by: v.nullable(v.string()),
  fits: v.boolean(),
  reasons: v.array(v.picklist(['days', 'minutes', 'source_size'])),
  suggest: v.nullable(v.object({ minutes_per_day: v.nullable(v.number()), total_weeks: v.nullable(v.number()) })),
});
const Usd = v.pipe(v.string(), v.regex(/^\d+\.\d{2}$/));
const Estimate = v.object({
  course_low_usd: Usd,
  course_high_usd: Usd,
  first_lesson: v.nullable(v.object({ low_usd: Usd, high_usd: Usd })),
  run_cap_usd: Usd,
  basis: v.string(),
  basis_kchars: v.string(),
  mean_lesson_kchars: v.nullable(v.string()),
  extrapolated: v.boolean(),
});
const Preview = v.object({
  plan_sha256: v.string(),
  inputs: v.object({ ...Inputs.entries, today: v.string() }),
  lessons: v.array(Lesson),
  summary: Summary,
  estimate: Estimate,
});
const Confirmed = v.object({
  course_id: v.string(),
  plan_course_id: v.string(),
  lessons: v.array(v.object({ plan_lesson_id: v.string(), lesson_id: v.string(), ordinal: v.number() })),
  created: v.number(),
  removed: v.number(),
  replayed: v.boolean(),
  compile: v.nullable(v.union([v.object({ run_id: v.string() }), v.object({ error: v.string() })])),
});

export type MaterialRow = v.InferOutput<typeof Material>;
export type StoredPlan = v.InferOutput<typeof StoredPlan>;
export type Preview = v.InferOutput<typeof Preview>;
export type Confirmed = v.InferOutput<typeof Confirmed>;

export interface PlanBody {
  material_id: string;
  total_weeks: number;
  minutes_per_day: number;
  rate: string;
  today: string;
}

export interface PlanApi {
  materials(): Promise<{ materials: MaterialRow[] }>;
  /** The stored plan, or null when the material has none yet (404). */
  stored(materialId: string): Promise<StoredPlan | null>;
  preview(body: PlanBody): Promise<Preview>;
  confirm(body: PlanBody & { plan_sha256: string; compile_first: boolean }): Promise<Confirmed>;
}

export interface PlanApiDeps {
  fetch: typeof fetch;
  onUnauthorized: () => void;
}

export const createPlanApi = (deps: PlanApiDeps): PlanApi => {
  const call = async <T>(method: 'GET' | 'POST', path: string, schema: v.GenericSchema<unknown, T>, body?: unknown): Promise<T> => {
    const init: RequestInit = { method, credentials: 'same-origin' };
    if (body !== undefined) {
      init.body = JSON.stringify(body);
      init.headers = { 'Content-Type': 'application/json' };
    }
    let res: Response;
    try {
      res = await deps.fetch(path, init);
    } catch {
      throw new ApiError(0, 'network');
    }
    if (res.status === 401) {
      deps.onUnauthorized();
      throw new ApiError(401, 'unauthenticated');
    }
    if (!res.ok) {
      const err = (await res.json().catch(() => null)) as { error?: unknown } | null;
      throw new ApiError(res.status, typeof err?.error === 'string' ? err.error : `http_${res.status}`);
    }
    const parsed = v.safeParse(schema, await res.json().catch(() => undefined));
    if (!parsed.success) throw new ApiError(res.status, 'bad_response');
    return parsed.output;
  };
  return {
    materials: () => call('GET', '/api/materials', Materials),
    stored: async (materialId) => {
      try {
        return await call('GET', `/api/plans?${new URLSearchParams({ material_id: materialId })}`, StoredPlan);
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null;
        throw e;
      }
    },
    preview: (body) => call('POST', '/api/plans/preview', Preview, body),
    confirm: (body) => call('POST', '/api/plans/confirm', Confirmed, body),
  };
};
