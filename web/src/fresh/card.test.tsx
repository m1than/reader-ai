// @trace FR-FRESHUI-1 FR-FRESHUI-2 FR-FRESHUI-3 FR-FRESHUI-4 FR-FRESHUI-5 FR-FRESHUI-6 VEC-FRESHUI-1
// The freshness card over the VEC-FRESHUI-1 bodies, with a fake client.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../app/api';
import { readJson } from '../app/testing/fixtures';
import { load } from '../app/testing/load';

afterEach(cleanup);

type Body = Record<string, unknown> & { report: Record<string, unknown> | null; estimate: Record<string, string> };
interface Bodies {
  states: Record<string, { material_id: string; expect: string[]; body: Body }>;
  errors: Record<string, { status: number; body: Record<string, unknown> }>;
  post_accepted: { status: number; body: { report_id: string } };
  bibliography_saved: Record<string, unknown>;
}
const bodies = () => readJson<Bodies>('spec/fixtures/WEB-FRESH/bodies.json');
const state = (name: string) => bodies().states[name]!;

/** A server error as the client raises it: the code and the whole body. */
const httpError = (name: string) => {
  const e = bodies().errors[name]!;
  return Object.assign(new ApiError(e.status, String(e.body.error)), { body: e.body });
};

const fakeApi = (gets: Array<unknown | Error>) => {
  let i = 0;
  return {
    get: vi.fn(async () => {
      const next = gets[Math.min(i, gets.length - 1)];
      i += 1;
      if (next instanceof Error) throw next;
      return structuredClone(next);
    }),
    saveBibliography: vi.fn(async () => structuredClone(bodies().bibliography_saved)),
    run: vi.fn(async () => structuredClone(bodies().post_accepted.body)),
  };
};

type FakeApi = ReturnType<typeof fakeApi>;

const show = async (api: FakeApi, materialId = state('estimate').material_id, extra: Record<string, unknown> = {}) => {
  const { FreshnessCard } = await load(() => import('./FreshnessCard'));
  expect(FreshnessCard).toBeTypeOf('function');
  await act(async () => {
    render(<FreshnessCard materialId={materialId} api={api as never} {...extra} />);
  });
  await waitFor(() => expect(api.get).toHaveBeenCalled());
};

const click = async (name: string | RegExp) => {
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name }));
  });
};

describe('every VEC-FRESHUI-1 state (VEC-FRESHUI-1)', () => {
  it('shows the texts the fixture expects', async () => {
    for (const [name, s] of Object.entries(bodies().states)) {
      const api = fakeApi([s.body]);
      await show(api, s.material_id, { pollMs: 60_000 });
      // The estimate state is the card after "Run check" (FR-FRESHUI-3).
      if (name === 'estimate') await click('Run check');
      for (const text of s.expect) await waitFor(() => expect(screen.getAllByText(text, { exact: false }).length, `${name}: ${text}`).toBeGreaterThan(0));
      cleanup();
    }
  });
});

describe('data and loading (FR-FRESHUI-1)', () => {
  it('loads GET …/freshness for the material and shows a status while loading', async () => {
    const { FreshnessCard } = await load(() => import('./FreshnessCard'));
    expect(FreshnessCard).toBeTypeOf('function');
    const api = { get: vi.fn(() => new Promise(() => undefined)), saveBibliography: vi.fn(), run: vi.fn() };
    render(<FreshnessCard materialId="m-1" api={api as never} />);
    expect(api.get).toHaveBeenCalledWith('m-1');
    const status = screen.getByRole('status');
    expect(status.textContent).toMatch(/loading/i);
    expect(status.className).toContain('visually-hidden');
  });

  it('an invalid body shows the error state; Retry loads again', async () => {
    const api = fakeApi([new ApiError(200, 'bad_response'), state('current').body]);
    await show(api);
    await waitFor(() => expect(screen.getByText(/could not be loaded/i)).toBeTruthy());
    await click('Retry');
    await waitFor(() => expect(screen.getByText('Current')).toBeTruthy());
    expect(api.get).toHaveBeenCalledTimes(2);
  });

  it('the route /materials/{id}/freshness renders the card in the shell; another id is not found', async () => {
    const { AppRoot } = await load(() => import('../app/AppRoot'));
    const s = state('current');
    const me = { id: '0b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b10', handle: 'ivan', role: 'owner', display_name: 'Ivan', consent: { evaluation: true, tuning: false } };
    const urls: string[] = [];
    const fetchFn = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      urls.push(url);
      const body = url === '/api/me' ? me : url === `/api/materials/${s.material_id}/freshness` ? s.body : { error: 'not_found' };
      return new Response(JSON.stringify(body), { status: body === s.body || body === me ? 200 : 404, headers: { 'Content-Type': 'application/json' } });
    });
    await act(async () => {
      render(<AppRoot initialPath={`/materials/${s.material_id}/freshness`} fetch={fetchFn as unknown as typeof fetch} navigateAway={vi.fn()} />);
    });
    await waitFor(() => expect(screen.getByText('Current')).toBeTruthy(), { timeout: 5000 });
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Source freshness');
    cleanup();
    await act(async () => {
      render(<AppRoot initialPath="/materials/not-a-uuid/freshness" fetch={fetchFn as unknown as typeof fetch} navigateAway={vi.fn()} />);
    });
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Not found' })).toBeTruthy());
    expect(urls.filter((u) => u.includes('not-a-uuid'))).toEqual([]);
  });
});

describe('bibliography (FR-FRESHUI-2)', () => {
  it('shows every field and its source', async () => {
    await show(fakeApi([state('estimate').body]));
    const bib = screen.getByRole('region', { name: 'Bibliography' });
    for (const text of ['Practical Distributed Ledgers', 'Ada Example, Grace Sample', '2nd', '2017', 'Example Press', '0306406152', 'Distributed Ledgers in Practice', '1st', '2015', 'from you'])
      expect(within(bib).getAllByText(text, { exact: false }).length, text).toBeGreaterThan(0);
  });

  it('Edit opens the form; Save sends PUT …/bibliography with the fields and reloads', async () => {
    const api = fakeApi([state('estimate').body]);
    await show(api);
    await click('Edit');
    const title = screen.getByLabelText('Title') as HTMLInputElement;
    expect(title.value).toBe('Practical Distributed Ledgers');
    fireEvent.change(title, { target: { value: 'Practical Distributed Ledgers, 3rd' } });
    fireEvent.change(screen.getByLabelText(/Authors/), { target: { value: 'Ada Example\nGrace Sample\n' } });
    fireEvent.change(screen.getByLabelText('Edition'), { target: { value: '3rd' } });
    fireEvent.change(screen.getByLabelText('Year'), { target: { value: '2019' } });
    fireEvent.change(screen.getByLabelText('ISBN'), { target: { value: '978-0-306-40615-7' } });
    fireEvent.change(screen.getByLabelText('Original title'), { target: { value: '' } });
    fireEvent.change(screen.getByLabelText('Original edition'), { target: { value: '' } });
    fireEvent.change(screen.getByLabelText('Original year'), { target: { value: '' } });
    await click('Save');
    expect(api.saveBibliography).toHaveBeenCalledWith(state('estimate').material_id, {
      title: 'Practical Distributed Ledgers, 3rd',
      authors: ['Ada Example', 'Grace Sample'],
      edition: '3rd',
      year: 2019,
      publisher: 'Example Press',
      isbn: '9780306406157',
      original_title: null,
      original_authors: ['Ada Example'],
      original_edition: null,
      original_year: null,
    });
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
  });

  it('client checks follow SRV-FRESH FR-FRESH-1 bounds and send nothing when they fail', async () => {
    const api = fakeApi([state('estimate').body]);
    await show(api);
    await click('Edit');
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'x'.repeat(201) } });
    fireEvent.change(screen.getByLabelText(/Authors/), { target: { value: Array.from({ length: 11 }, (_, i) => `A${i}`).join('\n') } });
    fireEvent.change(screen.getByLabelText('Edition'), { target: { value: 'e'.repeat(41) } });
    fireEvent.change(screen.getByLabelText('Year'), { target: { value: '1449' } });
    fireEvent.change(screen.getByLabelText('Publisher'), { target: { value: 'p'.repeat(121) } });
    fireEvent.change(screen.getByLabelText('ISBN'), { target: { value: '9780306406158' } });
    fireEvent.change(screen.getByLabelText('Original year'), { target: { value: String(new Date().getUTCFullYear() + 2) } });
    await click('Save');
    expect(api.saveBibliography).not.toHaveBeenCalled();
    for (const label of ['Title', /Authors/, 'Edition', 'Year', 'Publisher', 'ISBN', 'Original year']) {
      const input = screen.getByLabelText(label);
      expect(input.getAttribute('aria-invalid'), String(label)).toBe('true');
      const described = input.getAttribute('aria-describedby') ?? '';
      expect(described, String(label)).not.toBe('');
      expect(document.getElementById(described.split(' ')[0]!)?.textContent, String(label)).not.toBe('');
    }
    // An empty title and an author over 100 characters are refused too.
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: ' ' } });
    fireEvent.change(screen.getByLabelText(/Authors/), { target: { value: 'a'.repeat(101) } });
    await click('Save');
    expect(api.saveBibliography).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Title').getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByLabelText(/Authors/).getAttribute('aria-invalid')).toBe('true');
  });

  it("a 400 shows the server's field error", async () => {
    const api = fakeApi([state('estimate').body]);
    api.saveBibliography.mockRejectedValueOnce(httpError('bibliography_invalid'));
    await show(api);
    await click('Edit');
    await click('Save');
    await waitFor(() => expect(screen.getByLabelText('ISBN').getAttribute('aria-invalid')).toBe('true'));
  });

  it('a scan-read bibliography asks for confirmation; checking it sends the server sha256 with the run', async () => {
    const s = state('scan_only');
    const api = fakeApi([s.body]);
    await show(api, s.material_id);
    expect(screen.getByText("read from the book's first pages", { exact: false })).toBeTruthy();
    expect(screen.getByText('Check these details before the web search', { exact: false })).toBeTruthy();
    const box = screen.getByRole('checkbox', { name: 'These details are right' });
    fireEvent.click(box);
    await click(/^Run (check|again)$/);
    await click('Run for up to $0.33');
    expect(api.run).toHaveBeenCalledWith(s.material_id, { high_usd: '0.33', bibliography_sha256: 'c5'.repeat(32) });
  });

  it('without the confirmation the run carries no bibliography_sha256', async () => {
    const s = state('scan_only');
    const api = fakeApi([s.body]);
    await show(api, s.material_id);
    await click(/^Run (check|again)$/);
    await click('Run for up to $0.33');
    expect(api.run).toHaveBeenCalledWith(s.material_id, { high_usd: '0.33' });
    expect(Object.keys((api.run.mock.calls[0] as unknown[])[1] as object)).toEqual(['high_usd']);
  });

  it('an owner bibliography shows no confirmation', async () => {
    await show(fakeApi([state('estimate').body]));
    expect(screen.queryByRole('checkbox')).toBeNull();
  });
});

describe('run with the estimate (FR-FRESHUI-3)', () => {
  it('Run check shows the estimate and needs a second click to post high_usd', async () => {
    const api = fakeApi([state('estimate').body, state('running').body]);
    await show(api, state('estimate').material_id, { pollMs: 60_000 });
    await click('Run check');
    expect(screen.getByText('About $0.27–$0.39, at most $1.00')).toBeTruthy();
    expect(api.run).not.toHaveBeenCalled();
    await click('Run for up to $0.39');
    expect(api.run).toHaveBeenCalledWith(state('estimate').material_id, { high_usd: '0.39' });
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Searching the web (2 of 6)'));
  });

  it('shows money exactly as the server wrote it', async () => {
    const body = { ...state('estimate').body, estimate: { low_usd: '0.2700', high_usd: '0.390', cap_usd: '1' } };
    const api = fakeApi([body]);
    await show(api);
    await click('Run check');
    expect(screen.getByText('About $0.2700–$0.390, at most $1')).toBeTruthy();
    await click('Run for up to $0.390');
    expect(api.run).toHaveBeenCalledWith(state('estimate').material_id, { high_usd: '0.390' });
  });

  it('409 estimate_changed shows the new estimate and asks again', async () => {
    const api = fakeApi([state('estimate').body]);
    api.run.mockRejectedValueOnce(httpError('estimate_changed'));
    await show(api);
    await click('Run check');
    await click('Run for up to $0.39');
    await waitFor(() => expect(screen.getByText('About $0.31–$0.45, at most $1.00')).toBeTruthy());
    await click('Run for up to $0.45');
    expect(api.run).toHaveBeenLastCalledWith(state('estimate').material_id, { high_usd: '0.45' });
  });

  it('409 freshness_running switches to progress', async () => {
    const api = fakeApi([state('estimate').body, state('running').body]);
    api.run.mockRejectedValueOnce(httpError('freshness_running'));
    await show(api, state('estimate').material_id, { pollMs: 60_000 });
    await click('Run check');
    await click('Run for up to $0.39');
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Searching the web'));
  });

  it('409 bibliography_unconfirmed and nothing_to_confirm reload and ask to check the details again', async () => {
    for (const code of ['bibliography_unconfirmed', 'nothing_to_confirm']) {
      const s = state('scan_only');
      const api = fakeApi([s.body]);
      api.run.mockRejectedValueOnce(httpError(code));
      await show(api, s.material_id);
      fireEvent.click(screen.getByRole('checkbox', { name: 'These details are right' }));
      await click(/^Run (check|again)$/);
      await click('Run for up to $0.33');
      await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
      expect(screen.getByText(/check the details again/i), code).toBeTruthy();
      expect((screen.getByRole('checkbox', { name: 'These details are right' }) as HTMLInputElement).checked).toBe(false);
      cleanup();
    }
  });

  it("daily_budget_exhausted shows the daily limit with the server's numbers", async () => {
    const api = fakeApi([state('estimate').body]);
    const e = httpError('daily_budget_exhausted');
    e.body = { ...e.body, daily_cap_usd: '3.50', spent_usd: '3.41' };
    api.run.mockRejectedValueOnce(e);
    await show(api);
    await click('Run check');
    await click('Run for up to $0.39');
    await waitFor(() => expect(screen.getByText(/Daily limit for freshness checks reached \(\$3\.50\); try again tomorrow/)).toBeTruthy());
    expect(screen.getByText(/\$3\.41/)).toBeTruthy();
  });
});

describe('status and verdict (FR-FRESHUI-4)', () => {
  it('polls every 3 s by default while running and stops at a final status', async () => {
    const { POLL_MS } = await load(() => import('./FreshnessCard'));
    expect(POLL_MS).toBe(3000);
    const api = fakeApi([state('running').body, state('running').body, state('current').body]);
    await show(api, state('running').material_id, { pollMs: 5 });
    await waitFor(() => expect(screen.getByText('Current')).toBeTruthy());
    const calls = api.get.mock.calls.length;
    expect(calls).toBe(3);
    await new Promise((r) => setTimeout(r, 40));
    expect(api.get.mock.calls.length).toBe(calls);
  });

  it('names each running stage in a status line', async () => {
    const running = state('running').body;
    for (const [progress, text] of [
      [{ stage: 'scan', done: 0, total: 1 }, 'Reading the book'],
      [{ stage: 'search', done: 4, total: 6 }, 'Searching the web (4 of 6)'],
      [{ stage: 'report', done: 0, total: 1 }, 'Writing the report'],
    ] as const) {
      const body = { ...running, report: { ...running.report, progress } };
      await show(fakeApi([body]), state('running').material_id, { pollMs: 60_000 });
      expect(screen.getByRole('status').textContent).toContain(text);
      cleanup();
    }
  });

  it('the verdict badge has text and an icon, never colour alone', async () => {
    for (const [name, text] of [['current', 'Current'], ['partly_outdated', 'Partly outdated'], ['outdated', 'Outdated']] as const) {
      await show(fakeApi([state(name).body]), state(name).material_id);
      const label = screen.getByText(text, { selector: '.fresh-badge *' });
      const badge = label.closest('.fresh-badge')!;
      expect(badge.querySelector('svg[aria-hidden="true"]'), name).not.toBeNull();
      cleanup();
    }
  });

  it('shows the date checked, and Stale with each reason and Run again', async () => {
    await show(fakeApi([state('current').body]), state('current').material_id);
    expect(screen.getByText('Checked 2026-11-23')).toBeTruthy();
    expect(screen.queryByText('Stale')).toBeNull();
    cleanup();
    const stale = state('stale').body;
    for (const [reason, text] of [['ttl', 'older than 90 days'], ['reingested', 're-imported'], ['bibliography_changed', 'details changed']] as const) {
      await show(fakeApi([{ ...stale, stale_reason: reason }]), state('stale').material_id);
      expect(screen.getByText('Stale')).toBeTruthy();
      expect(screen.getByText(text, { exact: false })).toBeTruthy();
      expect(screen.getByRole('button', { name: 'Run again' })).toBeTruthy();
      cleanup();
    }
  });

  it('failed and budget_refused show their message and Run again', async () => {
    await show(fakeApi([state('failed').body]), state('failed').material_id);
    expect(screen.getByText(/The web searches returned no results/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Run again' })).toBeTruthy();
    cleanup();
    await show(fakeApi([state('budget_refused').body]), state('budget_refused').material_id);
    expect(screen.getByText('Daily limit for freshness checks reached ($2.00); try again tomorrow', { exact: false })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Run again' })).toBeTruthy();
    cleanup();
    const failed = state('failed').body;
    await show(fakeApi([{ ...failed, report: { ...failed.report, error: 'some_new_code' } }]), state('failed').material_id);
    expect(screen.getByText(/The check failed/)).toBeTruthy();
  });

  it('a scan-only report says the bibliography needs a check', async () => {
    await show(fakeApi([state('scan_only').body]), state('scan_only').material_id);
    expect(screen.getByText(/bibliography needs a check/i)).toBeTruthy();
    expect(document.querySelector('.fresh-badge')).toBeNull();
  });
});

describe('reasons and claims (FR-FRESHUI-5)', () => {
  it('lists reasons with citation links and claims with status, note and page links', async () => {
    const s = state('partly_outdated');
    await show(fakeApi([s.body]), s.material_id);
    const reasons = screen.getByRole('region', { name: 'Reasons' });
    expect(within(reasons).getByText('The client library chapter describes an API that was replaced in 2022.')).toBeTruthy();
    expect(within(reasons).getByRole('link', { name: /Migrating to v3/ }).getAttribute('href')).toBe('https://docs.lib.example/migration/v3');
    const claims = screen.getByRole('region', { name: 'Flagged claims' });
    for (const text of ['client library v1 API', 'api', 'high', 'changed', 'Version 3 replaced this API.', 'block size limit', 'unknown'])
      expect(within(claims).getAllByText(text, { exact: false }).length, text).toBeGreaterThan(0);
    // The lesson cell citing the page when there is one; else the spans view of that page.
    expect(within(claims).getByRole('link', { name: 'p. 88' }).getAttribute('href')).toBe('/lessons/w01#cell-e');
    expect(within(claims).getByRole('link', { name: 'p. 102' }).getAttribute('href')).toBe(`/api/materials/${s.material_id}/spans?page=102`);
    expect(within(claims).getByRole('link', { name: 'p. 102' }).getAttribute('target')).toBeNull();
  });

  it('a cell reference outside the id pattern falls back to the spans view', async () => {
    const s = state('partly_outdated');
    const report = s.body.report!;
    const claims = (report.claims as Array<Record<string, unknown>>).map((c) => ({ ...c, cell: { lesson_id: '../x', cell_id: 'e"><b>' } }));
    await show(fakeApi([{ ...s.body, report: { ...report, claims } }]), s.material_id);
    expect(screen.getByRole('link', { name: 'p. 88' }).getAttribute('href')).toBe(`/api/materials/${s.material_id}/spans?page=88`);
  });
});

describe('recommendations and links (FR-FRESHUI-6)', () => {
  it('each recommendation is a card with a safe title link, host, date as reported, kind, why and covered sections', async () => {
    const s = state('outdated');
    await show(fakeApi([s.body]), s.material_id);
    const recs = screen.getByRole('region', { name: 'Recommendations' });
    const items = within(recs).getAllByRole('listitem');
    expect(items).toHaveLength(5);
    const first = items[0]!;
    const a = within(first).getByRole('link');
    expect(a.getAttribute('href')).toBe('https://publisher.example/books/pdl-4');
    expect(a.getAttribute('rel')).toBe('noopener noreferrer nofollow');
    expect(a.getAttribute('target')).toBe('_blank');
    expect(a.textContent).toMatch(/^Practical Distributed Ledgers, 4th edition.*opens in a new tab/);
    const host = within(first).getByText('publisher.example');
    expect(host.tagName).toBe('CODE');
    for (const text of ['2024-06 (as reported)', 'The current edition of this book.', 'Covers: Ledgers, Consensus'])
      expect(within(first).getByText(text, { exact: false }), text).toBeTruthy();
    expect(within(first).getByText(/new edition/i)).toBeTruthy();
    // An IDN host is shown in its ASCII form, as the report has it.
    expect(within(items[2]!).getByText('xn--bcher-kva.example')).toBeTruthy();
    // A javascript: URL is shown as text, never as a link.
    const last = items[4]!;
    expect(within(last).queryByRole('link')).toBeNull();
    expect(within(last).getByText('javascript:alert(1)', { exact: false })).toBeTruthy();
    expect(screen.queryByText(/Add as source/i)).toBeNull();
  });

  it('says how many links or recommendations were removed and why', async () => {
    await show(fakeApi([state('outdated').body]), state('outdated').material_id);
    const note = screen.getByText(/removed/i);
    expect(note.textContent).toMatch(/2 not found in this check's search results/);
    expect(note.textContent).toMatch(/1 not a web link/);
    cleanup();
    await show(fakeApi([state('current').body]), state('current').material_id);
    expect(screen.queryByText(/removed/i)).toBeNull();
  });
});
