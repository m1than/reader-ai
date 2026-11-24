// @trace FR-UI-33 FR-OBS-23 FR-OBS-24 FR-OBS-25 FR-OBS-32
// WEB-UI FR-UI-33 (paired SRV-OBS phase 2): on /observability, the bench upload (two files, every error code shown),
// a label control on lesson and answer rows (the exact body; revision and mirror shown), one text-only banner per
// alert, and the stack row.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { readText, readYaml } from '../testing/fixtures';
import { load } from '../testing/load';

afterEach(cleanup);

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const LESSON = '20000000-0000-4000-8000-000000000001';
const QUESTION = '30000000-0000-4000-8000-000000000003';
const IMG = '<img src=x onerror=alert(1)>';

const base = (): Record<string, unknown> => {
  const s = readYaml<{ expected: Record<string, unknown> }>('spec/fixtures/SRV-OBS/summary.yaml').expected;
  return {
    days: 7,
    ...s,
    alerts: [
      { code: 'daily_spend_high', subject: 'tutor', value: 2.5, limit: 2.4 },
      { code: 'obs_unhealthy', subject: IMG, value: 0, limit: 1 },
    ],
    stack: { mlflow: { ok: true, checked_at: '2026-11-25T12:00:00Z' }, rotel: { ok: false, checked_at: '2026-11-25T11:30:00Z' } },
  };
};

type Call = { url: string; method: string; body: unknown };

const mount = async (post: (url: string, body: unknown) => Response) => {
  const page = await load(() => import('./ObservabilityPage'));
  const api = await load(() => import('./api'));
  const ctx = await load(() => import('../context'));
  const rq = await import('@tanstack/react-query');
  const calls: Call[] = [];
  const fetchFn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ url: String(input), method, body });
    return method === 'GET' ? json(200, base()) : post(String(input), body);
  });
  const obs = api.createObsApi({ fetch: fetchFn as unknown as typeof fetch, onUnauthorized: vi.fn() });
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
  return calls;
};

const file = (name: string, text: string) => new File([text], name, { type: 'text/plain' });

const upload = async (setText: string, resultText: string) => {
  const region = await waitFor(() => screen.getByRole('region', { name: 'Bench upload' }));
  await act(async () => {
    fireEvent.change(within(region).getByLabelText('Set file'), { target: { files: [file('set.yaml', setText)] } });
    fireEvent.change(within(region).getByLabelText('Result file'), { target: { files: [file('result.json', resultText)] } });
  });
  await act(async () => {
    fireEvent.click(within(region).getByRole('button', { name: 'Upload' }));
  });
  return region;
};

describe('bench upload (FR-UI-33 a, FR-OBS-23)', () => {
  const SET = readText('spikes/embeddings-local/questions.yaml');
  const RESULT = JSON.stringify({ kind: 'search', model: 'BAAI/bge-m3', chunk_chars: 1200, overlap: 200, questions: 1, per_question: [] });

  it('posts both files and shows "recorded"', async () => {
    const calls = await mount(() => json(202, { upload_id: '60000000-0000-4000-8000-000000000001' }));
    const region = await upload(SET, RESULT);
    const post = calls.find((c) => c.method === 'POST')!;
    expect(post.url).toBe('/api/obs/bench');
    expect(post.body).toEqual({ set: SET, result: JSON.parse(RESULT) });
    await waitFor(() => expect(within(region).getByText('recorded')).toBeTruthy());
  });

  for (const [status, code] of [[413, 'too_large'], [400, 'set_not_pinned'], [400, 'bench_invalid'], [409, 'already_recorded']] as const) {
    it(`shows ${code} by its code`, async () => {
      await mount(() => json(status, { error: code }));
      const region = await upload(SET, RESULT);
      await waitFor(() => expect(within(region).getByText(code)).toBeTruthy());
    });
  }
});

describe('labels (FR-UI-33 b, FR-OBS-24)', () => {
  it('a lesson label posts the exact body and shows revision and mirror', async () => {
    const calls = await mount(() => json(200, { revision: 2, mirror: 'queued' }));
    const region = await waitFor(() => screen.getByRole('region', { name: 'Cost per lesson' }));
    const row = within(region).getByText('Lesson one').closest('tr')!;
    await act(async () => {
      fireEvent.change(within(row).getByLabelText('Reason'), { target: { value: 'wrong_fact' } });
      fireEvent.click(within(row).getByRole('button', { name: 'Bad' }));
    });
    const post = calls.find((c) => c.method === 'POST')!;
    expect(post.url).toBe('/api/obs/labels');
    expect(post.body).toEqual({ target: 'lesson', id: LESSON, ok: false, reason: 'wrong_fact' });
    await waitFor(() => expect(within(row).getByText('revision 2, mirror queued')).toBeTruthy());
  });

  it('an answer label posts target answer; a 404 shows "not found"', async () => {
    const calls = await mount(() => json(404, { error: 'not_found' }));
    const region = await waitFor(() => screen.getByRole('region', { name: 'Tutor' }));
    const row = within(region).getByText(QUESTION).closest('tr')!;
    await act(async () => {
      fireEvent.click(within(row).getByRole('button', { name: 'Good' }));
    });
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ target: 'answer', id: QUESTION, ok: true });
    await waitFor(() => expect(within(row).getByText('not found')).toBeTruthy());
  });

  it('offers exactly the VEC-OBS-5 reasons', async () => {
    await mount(() => json(200, {}));
    const region = await waitFor(() => screen.getByRole('region', { name: 'Cost per lesson' }));
    const select = within(within(region).getByText('Lesson one').closest('tr')!).getByLabelText('Reason') as HTMLSelectElement;
    const reasons = readYaml<{ labels: { reasons: string[] } }>('spec/fixtures/SRV-OBS/phase2.yaml').labels.reasons;
    expect([...select.options].map((o) => o.value).filter(Boolean)).toEqual(reasons);
  });
});

describe('alerts and stack (FR-UI-33 c, d; FR-OBS-25, -32)', () => {
  it('one banner per alert, as text', async () => {
    await mount(() => json(200, {}));
    const banners = await waitFor(() => screen.getAllByRole('alert'));
    expect(banners).toHaveLength(2);
    expect(banners[0]?.textContent).toContain('daily_spend_high');
    expect(banners[1]?.textContent).toContain(IMG); // stays text
    expect(document.querySelector('img')).toBeNull();
  });

  it('the stack row: mlflow, rotel and jaeger', async () => {
    await mount(() => json(200, {}));
    const region = await waitFor(() => screen.getByRole('region', { name: 'Stack' }));
    expect(within(region).getByText(/mlflow/).textContent).toMatch(/healthy/);
    expect(within(region).getByText(/rotel/).textContent).toMatch(/not healthy/);
    expect(within(region).getByText(/jaeger/).textContent).toContain('see reader doctor');
  });
});
