// @trace FR-UI-4 FR-UI-5 VEC-UI-2
import { describe, expect, it, vi } from 'vitest';
import { readJson } from './testing/fixtures';
import { load } from './testing/load';

type Fixture = { list: unknown; lesson: unknown };
const fx = () => readJson<Fixture>('spec/fixtures/WEB-UI/lessons.json');

const respond = (status: number, body?: unknown) =>
  new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
  });

const setup = async (handler: (url: string, init: RequestInit) => Response) => {
  const { createApi } = await load(() => import('./api'));
  expect(createApi).toBeTypeOf('function');
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const onUnauthorized = vi.fn();
  const fetchFn = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input);
    calls.push({ url, init });
    return handler(url, init);
  });
  const api = createApi({ fetch: fetchFn as unknown as typeof fetch, onUnauthorized });
  return { api, calls, onUnauthorized };
};

describe('API client (FR-UI-4)', () => {
  it('parses the lesson list and lesson shapes of VEC-UI-2', async () => {
    const { api } = await setup((url) => (url === '/api/lessons' ? respond(200, fx().list) : respond(200, fx().lesson)));
    const list = await api.listLessons();
    expect(list.lessons).toHaveLength(3);
    expect(list.lessons[1]?.latest_sha256).toBeNull();
    const lesson = await api.getLesson('w01');
    expect(lesson.latest_sha256).toMatch(/^[0-9a-f]{64}$/);
  });

  it('rejects a response that does not match its shape with bad_response', async () => {
    const { api } = await setup(() => respond(200, { lessons: [{ lesson_id: 1 }] }));
    await expect(api.listLessons()).rejects.toMatchObject({ code: 'bad_response' });
  });

  it('sends same-origin requests; JSON bodies with the JSON type; bodiless POSTs without a body', async () => {
    const { api, calls } = await setup((url) =>
      url === '/api/kernel/sessions' ? respond(201, { session_id: 'a'.repeat(32), lesson_id: 'w01' }) : respond(204),
    );
    await api.createSession('w01');
    await api.interrupt('a'.repeat(32));
    await api.resetKernel();
    const [create, interrupt, reset] = calls;
    expect(create?.init.credentials).toBe('same-origin');
    expect(create?.init.method).toBe('POST');
    expect(new Headers(create?.init.headers).get('Content-Type')).toBe('application/json');
    expect(JSON.parse(String(create?.init.body))).toEqual({ lesson_id: 'w01' });
    expect(interrupt?.url).toBe(`/api/kernel/sessions/${'a'.repeat(32)}/interrupt`);
    expect(interrupt?.init.body ?? null).toBeNull();
    expect(new Headers(interrupt?.init.headers).has('Content-Type')).toBe(false);
    expect(reset?.url).toBe('/api/kernel/reset');
    expect(reset?.init.body ?? null).toBeNull();
  });

  it('encodes path segments', async () => {
    const { api, calls } = await setup(() => respond(200, fx().lesson));
    await api.getLesson('a b').catch(() => undefined);
    expect(calls[0]?.url).toBe('/api/lessons/a%20b');
  });

  it('maps HTTP errors to ApiError with status and code', async () => {
    const { api } = await setup(() => respond(409, { error: 'too_many_kernels' }));
    await expect(api.createSession('w01')).rejects.toMatchObject({ status: 409, code: 'too_many_kernels' });
  });

  it('caches lists for 30 s and revisions forever, keyed by sha256', async () => {
    const { queries } = await load(() => import('./queries'));
    expect(queries).toBeTypeOf('function');
    const { api } = await setup(() => respond(200, {}));
    const q = queries(api);
    expect(q.lessons().staleTime).toBe(30_000);
    expect(q.lesson('w01').staleTime).toBe(30_000);
    const rev = q.revision('w01', 'f'.repeat(64));
    expect(rev.staleTime).toBe(Infinity);
    expect(rev.queryKey).toContain('f'.repeat(64));
  });
});

describe('authentication (FR-UI-5)', () => {
  it('calls onUnauthorized on any 401 and never touches web storage', async () => {
    const { api, onUnauthorized } = await setup(() => respond(401, { error: 'unauthenticated' }));
    const before = { local: localStorage.length, session: sessionStorage.length };
    await expect(api.me()).rejects.toMatchObject({ status: 401 });
    await expect(api.listLessons()).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledTimes(2);
    expect({ local: localStorage.length, session: sessionStorage.length }).toEqual(before);
  });

  it('signs out with a bodiless POST', async () => {
    const { api, calls } = await setup(() => respond(204));
    await api.signOut();
    expect(calls[0]?.url).toBe('/api/auth/signout');
    expect(calls[0]?.init.method).toBe('POST');
    expect(calls[0]?.init.body ?? null).toBeNull();
  });

  it('the sign-in URL is the SRV-AUTH page', async () => {
    const { SIGN_IN_URL } = await load(() => import('./api'));
    expect(SIGN_IN_URL).toBe('/api/auth/signin');
  });
});
