// @trace FR-UI-3 FR-UI-5 FR-UI-6 VEC-UI-2
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { readJson } from './testing/fixtures';
import { load } from './testing/load';

type Fixture = { list: { lessons: unknown[] }; lesson: unknown; bad_ids: string[] };
const fx = () => readJson<Fixture>('spec/fixtures/WEB-UI/lessons.json');
// SRV-AUTH FR-AUTH-9: `id` is the user's UUID as a string (the live run found the client expecting a number).
const me = { id: '0b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b10', handle: 'ivan', role: 'owner', display_name: 'Ivan', consent: { evaluation: true, tuning: false } };

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

const mount = async (path: string, routes: Record<string, () => Response>) => {
  const { AppRoot } = await load(() => import('./AppRoot'));
  expect(AppRoot).toBeTypeOf('function');
  const urls: string[] = [];
  const fetchFn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    urls.push(url);
    const handler = routes[url];
    return handler ? handler() : json(404, { error: 'not_found' });
  });
  const navigate = vi.fn();
  await act(async () => {
    render(<AppRoot initialPath={path} fetch={fetchFn as unknown as typeof fetch} navigateAway={navigate} />);
  });
  return { urls, navigate };
};

afterEach(cleanup);

describe('routes (FR-UI-3)', () => {
  it('accepts valid lesson ids and refuses every bad id of VEC-UI-2', async () => {
    const { isLessonId } = await load(() => import('./routes'));
    expect(isLessonId).toBeTypeOf('function');
    for (const id of ['w01', 'a', 'L02', 'x_y-z.1', 'a'.repeat(128)]) expect(isLessonId(id)).toBe(true);
    for (const id of fx().bad_ids) expect(isLessonId(id)).toBe(false);
  });

  it('shows not-found for a bad id without requesting the lesson', async () => {
    const { urls } = await mount('/lessons/..', { '/api/me': () => json(200, me) });
    await waitFor(() => expect(screen.getByText(/not found/i)).toBeTruthy());
    expect(urls.filter((u) => u.startsWith('/api/lessons'))).toEqual([]);
  });

  it('shows not-found for an unknown path', async () => {
    await mount('/nowhere', { '/api/me': () => json(200, me) });
    await waitFor(() => expect(screen.getByText(/not found/i)).toBeTruthy());
  });
});

describe('sign-in and header (FR-UI-5)', () => {
  it('calls /api/me first and navigates to the sign-in page on 401', async () => {
    const { urls, navigate } = await mount('/', { '/api/me': () => json(401, { error: 'unauthenticated' }) });
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/api/auth/signin'));
    expect(urls[0]).toBe('/api/me');
  });

  it('shows the display name and signs out, then navigates to sign-in', async () => {
    const { navigate, urls } = await mount('/', {
      '/api/me': () => json(200, me),
      '/api/lessons': () => json(200, fx().list),
      '/api/auth/signout': () => new Response(null, { status: 204 }),
    });
    await waitFor(() => expect(screen.getByText('Ivan')).toBeTruthy());
    await act(async () => {
      screen.getByRole('button', { name: /sign out/i }).click();
    });
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/api/auth/signin'));
    expect(urls).toContain('/api/auth/signout');
  });
});

describe('lesson list (FR-UI-6)', () => {
  it('groups by course, orders by ordinal, links published lessons only', async () => {
    // A learner: the owner's unpublished rows carry FR-UI-15's compile status instead of "not published yet".
    await mount('/', { '/api/me': () => json(200, { ...me, role: 'learner' }), '/api/lessons': () => json(200, fx().list) });
    await waitFor(() => expect(screen.getByText('Fixture course')).toBeTruthy());
    expect(screen.getByText('Another course')).toBeTruthy();
    const w01 = screen.getByText('Fixture lesson W01: one cell of every kind').closest('a');
    expect(w01?.getAttribute('href')).toBe('/lessons/w01');
    const w02 = screen.getByText('Fixture lesson W02: not compiled yet');
    expect(w02.closest('a')).toBeNull();
    expect(screen.getByText(/not published yet/i)).toBeTruthy();
    const headings = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent);
    expect(headings).toEqual(['Another course', 'Fixture course']);
  });

  it('groupLessons sorts courses by title in the locale and lessons by ordinal', async () => {
    const { groupLessons } = await load(() => import('./lessons'));
    expect(groupLessons).toBeTypeOf('function');
    const list = fx().list.lessons as Parameters<typeof groupLessons>[0];
    const groups = groupLessons([...list].reverse(), 'en');
    expect(groups.map((g) => g.course_title)).toEqual(['Another course', 'Fixture course']);
    expect(groups[1]?.lessons.map((l) => l.ordinal)).toEqual([1, 2]);
  });

  it('shows the empty state', async () => {
    await mount('/', { '/api/me': () => json(200, me), '/api/lessons': () => json(200, { lessons: [] }) });
    await waitFor(() => expect(screen.getByText('No lessons yet')).toBeTruthy());
  });

  it('shows the error state with Retry', async () => {
    await mount('/', { '/api/me': () => json(200, me), '/api/lessons': () => json(500, { error: 'x' }) });
    await waitFor(() => expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy());
  });
});
