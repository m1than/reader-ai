// @trace FR-UI-13
// First run sends an owner with no lessons and no materials to /import; the lesson list shows each unpublished
// lesson's compile status. Synthetic bodies only.
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { load } from './testing/load';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const user = (role: string) => ({ id: '0b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b10', handle: 'ivan', role, display_name: 'Ivan', consent: { evaluation: true, tuning: false } });
const lesson = (id: string, published: boolean) => ({
  lesson_id: id,
  course_id: 'c1',
  course_title: 'Synthetic course',
  ordinal: 1,
  title: `Synthetic chapter ${id}`,
  latest_sha256: published ? 'a'.repeat(64) : null,
  updated_at: '2026-11-23T10:00:00Z',
});

type Routes = Record<string, () => Response>;

const mount = async (role: string, routes: Routes) => {
  const { AppRoot } = await load(() => import('./AppRoot'));
  expect(AppRoot).toBeTypeOf('function');
  const fetchFn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === '/api/me') return json(200, user(role));
    const route = routes[url];
    return route ? route() : json(404, { error: 'not_found' });
  });
  await act(async () => {
    render(<AppRoot initialPath="/" fetch={fetchFn as unknown as typeof fetch} navigateAway={vi.fn()} />);
  });
  return fetchFn;
};

const asked = (fetchFn: ReturnType<typeof vi.fn>, url: string) => fetchFn.mock.calls.some(([u]) => String(u) === url);

describe('first run (FR-UI-13)', () => {
  it('replaces / with /import for an owner with no lessons and no materials', async () => {
    await mount('owner', {
      '/api/lessons': () => json(200, { lessons: [] }),
      '/api/materials': () => json(200, { materials: [], inbox_retained_bytes: 0 }),
    });
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Import a source', level: 1 })).toBeTruthy());
    expect(window.history.length).toBeLessThanOrEqual(2);
  });

  it('keeps the empty list for an owner who has a material', async () => {
    const fetchFn = await mount('owner', {
      '/api/lessons': () => json(200, { lessons: [] }),
      '/api/materials': () => json(200, { materials: [{ id: 'synthetic', status: 'imported', page_count: 3 }] }),
    });
    await waitFor(() => expect(asked(fetchFn, '/api/materials')).toBe(true));
    expect(screen.queryByRole('heading', { name: 'Import a source' })).toBeNull();
  });

  it('never asks for materials as a learner', async () => {
    const fetchFn = await mount('learner', { '/api/lessons': () => json(200, { lessons: [] }) });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    expect(asked(fetchFn, '/api/materials')).toBe(false);
  });

  it('keeps the empty list when the materials call fails', async () => {
    await mount('owner', {
      '/api/lessons': () => json(200, { lessons: [] }),
      '/api/materials': () => json(503, { error: 'config_unavailable' }),
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    expect(screen.queryByRole('heading', { name: 'Import a source' })).toBeNull();
  });
});

describe('compile status in the list (FR-UI-13)', () => {
  it('maps each lesson compile state into the status slot', async () => {
    const fetchFn = await mount('owner', {
      '/api/lessons': () =>
        json(200, { lessons: [lesson('a', false), lesson('b', false), lesson('c', false), lesson('d', true)] }),
      '/api/lessons/a/compile': () => json(404, { error: 'not_found' }),
      '/api/lessons/b/compile': () => json(200, { state: 'running' }),
      '/api/lessons/c/compile': () => json(200, { state: 'budget_exhausted' }),
    });
    await waitFor(() => expect(screen.getByText('Compiling')).toBeTruthy());
    expect(screen.getByText('not compiled yet')).toBeTruthy();
    expect(screen.getByText('Compile stopped')).toBeTruthy();
    expect(asked(fetchFn, '/api/lessons/d/compile')).toBe(false); // published: no query
  });

  it('asks for at most 10 unpublished lessons per render, in list order', async () => {
    const lessons = Array.from({ length: 14 }, (_, i) => lesson(`l${String(i).padStart(2, '0')}`, false));
    const fetchFn = await mount('owner', { '/api/lessons': () => json(200, { lessons }) });
    await waitFor(() => expect(fetchFn.mock.calls.filter(([u]) => /\/compile$/.test(String(u))).length).toBe(10));
    const asks = fetchFn.mock.calls.map(([u]) => String(u)).filter((u) => /\/compile$/.test(u));
    expect(asks).toEqual(lessons.slice(0, 10).map((l) => `/api/lessons/${l.lesson_id}/compile`));
  });

  it('refetches every 10 s while a shown lesson compiles', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchFn = await mount('owner', {
      '/api/lessons': () => json(200, { lessons: [lesson('b', false)] }),
      '/api/lessons/b/compile': () => json(200, { state: 'stalled' }),
    });
    await waitFor(() => expect(screen.getByText('Compiling')).toBeTruthy());
    const before = fetchFn.mock.calls.filter(([u]) => String(u) === '/api/lessons/b/compile').length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(fetchFn.mock.calls.filter(([u]) => String(u) === '/api/lessons/b/compile').length).toBeGreaterThan(before);
  });
});
