// @trace FR-ACCOUNTS-1 FR-ACCOUNTS-2 FR-ACCOUNTS-3 INV-ACCOUNTS-1 INV-ACCOUNTS-2 VEC-ACCOUNTS-1
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { findForbidden } from '../app/sourceScan';
import { readJson } from '../app/testing/fixtures';
import { load } from '../app/testing/load';

type Row = { id: string; handle: string; display_name: string; requested_at: string; disabled?: boolean };
type Body = { pending: Row[]; accounts: Row[] };
type Fx = { empty: Body; one: Body; long_name: string; outcomes: Record<string, { status: number; body: unknown }> };
const fx = () => readJson<Fx>('spec/fixtures/WEB-ACCOUNTS/signups.json');

const json = (status: number, body: unknown) =>
  body === null ? new Response(null, { status }) : new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

type Call = { method: string; url: string; body: unknown };

const twenty = (): Body => ({
  pending: Array.from({ length: 20 }, (_, i) => ({
    id: `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`,
    handle: `lc-p${String(i).padStart(2, '0')}`,
    display_name: `Person ${i}`,
    requested_at: `2026-11-2${i % 3}T0${i % 9}:00:00+00:00`,
  })),
  accounts: [],
});

const mount = async (list: () => Response | Promise<Response>, actions: Record<string, () => Response | Promise<Response>> = {}) => {
  const page = await load(() => import('./SignupsPage'));
  const api = await load(() => import('./api'));
  const ctx = await load(() => import('../app/context'));
  const rq = await import('@tanstack/react-query');
  expect(page.SignupsPage).toBeTypeOf('function');
  expect(api.createSignupsApi).toBeTypeOf('function');
  const calls: Call[] = [];
  const fetchFn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? 'GET';
    calls.push({ method, url, body: init?.body === undefined ? undefined : JSON.parse(String(init.body)) });
    if (method === 'GET' && url === '/api/auth/signups') return list();
    const handler = actions[`${method} ${url}`];
    return handler ? handler() : json(404, { error: 'not_found' });
  });
  const onUnauthorized = vi.fn();
  const signups = api.createSignupsApi({ fetch: fetchFn as unknown as typeof fetch, onUnauthorized });
  const client = new rq.QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } });
  const services = { api: {} as never, notify: vi.fn(), navigateAway: vi.fn(), signups };
  await act(async () => {
    render(
      <ctx.ServicesContext.Provider value={services}>
        <rq.QueryClientProvider client={client}>
          <main id="content" tabIndex={-1}>
            <page.SignupsPage />
          </main>
        </rq.QueryClientProvider>
      </ctx.ServicesContext.Provider>,
    );
  });
  return { calls, onUnauthorized, client };
};

afterEach(cleanup);

describe('pending list (FR-ACCOUNTS-1)', () => {
  it('lists VEC-ACCOUNTS-1 oldest first with text, time and accessible names', async () => {
    await mount(() => json(200, fx().one));
    await waitFor(() => expect(screen.getByRole('heading', { level: 1, name: 'Sign-ups' })).toBeTruthy());
    expect(screen.getByText('New accounts wait here until you approve them. Approved accounts can sign in as learners.')).toBeTruthy();
    const waiting = screen.getByRole('heading', { level: 2, name: 'Waiting for approval' });
    expect(waiting).toBeTruthy();
    const list = screen.getByRole('list', { name: 'Waiting for approval' });
    const items = within(list).getAllByRole('listitem');
    expect(items).toHaveLength(1);
    expect(within(items[0]!).getByText('@lc-1a2b')).toBeTruthy();
    expect(within(items[0]!).getByText('Анна Петрова')).toBeTruthy();
    const time = items[0]!.querySelector('time');
    expect(time?.getAttribute('datetime')).toBe('2026-11-23T09:15:00+00:00');
    expect(screen.getByRole('button', { name: 'Approve @lc-1a2b' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Reject @lc-1a2b' })).toBeTruthy();
  });

  it('orders twenty pending oldest first', async () => {
    const body = twenty();
    await mount(() => json(200, body));
    await waitFor(() => expect(screen.getAllByRole('button', { name: /^Approve @/ })).toHaveLength(20));
    const order = [...body.pending].sort((a, b) => a.requested_at.localeCompare(b.requested_at)).map((p) => `Approve @${p.handle}`);
    expect(screen.getAllByRole('button', { name: /^Approve @/ }).map((b) => b.getAttribute('aria-label') ?? b.textContent)).toEqual(order);
  });

  it('shows the empty texts', async () => {
    await mount(() => json(200, fx().empty));
    await waitFor(() => expect(screen.getByText('No sign-ups are waiting.')).toBeTruthy());
    expect(screen.getByText('No approved sign-up accounts.')).toBeTruthy();
  });

  it('shows a loading status with hidden text, then an error with Retry', async () => {
    let fail = true;
    let release: () => void = () => undefined;
    const held = new Promise<void>((r) => (release = r));
    await mount(async () => {
      await held;
      return fail ? json(503, { error: 'unavailable' }) : json(200, fx().empty);
    });
    expect(screen.getByRole('status').textContent).toBe('Loading sign-ups');
    expect(screen.getByRole('status').getAttribute('aria-busy')).toBeNull();
    await act(async () => release());
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy());
    fail = false;
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(screen.getByText('No sign-ups are waiting.')).toBeTruthy());
  });

  it('renders markup and bidi characters in names as text', async () => {
    await mount(() => json(200, fx().one));
    await waitFor(() => expect(screen.getByText(/Petr <b>bold<\/b>/)).toBeTruthy());
    expect(document.querySelector('b')).toBeNull();
  });
});

describe('approve and reject (FR-ACCOUNTS-2)', () => {
  const one = () => fx().one;
  const id = () => one().pending[0]!.id;

  it('approves at once with {} and announces it', async () => {
    let body: Body = one();
    const { calls } = await mount(() => json(200, body), {
      [`POST /api/auth/signups/${id()}/approve`]: () => {
        body = { pending: [], accounts: [...one().accounts] };
        return json(200, { status: 'active' });
      },
    });
    await waitFor(() => screen.getByRole('button', { name: 'Approve @lc-1a2b' }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve @lc-1a2b' }));
    await waitFor(() => expect(screen.getByText('@lc-1a2b approved.')).toBeTruthy());
    expect(calls.filter((c) => c.method !== 'GET')).toEqual([{ method: 'POST', url: `/api/auth/signups/${id()}/approve`, body: {} }]);
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('heading', { level: 1, name: 'Sign-ups' })));
  });

  it('asks before rejecting, focuses Cancel, and Cancel sends nothing', async () => {
    const { calls } = await mount(() => json(200, one()));
    await waitFor(() => screen.getByRole('button', { name: 'Reject @lc-1a2b' }));
    fireEvent.click(screen.getByRole('button', { name: 'Reject @lc-1a2b' }));
    expect(screen.getByText('Reject @lc-1a2b? The request is deleted and the handle becomes free.')).toBeTruthy();
    expect(document.activeElement?.textContent).toBe('Cancel');
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(calls.filter((c) => c.method !== 'GET')).toEqual([]);
  });

  it('rejects after confirmation', async () => {
    let body: Body = one();
    const { calls } = await mount(() => json(200, body), {
      [`POST /api/auth/signups/${id()}/reject`]: () => {
        body = { pending: [], accounts: one().accounts };
        return json(204, null);
      },
    });
    await waitFor(() => screen.getByRole('button', { name: 'Reject @lc-1a2b' }));
    fireEvent.click(screen.getByRole('button', { name: 'Reject @lc-1a2b' }));
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }));
    await waitFor(() => expect(screen.getByText('@lc-1a2b rejected.')).toBeTruthy());
    expect(calls.filter((c) => c.method !== 'GET')).toEqual([{ method: 'POST', url: `/api/auth/signups/${id()}/reject`, body: {} }]);
  });

  it.each([
    ['reauth_required', 'Sign in again to approve or reject (required every 10 minutes for these actions).'],
    ['not_found', 'This request is no longer waiting.'],
    ['server_error', 'Could not reach reader-ai. Try again.'],
  ])('maps %s on approve to its message, never the server text', async (outcome, message) => {
    const o = fx().outcomes[outcome]!;
    const { onUnauthorized } = await mount(() => json(200, one()), {
      [`POST /api/auth/signups/${id()}/approve`]: () => json(o.status, o.body),
    });
    await waitFor(() => screen.getByRole('button', { name: 'Approve @lc-1a2b' }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve @lc-1a2b' }));
    await waitFor(() => expect(screen.getByText(message)).toBeTruthy());
    expect(document.body.textContent).not.toContain('unavailable');
    expect(onUnauthorized).not.toHaveBeenCalled();
    if (outcome === 'reauth_required') {
      const link = screen.getByRole('link', { name: /sign in again/i });
      expect(link.getAttribute('href')).toBe('/api/auth/signin?next=/signups');
    }
    if (outcome === 'server_error') expect((screen.getByRole('button', { name: 'Approve @lc-1a2b' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('hands an unauthenticated 401 to the shell redirect', async () => {
    const { onUnauthorized } = await mount(() => json(200, one()), {
      [`POST /api/auth/signups/${id()}/approve`]: () => json(401, { error: 'unauthenticated' }),
    });
    await waitFor(() => screen.getByRole('button', { name: 'Approve @lc-1a2b' }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve @lc-1a2b' }));
    await waitFor(() => expect(onUnauthorized).toHaveBeenCalled());
  });

  it('a network failure keeps the buttons usable', async () => {
    await mount(() => json(200, one()), {
      [`POST /api/auth/signups/${id()}/reject`]: () => {
        throw new TypeError('network');
      },
    });
    await waitFor(() => screen.getByRole('button', { name: 'Reject @lc-1a2b' }));
    fireEvent.click(screen.getByRole('button', { name: 'Reject @lc-1a2b' }));
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }));
    await waitFor(() => expect(screen.getByText('Could not reach reader-ai. Try again.')).toBeTruthy());
  });
});

describe('approved accounts (FR-ACCOUNTS-3)', () => {
  it('lists accounts, disabled ones as text, and the lost-passkey help', async () => {
    await mount(() => json(200, fx().one));
    await waitFor(() => screen.getByRole('heading', { level: 2, name: 'Accounts' }));
    expect(screen.getByRole('button', { name: 'Disable @maria' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Disable @petr/ })).toBeNull();
    expect(screen.getByText('Disabled')).toBeTruthy();
    expect(
      screen.getByText('A sign-up account that loses its passkey cannot recover it. Disable it; the person can sign up again with another handle.'),
    ).toBeTruthy();
  });

  it('disables after a confirmation that says Enable undoes it (ADR-0037)', async () => {
    const maria = fx().one.accounts[0]!;
    const { calls } = await mount(() => json(200, fx().one), {
      [`POST /api/auth/signups/${maria.id}/disable`]: () => json(204, null),
    });
    await waitFor(() => screen.getByRole('button', { name: 'Disable @maria' }));
    fireEvent.click(screen.getByRole('button', { name: 'Disable @maria' }));
    expect(screen.getByText('Disable @maria? Their sessions end and they can no longer sign in. Enable undoes it.')).toBeTruthy();
    expect(screen.queryByText(/This cannot be undone here\./)).toBeNull();
    expect(document.activeElement?.textContent).toBe('Cancel');
    fireEvent.click(screen.getByRole('button', { name: 'Disable' }));
    await waitFor(() => expect(calls.filter((c) => c.method !== 'GET')).toEqual([{ method: 'POST', url: `/api/auth/signups/${maria.id}/disable`, body: {} }]));
  });

  it('enables a disabled account with one click (WEB-MANAGE paired change, SRV-AUTH FR-AUTH-48)', async () => {
    const disabled = fx().one.accounts.find((a: Row) => a.disabled)!;
    const { calls } = await mount(() => json(200, fx().one), {
      [`POST /api/auth/signups/${disabled.id}/enable`]: () => json(200, { status: 'active' }),
    });
    const enable = await waitFor(() => screen.getByRole('button', { name: `Enable @${disabled.handle}` }));
    fireEvent.click(enable);
    await waitFor(() => expect(calls.filter((c) => c.method !== 'GET')).toEqual([{ method: 'POST', url: `/api/auth/signups/${disabled.id}/enable`, body: {} }]));
  });
});

describe('no state change without a click (INV-ACCOUNTS-1)', () => {
  it('load, refetch, focus and keyboard navigation send only GETs', async () => {
    const { calls, client } = await mount(() => json(200, fx().one));
    await waitFor(() => screen.getByRole('button', { name: 'Approve @lc-1a2b' }));
    await act(async () => {
      await client.refetchQueries();
      window.dispatchEvent(new Event('focus'));
      document.dispatchEvent(new Event('visibilitychange'));
    });
    for (const b of screen.getAllByRole('button')) {
      act(() => b.focus());
      fireEvent.keyDown(b, { key: 'Tab' });
    }
    expect(calls.every((c) => c.method === 'GET')).toBe(true);
  });
});

describe('text only (INV-ACCOUNTS-2)', () => {
  it('web/src/accounts has no HTML-string sink', () => {
    const dir = join(process.cwd(), 'src', 'accounts');
    const files = readdirSync(dir).filter((f) => /\.(ts|tsx)$/.test(f) && !/\.(test|stories)\.tsx?$/.test(f));
    expect(files.length).toBeGreaterThan(0);
    for (const f of files) expect(findForbidden(readFileSync(join(dir, f), 'utf8')), f).toEqual([]);
  });
});
