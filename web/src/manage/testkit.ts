// Test helper for WEB-MANAGE's unit tests (not imported by the app): a fake API answering from VEC-MANAGE-1
// (spec/fixtures/WEB-MANAGE/responses.json) and recording every request, so tests read the request oracle.
import { readJson } from '../app/testing/fixtures';

export type Fixture = Record<string, unknown> & {
  me_owner: Record<string, unknown>;
  me_learner: Record<string, unknown>;
  lessons: { lessons: Array<Record<string, unknown>> };
  courses: Record<'active' | 'archived' | 'deleted', { courses: Array<Record<string, unknown>> }>;
  refusals: Record<string, { status: number; body: unknown }>;
};

export const fixture = (): Fixture => readJson<Fixture>('spec/fixtures/WEB-MANAGE/responses.json');
export const notebookW01 = (): Record<string, unknown> => readJson('spec/fixtures/WEB-NOTEBOOK/lesson-w01.ipynb');

export interface Req {
  method: string;
  path: string;
  body: unknown;
}

export type Answer = { status: number; body?: unknown };
export type Override = Answer | ((req: Req) => Answer | undefined);

const json = (status: number, body?: unknown) =>
  body === undefined ? new Response(null, { status }) : new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

export interface FakeServer {
  fetch: typeof fetch;
  log: Req[];
  /** Requests other than GET. */
  writes: () => Req[];
  /** Requests whose path starts with one of these prefixes. */
  to: (...prefixes: string[]) => Req[];
}

/** `overrides` keys are `"<METHOD> <path with query>"` or `"<METHOD> <path>"`. */
export const fakeServer = (opts: { role?: 'owner' | 'learner'; overrides?: Record<string, Override>; fx?: Fixture } = {}): FakeServer => {
  const fx = opts.fx ?? fixture();
  const log: Req[] = [];
  const get = (k: string) => fx[k] as never;
  const route = (req: Req): Answer => {
    const [path, query = ''] = req.path.split('?');
    const q = new URLSearchParams(query);
    for (const key of [`${req.method} ${req.path}`, `${req.method} ${path}`]) {
      const o = opts.overrides?.[key];
      const a = typeof o === 'function' ? o(req) : o;
      if (a) return a;
    }
    const m = (re: RegExp) => re.exec(path!);
    const ok = (body: unknown): Answer => ({ status: 200, body });
    if (req.method === 'GET') {
      if (path === '/api/me') return ok(opts.role === 'learner' ? fx.me_learner : fx.me_owner);
      if (path === '/api/lessons') return ok(fx.lessons);
      if (path === '/api/compiles') return ok({ runs: [] });
      if (path === '/api/courses') return ok(fx.courses[(q.get('state') ?? 'active') as 'active']);
      let r = m(/^\/api\/courses\/([^/]+)\/lessons$/);
      if (r) return ok((get('deleted_lessons') as Record<string, unknown>)[r[1]!] ?? { lessons: [] });
      if (m(/^\/api\/courses\/[^/]+\/impact$/)) return ok(get('course_impact'));
      if (path === '/api/plans') return ok(get('stored_plan'));
      r = m(/^\/api\/lessons\/([^/]+)$/);
      if (r) {
        const row = fx.lessons.lessons.find((l) => l.lesson_id === r![1]);
        const detail = get('lesson_detail') as { lesson: Record<string, unknown>; revisions: unknown[] };
        return row ? ok({ ...detail, lesson: { ...detail.lesson, ...row } }) : { status: 404, body: { error: 'not_found' } };
      }
      if (m(/^\/api\/lessons\/[^/]+\/revisions\/[^/]+$/)) return ok(notebookW01());
      if (m(/^\/api\/lessons\/[^/]+\/compiles$/)) return ok(get('compile_history'));
      if (m(/^\/api\/lessons\/[^/]+\/compile$/)) return { status: 404, body: { error: 'not_found' } };
      if (m(/^\/api\/lessons\/[^/]+\/notes$/)) return ok(get('notes'));
      if (path === '/api/bookmarks') return ok(get('bookmarks'));
      if (m(/^\/api\/lessons\/[^/]+\/questions$/)) return ok({ questions: [] });
      if (path === '/api/materials') return ok((get('materials') as Record<string, unknown>)[q.get('state') === 'deleted' ? 'deleted' : 'active']);
      r = m(/^\/api\/materials\/([^/]+)\/impact$/);
      if (r) return ok((get('material_impact') as Record<string, unknown>)[r[1]!] ?? { courses: [], lessons: 0, uploads_open: 0 });
      if (path === '/api/materials/converter') return { status: 404, body: { error: 'not_found' } };
      if (path === '/api/auth/users') return ok(get('people'));
      if (path === '/api/auth/credentials') return ok(get('credentials_three'));
      if (path === '/api/auth/sessions') return ok(get('sessions'));
      if (path === '/api/auth/signups') return ok({ pending: [], accounts: [] });
      if (path === '/api/settings') return ok(get('settings'));
      if (path === '/api/audit') return ok(get(q.get('before') ? 'audit_page2' : 'audit_page1'));
      return { status: 404, body: { error: 'not_found' } };
    }
    // Writes: the success bodies of the routes.
    const body = (req.body ?? {}) as Record<string, unknown>;
    const course = (id: string) => [...fx.courses.active.courses, ...fx.courses.archived.courses, ...fx.courses.deleted.courses].find((c) => c.course_id === id);
    let r = m(/^\/api\/courses\/([^/]+)$/);
    if (r && req.method === 'POST' && r[1] !== 'order') return { status: 200, body: { ...course(r[1]!), ...body, version: Number(course(r[1]!)?.version ?? 1) + 1 } };
    if (r && req.method === 'DELETE') return { status: 200, body: get('course_deleted') };
    if (path === '/api/courses/order') return { status: 200, body: fx.courses.active };
    r = m(/^\/api\/courses\/([^/]+)\/(archive|unarchive|restore)$/);
    if (r) return { status: 200, body: course(r[1]!) };
    if (m(/^\/api\/courses\/[^/]+\/lessons\/order$/)) return { status: 200, body: {} };
    r = m(/^\/api\/lessons\/([^/]+)$/);
    if (r && req.method === 'POST') return { status: 200, body: { ...fx.lessons.lessons.find((l) => l.lesson_id === r![1]), ...body } };
    if (r && req.method === 'DELETE') return { status: 200, body: {} };
    if (m(/^\/api\/lessons\/[^/]+\/(skip|unskip|restore)$/)) return { status: 200, body: {} };
    if (m(/^\/api\/lessons\/[^/]+\/progress$/)) return { status: 200, body: get(body.done ? 'progress_done' : 'progress_undone') };
    if (m(/^\/api\/lessons\/[^/]+\/revert$/)) return { status: 200, body: get('revert_applied') };
    if (m(/^\/api\/lessons\/[^/]+\/questions\/clear$/)) return { status: 200, body: get('tutor_cleared') };
    if (m(/^\/api\/compiles\/[^/]+\/cancel$/)) return { status: 200, body: {} };
    if (m(/^\/api\/lessons\/[^/]+\/notes$/)) return { status: 201, body: { ...(get('note_created') as object), ...body } };
    if (m(/^\/api\/lessons\/[^/]+\/notes\/[^/]+$/)) return req.method === 'DELETE' ? { status: 204 } : { status: 200, body: { ...(get('note_created') as object), ...body } };
    if (m(/^\/api\/lessons\/[^/]+\/bookmarks\/[^/]+$/)) return { status: req.method === 'DELETE' ? 204 : 201 };
    if (path === '/api/plans/preview') return { status: 200, body: get('plan_preview') };
    if (path === '/api/plans/confirm') return { status: 200, body: get('plan_confirmed') };
    r = m(/^\/api\/materials\/([^/]+)$/);
    if (r && req.method === 'POST') return { status: 200, body: { ...(get('materials') as { active: { materials: Array<Record<string, unknown>> } }).active.materials.find((x) => x.id === r![1]), ...body } };
    if (r && req.method === 'DELETE') return { status: 200, body: { deleted_at: '2026-11-24T12:00:00Z' } };
    if (m(/^\/api\/materials\/[^/]+\/restore$/)) return { status: 200, body: get('material_restored') };
    if (m(/^\/api\/auth\/credentials\/[^/]+$/)) return req.method === 'DELETE' ? { status: 200, body: { revoked_sessions: 2 } } : { status: 200, body: {} };
    if (m(/^\/api\/auth\/sessions\/[^/]+$/)) return { status: 204 };
    if (path === '/api/auth/sessions/revoke-others') return { status: 200, body: { revoked: 2 } };
    if (path === '/api/auth/register/options') return { status: 200, body: get('register_options') };
    if (path === '/api/auth/register/verify') return { status: 200, body: { ok: true } };
    if (m(/^\/api\/auth\/users\/[^/]+\/sessions\/revoke$/)) return { status: 200, body: { revoked: 1 } };
    if (m(/^\/api\/auth\/users\/[^/]+\/credentials\/[^/]+$/)) return { status: 204 };
    if (m(/^\/api\/auth\/users\/[^/]+$/)) return { status: 204 };
    if (m(/^\/api\/auth\/signups\/[^/]+\/(enable|disable)$/)) return { status: 200, body: { status: 'active' } };
    if (m(/^\/api\/settings\/[^/]+$/)) return { status: 200, body: get('settings') };
    return { status: 404, body: { error: 'not_found' } };
  };
  const fetchFn = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const raw = init?.body;
    let body: unknown = undefined;
    if (typeof raw === 'string') {
      try {
        body = JSON.parse(raw);
      } catch {
        body = raw;
      }
    }
    const req: Req = { method: (init?.method ?? 'GET').toUpperCase(), path: String(input), body };
    log.push(req);
    const a = route(req);
    return json(a.status, a.body);
  }) as typeof fetch;
  return {
    fetch: fetchFn,
    log,
    writes: () => log.filter((r) => r.method !== 'GET'),
    to: (...prefixes) => log.filter((r) => prefixes.some((p) => r.path.startsWith(p))),
  };
};

/** A refusal of VEC-MANAGE-1 as an override answer. */
export const refusal = (name: string, fx: Fixture = fixture()): Answer => {
  const r = fx.refusals[name];
  if (!r) throw new Error(`no refusal ${name}`);
  return r;
};
