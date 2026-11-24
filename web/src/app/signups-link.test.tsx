// @trace FR-UI-14 FR-ACCOUNTS-4 FR-ACCOUNTS-5
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { load } from './testing/load';

const owner = { id: '0b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b10', handle: 'ivan', role: 'owner', display_name: 'Ivan', consent: { evaluation: true, tuning: false } };
const learner = { ...owner, handle: 'lc-1a2b', role: 'learner', display_name: 'Анна' };
const pending = (n: number) => ({
  pending: Array.from({ length: n }, (_, i) => ({ id: `00000000-0000-4000-8000-00000000000${i}`, handle: `lc-p${i}x`, display_name: `P${i}`, requested_at: '2026-11-23T09:00:00+00:00' })),
  accounts: [],
});

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
  await act(async () => {
    render(<AppRoot initialPath={path} fetch={fetchFn as unknown as typeof fetch} navigateAway={vi.fn()} />);
  });
  return { urls };
};

afterEach(cleanup);

describe('Sign-ups entry points (FR-UI-14, FR-ACCOUNTS-4)', () => {
  it('shows the owner a link with the pending count, asked once', async () => {
    const { urls } = await mount('/', {
      '/api/me': () => json(200, owner),
      '/api/lessons': () => json(200, { lessons: [] }),
      '/api/auth/signups': () => json(200, pending(3)),
    });
    // The link is a NAV_ITEMS entry (FR-UI-11): it shows "Sign-ups 3" and reads "Sign-ups 3 waiting", only "waiting"
    // visually hidden (code review 2026-11-24; e2e/web/ui/header.spec.ts checks the name Chromium computes; jsdom
    // drops the spaces between inline parts, so this compares without whitespace).
    const link = await screen.findByRole('link', { name: (n) => n.replace(/\s+/g, '') === 'Sign-ups3waiting' });
    expect(link.getAttribute('href')).toBe('/signups');
    expect(link.textContent).toBe('Sign-ups 3 waiting');
    expect(link.querySelector('.nav-badge')?.textContent).toBe('3');
    expect(link.querySelector('.visually-hidden')?.textContent).toBe(' waiting');
    expect(urls.filter((u) => u === '/api/auth/signups')).toHaveLength(1);
  });

  it('shows no count when nothing waits, and no count when the call fails', async () => {
    await mount('/', { '/api/me': () => json(200, owner), '/api/lessons': () => json(200, { lessons: [] }), '/api/auth/signups': () => json(200, pending(0)) });
    expect((await screen.findByRole('link', { name: 'Sign-ups' })).textContent).toBe('Sign-ups');
    cleanup();
    await mount('/', { '/api/me': () => json(200, owner), '/api/lessons': () => json(200, { lessons: [] }), '/api/auth/signups': () => json(503, { error: 'unavailable' }) });
    await waitFor(() => expect(screen.getByRole('link', { name: 'Sign-ups' }).textContent).toBe('Sign-ups'));
  });

  it('gives a learner no link and no call', async () => {
    const { urls } = await mount('/', { '/api/me': () => json(200, learner), '/api/lessons': () => json(200, { lessons: [] }) });
    await screen.findByRole('heading', { level: 1, name: 'Lessons' });
    expect(screen.queryByRole('link', { name: /Sign-ups/ })).toBeNull();
    expect(urls).not.toContain('/api/auth/signups');
  });

  it('shows a learner not-found on /signups without calling the sign-up routes', async () => {
    const { urls } = await mount('/signups', { '/api/me': () => json(200, learner) });
    await waitFor(() => expect(screen.getByText(/not found/i)).toBeTruthy());
    expect(urls.filter((u) => u.startsWith('/api/auth/signups'))).toEqual([]);
  });

  it('routes the owner to the Sign-ups page inside the one main', async () => {
    await mount('/signups', { '/api/me': () => json(200, owner), '/api/auth/signups': () => json(200, pending(1)) });
    await screen.findByRole('heading', { level: 1, name: 'Sign-ups' });
    expect(document.querySelectorAll('main#content')).toHaveLength(1);
    expect(document.querySelector('a.skip-link')).toBeTruthy();
  });
});
