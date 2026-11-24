// API client (WEB-UI FR-UI-4, FR-UI-5): one typed function per route the slice calls, same-origin requests,
// run-time validation of every response (VEC-UI-2 for the lesson routes).
import * as v from 'valibot';

export const SIGN_IN_URL = '/api/auth/signin';

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(`${status} ${code}`);
    this.name = 'ApiError';
  }
}

const Me = v.object({
  id: v.string(), // the user's UUID (SRV-AUTH FR-AUTH-9)
  handle: v.string(),
  role: v.string(),
  display_name: v.string(),
  consent: v.object({ evaluation: v.boolean(), tuning: v.boolean() }),
});

const sha = v.pipe(v.string(), v.regex(/^[0-9a-f]{64}$/));

const LessonSummary = v.object({
  lesson_id: v.string(),
  course_id: v.string(),
  course_title: v.string(),
  ordinal: v.number(),
  title: v.string(),
  updated_at: v.string(),
  latest_sha256: v.nullable(sha),
});

const LessonList = v.object({ lessons: v.array(LessonSummary) });
// SRV-LESSON FR-LESSON-8: the metadata is nested under `lesson`.
const Lesson = v.object({
  lesson: LessonSummary,
  revisions: v.array(
    v.object({ sha256: sha, byte_size: v.number(), cell_count: v.number(), created_at: v.string(), stages: v.array(v.string()) }),
  ),
});

const Notebook = v.looseObject({
  nbformat: v.number(),
  metadata: v.optional(v.record(v.string(), v.unknown())),
  cells: v.array(
    v.looseObject({
      cell_type: v.picklist(['markdown', 'code', 'raw']),
      id: v.optional(v.string()),
      source: v.union([v.string(), v.array(v.string())]),
      metadata: v.optional(v.record(v.string(), v.unknown())),
      outputs: v.optional(v.array(v.record(v.string(), v.unknown()))),
      execution_count: v.optional(v.nullable(v.number())),
    }),
  ),
});

const Session = v.object({ session_id: v.string(), lesson_id: v.optional(v.string()) });

// SRV-PIPELINE FR-PIPELINE-11, -16, -17 (WEB-UI FR-UI-15): a run's status; only the fields the app reads are checked.
const CompileStatus = v.looseObject({
  run_id: v.string(),
  lesson_id: v.string(),
  state: v.string(),
  cap_usd: v.string(),
  steps: v.array(v.looseObject({ step: v.number(), stage: v.string(), status: v.optional(v.string()) })),
  spent_usd: v.string(),
  refusals: v.array(v.string()),
  latest_sha256: v.nullable(sha),
  error: v.optional(v.nullable(v.string())),
});
const OpenRuns = v.object({ runs: v.array(CompileStatus) });
const Started = v.object({ run_id: v.string() });
// SRV-PLAN FR-PLAN-13.
const usd = v.pipe(v.string(), v.regex(/^\d+\.\d{2}$/));
const Estimate = v.object({
  lesson_id: v.string(),
  kchars: v.pipe(v.string(), v.regex(/^\d+\.\d{3}$/)),
  low_usd: usd,
  high_usd: usd,
  run_cap_usd: usd,
  basis: v.string(),
  basis_kchars: v.pipe(v.string(), v.regex(/^\d+(\.\d+)?$/)),
  extrapolated: v.boolean(),
});

export type Me = v.InferOutput<typeof Me>;
export type LessonSummary = v.InferOutput<typeof LessonSummary>;
export type Lesson = v.InferOutput<typeof Lesson>;
export type Notebook = v.InferOutput<typeof Notebook>;
export type CompileStatus = v.InferOutput<typeof CompileStatus>;
export type LessonEstimate = v.InferOutput<typeof Estimate>;

export interface ApiDeps {
  fetch: typeof fetch;
  onUnauthorized: () => void;
}

const seg = encodeURIComponent;

export const createApi = (deps: ApiDeps) => {
  const call = async <T>(method: 'GET' | 'POST', path: string, schema: v.GenericSchema<unknown, T> | null, body?: unknown): Promise<T> => {
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
    if (schema === null) return undefined as T;
    const json: unknown = await res.json().catch(() => undefined);
    const parsed = v.safeParse(schema, json);
    if (!parsed.success) throw new ApiError(res.status, 'bad_response');
    return parsed.output;
  };

  return {
    me: () => call('GET', '/api/me', Me),
    signOut: () => call('POST', '/api/auth/signout', null),
    listLessons: () => call('GET', '/api/lessons', LessonList),
    getLesson: (lessonId: string) => call('GET', `/api/lessons/${seg(lessonId)}`, Lesson),
    getRevision: (lessonId: string, sha256: string) => call('GET', `/api/lessons/${seg(lessonId)}/revisions/${seg(sha256)}`, Notebook),
    createSession: (lessonId: string) => call('POST', '/api/kernel/sessions', Session, { lesson_id: lessonId }),
    interrupt: (sessionId: string) => call('POST', `/api/kernel/sessions/${seg(sessionId)}/interrupt`, null),
    resetKernel: () => call('POST', '/api/kernel/reset', null),
    // WEB-UI FR-UI-15: the owner's compile routes.
    openCompiles: () => call('GET', '/api/compiles', OpenRuns),
    /** The lesson's latest run, or null when it has none (404). */
    latestCompile: async (lessonId: string): Promise<CompileStatus | null> => {
      try {
        return await call('GET', `/api/lessons/${seg(lessonId)}/compile`, CompileStatus);
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null;
        throw e;
      }
    },
    /** Bodiless: the server's default cap (SRV-PIPELINE FR-PIPELINE-11). */
    startCompile: (lessonId: string) => call('POST', `/api/lessons/${seg(lessonId)}/compile`, Started),
    resumeCompile: (runId: string) => call('POST', `/api/compiles/${seg(runId)}/resume`, CompileStatus),
    lessonEstimate: (lessonId: string) => call('GET', `/api/lessons/${seg(lessonId)}/estimate`, Estimate),
  };
};

export type Api = ReturnType<typeof createApi>;
