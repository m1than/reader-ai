// @trace FR-NOTEBOOK-12
// The not-published lesson page offers the owner WEB-UI FR-UI-15's compile control and renders the lesson once a
// compile has written a revision; a learner's page is unchanged. Stub fetch only; nothing starts a real compile.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { readJson } from '../app/testing/fixtures';
import { load } from '../app/testing/load';

afterEach(cleanup);

const owner = { id: '0b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b10', handle: 'ivan', role: 'owner', display_name: 'Ivan', consent: { evaluation: true, tuning: false } };
const learner = { ...owner, handle: 'anna', role: 'learner', display_name: 'Anna' };
const SHA = '1'.repeat(64);
const notebook = () => readJson<Record<string, unknown>>('spec/fixtures/WEB-NOTEBOOK/lesson-w01.ipynb');
const base = () => ({ ...readJson<{ lesson: { lesson: Record<string, unknown> } }>('spec/fixtures/WEB-UI/lessons.json').lesson.lesson, lesson_id: 'w02', ordinal: 2, title: 'Fixture lesson W02' });

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

const run = (state: string, extra: Record<string, unknown> = {}) => ({
  run_id: 'run-1', lesson_id: 'w02', state, cap_usd: '8.00', steps: [{ step: 1, stage: 'draft', status: 'running', reused: false }],
  spent_usd: '0.00', task_budgets: {}, refusals: [], g2: null, latest_sha256: null, ...extra,
});

const mount = async (handler: (url: string, init?: RequestInit) => Response) => {
  const { AppRoot } = await load(() => import('../app/AppRoot'));
  expect(AppRoot).toBeTypeOf('function');
  const calls: { method: string; url: string }[] = [];
  const fetchFn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ method: init?.method ?? 'GET', url: String(input) });
    return handler(String(input), init);
  });
  await act(async () => {
    render(<AppRoot initialPath="/lessons/w02" fetch={fetchFn as unknown as typeof fetch} navigateAway={vi.fn()} />);
  });
  return calls;
};

describe('not-published page (FR-NOTEBOOK-12, amended)', () => {
  it('a learner sees the text and the back link only, and no compile request is made', async () => {
    const calls = await mount((url) =>
      url === '/api/me' ? json(200, learner) : url === '/api/lessons/w02' ? json(200, { lesson: { ...base(), latest_sha256: null }, revisions: [] }) : json(404, { error: 'not_found' }),
    );
    await waitFor(() => expect(screen.getByText('This lesson is not published yet.')).toBeTruthy());
    expect(screen.getByRole('link', { name: 'Lessons' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Compile' })).toBeNull();
    expect(calls.filter((c) => /\/compile$|\/estimate$|\/api\/compiles\//.test(c.url))).toEqual([]);
  });

  it('the owner gets the status and the Compile button, and the confirm posts the compile', async () => {
    let posted = false;
    const calls = await mount((url, init) => {
      if (url === '/api/me') return json(200, owner);
      if (url === '/api/lessons/w02') return json(200, { lesson: { ...base(), latest_sha256: null }, revisions: [] });
      if (url === '/api/lessons/w02/estimate')
        return json(200, { lesson_id: 'w02', kchars: '22.104', low_usd: '0.47', high_usd: '1.11', run_cap_usd: '8.00', basis: 'L02', basis_kchars: '58.938', extrapolated: true });
      if (init?.method === 'POST' && url === '/api/lessons/w02/compile') {
        posted = true;
        return json(202, { run_id: 'run-1' });
      }
      if (url === '/api/lessons/w02/compile') return posted ? json(200, run('running')) : json(404, { error: 'not_found' });
      return json(404, { error: 'not_found' });
    });
    await waitFor(() => expect(screen.getByText('not compiled yet')).toBeTruthy());
    expect(screen.getByText('This lesson is not published yet.')).toBeTruthy();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Compile' }));
    });
    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('Estimate: $0.47–$1.11')).toBeTruthy());
    expect(within(dialog).getByText('Stops at $8.00.')).toBeTruthy();
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Compile' }));
    });
    expect(calls.filter((c) => c.method === 'POST').map((c) => c.url)).toEqual(['/api/lessons/w02/compile']);
    expect(await screen.findByText('Compiling: drafting')).toBeTruthy();
  });

  it('renders the lesson once the status reports a revision', async () => {
    let reported = false;
    await mount((url) => {
      if (url === '/api/me') return json(200, owner);
      if (url === '/api/lessons/w02')
        return json(200, reported
          ? { lesson: { ...base(), latest_sha256: SHA }, revisions: [{ sha256: SHA, byte_size: 1, cell_count: 1, created_at: '2026-11-23T09:00:00Z', stages: ['draft'] }] }
          : { lesson: { ...base(), latest_sha256: null }, revisions: [] });
      if (url === '/api/lessons/w02/compile') {
        reported = true;
        return json(200, run('converged', { latest_sha256: SHA, steps: [{ step: 1, stage: 'draft', model: 'm', cost_usd: '0.6', duration_s: 1, reused: false }] }));
      }
      if (url === `/api/lessons/w02/revisions/${SHA}`) return json(200, notebook());
      return json(404, { error: 'not_found' });
    });
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Digests' })).toBeTruthy());
    expect(screen.queryByText('This lesson is not published yet.')).toBeNull();
  });
});
