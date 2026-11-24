// @trace FR-UI-13
// First run sends an owner with no lessons and no materials to /import. Synthetic bodies only. (The compile status in
// the lesson list moved to V/compile-ui, FR-UI-15.)
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { load } from './testing/load';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const user = (role: string) => ({ id: '0b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b10', handle: 'ivan', role, display_name: 'Ivan', consent: { evaluation: true, tuning: false } });
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
