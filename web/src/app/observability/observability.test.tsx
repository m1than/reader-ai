// @trace FR-UI-18 FR-OBS-10
// The owner's observability page (WEB-UI FR-UI-18, SRV-OBS FR-OBS-10): four views of GET /api/obs/summary, trace
// links to the 8443 dashboard, text-only values, "More rows may exist." when a view is truncated, owner only.
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { readYaml, webRoot } from '../testing/fixtures';
import { load } from '../testing/load';

afterEach(cleanup);

type Node = unknown;
// `job: 101` and `jobs: [106]` name seeded rows; `failures.jobs`, a list of groups, stays.
const rowRef = (k: string, v: Node): boolean =>
  (k === 'job' && typeof v === 'number') || (k === 'jobs' && Array.isArray(v) && v.every((x) => typeof x === 'number'));
const strip = (n: Node): Node =>
  Array.isArray(n)
    ? n.map(strip)
    : n && typeof n === 'object'
      ? Object.fromEntries(Object.entries(n as Record<string, Node>).filter(([k, v]) => !rowRef(k, v)).map(([k, v]) => [k, strip(v)]))
      : n;
const summary = (): Record<string, unknown> => ({ days: 7, ...(strip(readYaml<{ expected: object }>('spec/fixtures/SRV-OBS/summary.yaml').expected) as object) });
const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const DEEP = 'https://localhost:8443/#/experiments/0/traces?selectedEvaluationId=tr-';

const mountPage = async (respond: () => Response) => {
  const page = await load(() => import('./ObservabilityPage'));
  const api = await load(() => import('./api'));
  const ctx = await load(() => import('../context'));
  const rq = await import('@tanstack/react-query');
  expect(page.ObservabilityPage).toBeTypeOf('function');
  expect(api.createObsApi).toBeTypeOf('function');
  const calls: string[] = [];
  const fetchFn = vi.fn(async (input: RequestInfo | URL) => {
    calls.push(String(input));
    return respond();
  });
  const onUnauthorized = vi.fn();
  const obs = api.createObsApi({ fetch: fetchFn as unknown as typeof fetch, onUnauthorized });
  const client = new rq.QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } });
  await act(async () => {
    render(
      <ctx.ServicesContext.Provider value={{ api: {} as never, notify: vi.fn(), navigateAway: vi.fn(), obs }}>
        <rq.QueryClientProvider client={client}>
          <main id="content" tabIndex={-1}>
            <page.ObservabilityPage />
          </main>
        </rq.QueryClientProvider>
      </ctx.ServicesContext.Provider>,
    );
  });
  return { calls, onUnauthorized };
};

describe('observability page (FR-UI-18)', () => {
  it('renders the four views with trace links', async () => {
    const { calls } = await mountPage(() => json(200, summary()));
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Compile runs' })).toBeTruthy());
    expect(calls).toEqual(['/api/obs/summary?days=7']);
    for (const name of ['Tutor', 'Failures', 'Ingest']) expect(screen.getByRole('heading', { name })).toBeTruthy();
    expect(screen.getByText('fact_check')).toBeTruthy();
    expect(screen.getByText('1.86')).toBeTruthy();
    expect(screen.getByText('4000', { selector: '[data-metric="p50"]' })).toBeTruthy(); // tutor p50 ms
    expect(screen.getByText('9000', { selector: '[data-metric="p95"]' })).toBeTruthy();
    expect(screen.getByText('297.0')).toBeTruthy(); // pages per minute (import)
    const links = screen.getAllByRole('link', { name: 'Trace' });
    const hrefs = links.map((a) => a.getAttribute('href'));
    expect(hrefs).toContain(`${DEEP}cf8a1cbe5cc23a546382dc30e5e2b16a`);
    expect(hrefs).toContain(`${DEEP}c7c885850da1d6bbde90e0f9302cbf7f`);
    for (const a of links) expect(a.getAttribute('rel')).toBe('noopener noreferrer');
    const failures = screen.getByRole('region', { name: 'Failures' });
    expect(within(failures).getAllByText('system')).toHaveLength(2);
    expect(screen.queryByText('More rows may exist.')).toBeNull();
  });

  it('shows "no trace" and no link where no trace can exist', async () => {
    const body = summary() as {
      ingest: { rows: Array<{ trace_id: string | null }> };
      failures: { jobs: Array<{ trace_ids: string[] }> };
    };
    body.ingest.rows[0]!.trace_id = null;
    for (const g of body.failures.jobs) g.trace_ids = [];
    await mountPage(() => json(200, body));
    const ingest = await waitFor(() => screen.getByRole('region', { name: 'Ingest' }));
    expect(within(ingest).getByText('no trace')).toBeTruthy();
    expect(within(ingest).queryByRole('link', { name: 'Trace' })).toBeNull();
    const failures = screen.getByRole('region', { name: 'Failures' });
    expect(within(failures).getAllByText('no trace')).toHaveLength(3);
  });

  it('says more rows may exist when a view is truncated', async () => {
    const body = summary() as { failures: { truncated: boolean } };
    body.failures.truncated = true;
    await mountPage(() => json(200, body));
    await waitFor(() => expect(screen.getByText('More rows may exist.')).toBeTruthy());
  });

  it('renders values as text only', async () => {
    const body = summary() as { failures: { jobs: Array<{ kind: string }> } };
    body.failures.jobs[0]!.kind = '<img src=x onerror=alert(1)>';
    await mountPage(() => json(200, body));
    await waitFor(() => expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeTruthy());
    expect(document.querySelector('main img')).toBeNull();
  });

  it('refuses an invalid body and offers a retry', async () => {
    await mountPage(() => json(200, { nope: true }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy());
  });

  it('sends a 401 to the sign-in redirect', async () => {
    const { onUnauthorized } = await mountPage(() => json(401, { error: 'unauthenticated' }));
    await waitFor(() => expect(onUnauthorized).toHaveBeenCalled());
  });
});

const user = (role: string) => ({ id: '0b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b10', handle: 'ivan', role, display_name: 'Ivan', consent: { evaluation: true, tuning: false } });

const mountApp = async (role: string) => {
  const { AppRoot } = await load(() => import('../AppRoot'));
  expect(AppRoot).toBeTypeOf('function');
  const urls: string[] = [];
  const fetchFn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    urls.push(url);
    if (url === '/api/me') return json(200, user(role));
    if (url === '/api/lessons') return json(200, { lessons: [] });
    if (url.startsWith('/api/obs/summary')) return json(200, summary());
    return json(404, { error: 'not_found' });
  });
  await act(async () => {
    render(<AppRoot initialPath="/observability" fetch={fetchFn as unknown as typeof fetch} navigateAway={vi.fn()} />);
  });
  return urls;
};

describe('observability route (FR-UI-18)', () => {
  it('serves the owner, with a header link', async () => {
    await mountApp('owner');
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Observability', level: 1 })).toBeTruthy());
    expect(screen.getByRole('link', { name: 'Observability' }).getAttribute('href')).toBe('/observability');
  });

  it('shows not-found to anyone else without any /api/obs request', async () => {
    const urls = await mountApp('learner');
    await waitFor(() => expect(screen.getByText(/not found/i)).toBeTruthy());
    expect(urls.some((u) => u.startsWith('/api/obs'))).toBe(false);
    expect(screen.queryByRole('link', { name: 'Observability' })).toBeNull();
  });

  it('loads the page as a lazy chunk', () => {
    const routes = readFileSync(`${webRoot}src/app/routes.tsx`, 'utf8');
    // FR-UI-31: the lazy route loads its Russian catalogues with its chunk (withAreas).
    expect(routes).toMatch(/lazy\(\s*withAreas\(\(\) => import\('\.\/observability\/ObservabilityPage'\)/);
    expect(routes).not.toMatch(/^import .*ObservabilityPage/m);
  });
});
