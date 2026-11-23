// @trace FR-UI-15
// The owner's Compile action and compile state in the lesson list (V/compile-ui). Run state, not `latest_sha256`,
// says whether a compile finished: the draft step already writes a revision. Everything runs against a stub fetch;
// nothing here starts a real compile.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { readYaml } from './testing/fixtures';
import { load } from './testing/load';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const owner = { id: '0b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b10', handle: 'ivan', role: 'owner', display_name: 'Ivan', consent: { evaluation: true, tuning: false } };
const learner = { ...owner, handle: 'anna', role: 'learner', display_name: 'Anna' };
const SHA = '3'.repeat(64);
const CHIP = 'draft, compile not finished';

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

const lesson = (id: string, ordinal: number, sha: string | null = null, course = 'Planned course') => ({
  lesson_id: id,
  course_id: `c-${course}`,
  course_title: course,
  ordinal,
  title: `Lesson ${id}`,
  updated_at: '2026-11-23T09:00:00Z',
  latest_sha256: sha,
});

type Step = { step: number; stage: string; status?: string; [k: string]: unknown };
type Run = {
  run_id: string;
  lesson_id: string;
  state: string;
  cap_usd: string;
  steps: Step[];
  spent_usd: string;
  refusals: string[];
  latest_sha256: string | null;
  error?: string | null;
  [k: string]: unknown;
};
const run = (state: string, steps: Step[], extra: Record<string, unknown> = {}): Run => ({
  run_id: 'run-1',
  lesson_id: 'w02',
  state,
  cap_usd: '8.00',
  steps,
  spent_usd: '0.60',
  task_budgets: { compile_draft: '4.00' },
  refusals: [],
  g2: null,
  latest_sha256: null,
  error: null,
  ...extra,
});
const done = (step: number, stage: string) => ({ step, stage, model: 'claude-opus-5-5', cost_usd: '0.600000', duration_s: 12.5, reused: false });
const pending = (step: number, stage: string, status: string) => ({ step, stage, status, reused: false });
const runs = (...rs: unknown[]) => ({ runs: rs });

const estimate = (kchars = '22.104', extrapolated = true) => ({
  lesson_id: 'w02',
  kchars,
  low_usd: '0.47',
  high_usd: '1.11',
  run_cap_usd: '8.00',
  basis: 'L02',
  basis_kchars: '58.938',
  extrapolated,
});

type Handler = (url: string, init?: RequestInit) => Response | Promise<Response>;

const mount = async (path: string, handler: Handler) => {
  const { AppRoot } = await load(() => import('./AppRoot'));
  expect(AppRoot).toBeTypeOf('function');
  const calls: { method: string; url: string; body: unknown }[] = [];
  const fetchFn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ method: init?.method ?? 'GET', url, body: init?.body });
    return handler(url, init);
  });
  await act(async () => {
    render(<AppRoot initialPath={path} fetch={fetchFn as unknown as typeof fetch} navigateAway={vi.fn()} />);
  });
  return calls;
};

const listOf = (lessons: unknown[]) => ({ lessons });
const w02List = (sha: string | null = null) => listOf([lesson('w01', 1, '1'.repeat(64)), lesson('w02', 2, sha)]);

/** The owner's list with a fixed open-runs answer. */
const ownerList = (list: unknown, open: unknown = runs()): Handler => (url) =>
  url === '/api/me' ? json(200, owner) : url === '/api/lessons' ? json(200, list) : url === '/api/compiles' ? json(200, open) : json(404, { error: 'not_found' });

const rowOf = (title: string) => screen.getByText(title).closest('li') as HTMLElement;

describe('who sees the Compile action, and where the run state comes from', () => {
  it('a learner sees FR-UI-6 unchanged and makes no compile or estimate request', async () => {
    const calls = await mount('/', (url) => (url === '/api/me' ? json(200, learner) : url === '/api/lessons' ? json(200, w02List()) : json(404, { error: 'not_found' })));
    await waitFor(() => expect(screen.getByText('Lesson w02')).toBeTruthy());
    expect(within(rowOf('Lesson w02')).getByText('not published yet')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Compile' })).toBeNull();
    expect(screen.queryByText(CHIP)).toBeNull();
    expect(calls.filter((c) => /\/compile$|\/compiles|\/estimate$/.test(c.url))).toEqual([]);
  });

  it('the owner list reads GET /api/compiles once and no per-lesson status', async () => {
    const many = listOf(Array.from({ length: 30 }, (_, i) => lesson(`u${String(i + 1).padStart(2, '0')}`, i + 1)));
    const calls = await mount('/', ownerList(many));
    await waitFor(() => expect(screen.getAllByText('not compiled yet')).toHaveLength(30));
    expect(calls.filter((c) => c.url === '/api/compiles')).toHaveLength(1);
    expect(calls.filter((c) => /\/api\/lessons\/[^/]+\/compile$/.test(c.url))).toEqual([]);
    expect(screen.getAllByRole('button', { name: 'Compile' })).toHaveLength(30);
  });

  it('no open run: "not compiled yet" and Compile when unpublished, nothing extra when linked', async () => {
    await mount('/', ownerList(w02List()));
    await waitFor(() => expect(within(rowOf('Lesson w02')).getByText('not compiled yet')).toBeTruthy());
    expect(within(rowOf('Lesson w02')).getByRole('button', { name: 'Compile' })).toBeTruthy();
    expect(within(rowOf('Lesson w01')).queryByRole('button')).toBeNull();
    expect(within(rowOf('Lesson w01')).queryByText(CHIP)).toBeNull();
  });

  it('a failed run-state read leaves "not published yet" and the Compile button', async () => {
    await mount('/', (url) => (url === '/api/me' ? json(200, owner) : url === '/api/lessons' ? json(200, w02List()) : json(500, { error: 'internal' })));
    await waitFor(() => expect(within(rowOf('Lesson w02')).getByRole('button', { name: 'Compile' })).toBeTruthy());
    expect(within(rowOf('Lesson w02')).getByText('not published yet')).toBeTruthy();
  });
});

describe('linked rows with an open run (a revision is not a finished compile)', () => {
  const linked = (r: unknown) => mount('/', ownerList(w02List(SHA), runs(r)));

  it('running with a revision: the link, the chip and "Compiling"', async () => {
    await linked(run('running', [done(1, 'draft'), pending(2, 'fact_check', 'running')], { latest_sha256: SHA }));
    await waitFor(() => expect(within(rowOf('Lesson w02')).getByText('Compiling: checking facts')).toBeTruthy());
    const row = rowOf('Lesson w02');
    expect(within(row).getByRole('link', { name: 'Lesson w02' }).getAttribute('href')).toBe('/lessons/w02');
    expect(within(row).getByText(CHIP)).toBeTruthy();
    expect(within(row).queryByRole('button')).toBeNull();
  });

  it('stage_failed with a revision: the chip, the reason and Retry', async () => {
    await linked(run('stage_failed', [done(1, 'draft'), pending(2, 'fact_check', 'dead')], { latest_sha256: SHA, error: 'provider_missing' }));
    await waitFor(() => expect(within(rowOf('Lesson w02')).getByRole('button', { name: 'Retry' })).toBeTruthy());
    const row = rowOf('Lesson w02');
    expect(within(row).getByText(CHIP)).toBeTruthy();
    expect(within(row).getByText('Compile stopped at checking facts. No AI provider is connected for your account. Connect one, then retry.')).toBeTruthy();
    expect(within(row).getByRole('link', { name: 'Lesson w02' })).toBeTruthy();
  });

  it('budget_exhausted with a revision: the chip, the refusal and Compile', async () => {
    await linked(run('budget_exhausted', [done(1, 'draft'), done(2, 'fact_check')], { latest_sha256: SHA, refusals: ['budget_exhausted'], spent_usd: '7.95' }));
    await waitFor(() => expect(within(rowOf('Lesson w02')).getByRole('button', { name: 'Compile' })).toBeTruthy());
    const row = rowOf('Lesson w02');
    expect(within(row).getByText(CHIP)).toBeTruthy();
    expect(within(row).getByText('Compile stopped: it reached its $8.00 cap. Spent $7.95.')).toBeTruthy();
  });

  it('an unpublished row with an open run has no chip', async () => {
    await mount('/', ownerList(w02List(), runs(run('running', [pending(1, 'draft', 'running')]))));
    await waitFor(() => expect(within(rowOf('Lesson w02')).getByText('Compiling: drafting')).toBeTruthy());
    expect(within(rowOf('Lesson w02')).queryByText(CHIP)).toBeNull();
  });
});

describe('status mapping', () => {
  const model = () => load(() => import('./compile/model'));

  it('labels every stage and takes the stage of the last step with a status', async () => {
    const { describeRun } = await model();
    expect(describeRun).toBeTypeOf('function');
    const labels: Record<string, string> = {
      draft: 'drafting',
      fact_check: 'checking facts',
      adversarial_review: 'reviewing',
      revise: 'revising',
      findings: 'collecting findings',
      lab_verification: 'verifying labs',
    };
    for (const [stage, label] of Object.entries(labels)) {
      expect(describeRun(run('running', [done(1, 'draft'), pending(2, stage, 'running')]))).toEqual({ text: `Compiling: ${label}`, action: null });
    }
    expect(describeRun(run('stalled', [done(1, 'draft'), pending(2, 'fact_check', 'none')])).text).toBe('Compiling: checking facts');
    expect(describeRun(run('running', [done(1, 'draft')])).text).toBe('Compiling: drafting');
  });

  it('maps no run, stopped (with and without a code), budget refusals and finished runs, with or without a revision', async () => {
    const { describeRun } = await model();
    expect(describeRun(null)).toEqual({ text: 'not compiled yet', action: 'compile' });
    for (const latest_sha256 of [null, SHA]) {
      const failed = (error: string | null) => run('stage_failed', [pending(1, 'draft', 'dead')], { error, latest_sha256 });
      expect(describeRun(failed('provider_missing'))).toEqual({
        text: 'Compile stopped at drafting. No AI provider is connected for your account. Connect one, then retry.',
        action: 'retry',
      });
      expect(describeRun(failed(null))).toEqual({ text: 'Compile stopped at drafting', action: 'retry' });
      expect(describeRun(failed('sdk_error'))).toEqual({ text: 'Compile stopped at drafting', action: 'retry' });
      const { error: _omit, ...noField } = failed(null) as Record<string, unknown>;
      expect(describeRun(noField as never)).toEqual({ text: 'Compile stopped at drafting', action: 'retry' });
      const budget = (code: string) => run('budget_exhausted', [done(1, 'draft')], { refusals: [code], spent_usd: '7.95', latest_sha256 });
      expect(describeRun(budget('budget_exhausted'))).toEqual({ text: 'Compile stopped: it reached its $8.00 cap. Spent $7.95.', action: 'compile' });
      expect(describeRun(budget('task_budget_exhausted')).text).toBe('Compile stopped: a stage reached its own budget. Spent $7.95.');
      expect(describeRun(budget('monthly_budget_exhausted')).text).toBe('Compile stopped: the monthly budget is used up. Spent $7.95.');
      expect(describeRun(budget('daily_budget_exhausted')).text).toBe('Compile stopped: the daily budget is used up. Spent $7.95.');
      expect(describeRun(budget('something_new')).text).toBe('Compile stopped: a budget was reached. Spent $7.95.');
    }
    for (const state of ['converged', 'open_findings']) expect(describeRun(run(state, [done(1, 'draft')], { latest_sha256: SHA }))).toEqual({ text: null, action: null });
  });

  it('isOpen: the chip states', async () => {
    const { isOpen } = await model();
    expect(isOpen).toBeTypeOf('function');
    for (const s of ['running', 'stalled', 'stage_failed', 'budget_exhausted']) expect(isOpen(s)).toBe(true);
    for (const s of ['converged', 'open_findings']) expect(isOpen(s)).toBe(false);
  });

  it('polls a running or stalled run every 5 s for at most 30 minutes', async () => {
    const { pollInterval, COMPILE_POLL_MS, COMPILE_POLL_MAX_MS } = await model();
    expect(COMPILE_POLL_MS).toBe(5000);
    expect(COMPILE_POLL_MAX_MS).toBe(30 * 60 * 1000);
    expect(pollInterval('running', 0, 0)).toBe(5000);
    expect(pollInterval('stalled', 0, COMPILE_POLL_MAX_MS - 1)).toBe(5000);
    expect(pollInterval('running', 0, COMPILE_POLL_MAX_MS)).toBe(false);
    for (const s of ['stage_failed', 'budget_exhausted', 'converged', 'open_findings', null]) expect(pollInterval(s, 0, 0)).toBe(false);
  });

  it('submitAction: nothing while closed; resume without a run closes and never compiles', async () => {
    const { submitAction } = await model();
    expect(submitAction).toBeTypeOf('function');
    const stopped = run('stage_failed', [pending(1, 'draft', 'dead')]) as never;
    expect(submitAction(false, 'compile', null)).toBe('skip');
    expect(submitAction(false, 'resume', stopped)).toBe('skip');
    expect(submitAction(true, 'resume', null)).toBe('close');
    expect(submitAction(true, 'resume', undefined)).toBe('close');
    expect(submitAction(true, 'resume', stopped)).toBe('resume');
    expect(submitAction(true, 'compile', null)).toBe('compile');
    expect(submitAction(true, 'compile', stopped)).toBe('compile');
  });

  it('a run started after 31 idle minutes is polled (the window restarts when a run starts)', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    const t0 = new Date('2026-11-24T10:00:00Z').getTime();
    vi.setSystemTime(t0);
    let posted = false;
    const calls = await mount('/', (url, init) => {
      if (url === '/api/me') return json(200, owner);
      if (url === '/api/lessons') return json(200, w02List());
      if (init?.method === 'POST' && url === '/api/lessons/w02/compile') {
        posted = true;
        return json(202, { run_id: 'run-late' });
      }
      if (url === '/api/compiles') return json(200, posted ? runs(run('running', [pending(1, 'draft', 'running')], { run_id: 'run-late' })) : runs());
      if (url === '/api/lessons/w02/estimate') return json(200, estimate());
      return json(404, { error: 'not_found' });
    });
    await waitFor(() => expect(within(rowOf('Lesson w02')).getByText('not compiled yet')).toBeTruthy());
    vi.setSystemTime(t0 + 31 * 60 * 1000); // idle for 31 minutes: the first window has long expired
    await act(async () => {
      fireEvent.click(within(rowOf('Lesson w02')).getByRole('button', { name: 'Compile' }));
    });
    const dialog = await screen.findByRole('dialog');
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Compile' }));
    });
    await within(rowOf('Lesson w02')).findByText('Compiling: drafting');
    const after = calls.filter((c) => c.url === '/api/compiles').length;
    await waitFor(() => expect(calls.filter((c) => c.url === '/api/compiles').length).toBeGreaterThan(after), { timeout: 8000 });
    expect(within(rowOf('Lesson w02')).queryByRole('button', { name: 'Refresh' })).toBeNull();
  }, 15_000);

  it('pins RUN_CAP_USD to SRV-PIPELINE bounds.run_cap_usd', async () => {
    const { RUN_CAP_USD } = await model();
    const spec = readYaml<{ bounds: { run_cap_usd: number } }>('spec/modules/SRV-PIPELINE.yaml');
    expect(RUN_CAP_USD).toBe(spec.bounds.run_cap_usd.toFixed(2));
  });

  it('stops polling after 30 minutes and offers Refresh, which reads once more', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    const t0 = new Date('2026-11-23T10:00:00Z').getTime();
    vi.setSystemTime(t0);
    const calls = await mount('/', ownerList(w02List(), runs(run('running', [pending(1, 'draft', 'running')]))));
    await waitFor(() => expect(within(rowOf('Lesson w02')).getByText('Compiling: drafting')).toBeTruthy());
    vi.setSystemTime(t0 + 31 * 60 * 1000);
    const refresh = await within(rowOf('Lesson w02')).findByRole('button', { name: 'Refresh' }, { timeout: 8000 });
    const before = calls.filter((c) => c.url === '/api/compiles').length;
    await act(async () => {
      fireEvent.click(refresh);
    });
    await waitFor(() => expect(calls.filter((c) => c.url === '/api/compiles').length).toBe(before + 1));
    await waitFor(() => expect(within(rowOf('Lesson w02')).queryByRole('button', { name: 'Refresh' })).toBeNull());
  }, 15_000);
});

describe('the confirm', () => {
  /** The owner's list; w02 gets an open run after a POST, from GET /api/compiles. */
  const ownerRoutes = (over: Record<string, Handler> = {}): Handler => {
    let posted = false;
    return (url, init) => {
      const key = `${init?.method ?? 'GET'} ${url}`;
      if (over[key]) {
        if (init?.method === 'POST') posted = true;
        return over[key](url, init);
      }
      if (url === '/api/me') return json(200, owner);
      if (url === '/api/lessons') return json(200, w02List());
      if (url === '/api/compiles') return json(200, posted ? runs(run('running', [pending(1, 'draft', 'running')])) : runs());
      if (url === '/api/lessons/w02/estimate') return json(200, estimate());
      return json(404, { error: 'not_found' });
    };
  };

  const openDialog = async () => {
    await waitFor(() => expect(within(rowOf('Lesson w02')).getByRole('button', { name: 'Compile' })).toBeTruthy());
    await act(async () => {
      fireEvent.click(within(rowOf('Lesson w02')).getByRole('button', { name: 'Compile' }));
    });
    return screen.findByRole('dialog');
  };

  it('shows the lesson, the estimate labelled as one, the extrapolation and the cap, then posts without a body', async () => {
    const calls = await mount('/', ownerRoutes({ 'POST /api/lessons/w02/compile': () => json(202, { run_id: 'run-1' }) }));
    const dialog = await openDialog();
    expect(within(dialog).getByRole('heading', { name: 'Compile lesson 2?' })).toBeTruthy();
    expect(within(dialog).getByText('Lesson w02')).toBeTruthy();
    await waitFor(() => expect(within(dialog).getByText('Estimate: $0.47–$1.11')).toBeTruthy());
    expect(within(dialog).getByText("An estimate from one measured lesson (L02), scaled by this lesson's 22.1 thousand characters; not a bound.")).toBeTruthy();
    expect(within(dialog).getByText('This lesson is 2.7× shorter than the measured one; the real cost is likely higher than this range.')).toBeTruthy();
    expect(within(dialog).getByText('Stops at $8.00.')).toBeTruthy();
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Compile' }));
    });
    expect(calls.filter((c) => c.method === 'POST')).toEqual([{ method: 'POST', url: '/api/lessons/w02/compile', body: undefined }]);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    const status = await within(rowOf('Lesson w02')).findByText('Compiling: drafting');
    await waitFor(() => expect(document.activeElement).toBe(status));
    expect(status.getAttribute('tabindex')).toBe('-1');
    expect(calls.filter((c) => c.url === '/api/compiles').length).toBeGreaterThanOrEqual(2);
  });

  it('says when the lesson is longer, when there is no estimate, and refuses a lesson without text', async () => {
    await mount('/', ownerRoutes({ 'GET /api/lessons/w02/estimate': () => json(200, estimate('150.000', true)) }));
    let dialog = await openDialog();
    await waitFor(() => expect(within(dialog).getByText('This lesson is 2.5× longer than the measured one; the real cost may differ from this range.')).toBeTruthy());
    cleanup();
    await mount('/', ownerRoutes({ 'GET /api/lessons/w02/estimate': () => json(200, estimate('58.000', false)) }));
    dialog = await openDialog();
    await waitFor(() => expect(within(dialog).getByText('Estimate: $0.47–$1.11')).toBeTruthy());
    expect(within(dialog).queryByText(/shorter|longer/)).toBeNull();
    cleanup();
    await mount('/', ownerRoutes({ 'GET /api/lessons/w02/estimate': () => json(404, { error: 'not_found' }) }));
    dialog = await openDialog();
    await waitFor(() => expect(within(dialog).getByText('No estimate for this lesson.')).toBeTruthy());
    expect(within(dialog).getByText('Stops at $8.00.')).toBeTruthy();
    expect((within(dialog).getByRole('button', { name: 'Compile' }) as HTMLButtonElement).disabled).toBe(false);
    cleanup();
    const calls = await mount('/', ownerRoutes({ 'GET /api/lessons/w02/estimate': () => json(200, { ...estimate('0.000', true), low_usd: '0.00', high_usd: '0.00' }) }));
    dialog = await openDialog();
    await waitFor(() => expect(within(dialog).getByText("This lesson's pages have no text to compile.")).toBeTruthy());
    const confirm = within(dialog).getByRole('button', { name: 'Compile' }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    await act(async () => {
      fireEvent.click(confirm);
    });
    expect(calls.filter((c) => c.method === 'POST')).toEqual([]);
  });

  it('sends one request for two clicks in one act, and cancels without one', async () => {
    let release: (r: Response) => void = () => undefined;
    const calls = await mount('/', ownerRoutes({ 'POST /api/lessons/w02/compile': () => new Promise<Response>((r) => (release = r)) }));
    let dialog = await openDialog();
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(calls.filter((c) => c.method === 'POST')).toEqual([]);
    dialog = await openDialog();
    const confirm = within(dialog).getByRole('button', { name: 'Compile' });
    await act(async () => {
      fireEvent.click(confirm);
      fireEvent.click(confirm); // before the disabled state renders: the in-flight ref guards it
    });
    expect(calls.filter((c) => c.method === 'POST')).toHaveLength(1);
    await act(async () => {
      release(json(202, { run_id: 'run-1' }));
    });
  });

  it('closes on compile_running and already_started with a notice and shows the run', async () => {
    for (const [code, text] of [
      ['compile_running', 'A compile of this lesson is already running.'],
      ['already_started', 'A lesson of this course is already compiled or compiling.'],
    ] as const) {
      await mount('/', ownerRoutes({ 'POST /api/lessons/w02/compile': () => json(409, { error: code }) }));
      const dialog = await openDialog();
      await act(async () => {
        fireEvent.click(within(dialog).getByRole('button', { name: 'Compile' }));
      });
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(await screen.findByText(text)).toBeTruthy();
      expect(await within(rowOf('Lesson w02')).findByText('Compiling: drafting')).toBeTruthy();
      cleanup();
    }
  });

  it('keeps the dialog open with an alert for every other error', async () => {
    const cases: [number, string, string][] = [
      [404, 'lesson_unknown', 'This lesson no longer exists. Reload the list.'],
      [404, 'not_found', 'This lesson no longer exists. Reload the list.'],
      [409, 'no_material', 'The source of this lesson is not imported.'],
      [409, 'no_source_spans', "This lesson's pages have no text to compile."],
      [409, 'source_too_large', 'This lesson is too long to compile in one run.'],
      [500, 'internal', 'Something went wrong. Try again.'],
    ];
    for (const [status, code, text] of cases) {
      await mount('/', ownerRoutes({ 'POST /api/lessons/w02/compile': () => json(status, { error: code }) }));
      const dialog = await openDialog();
      await act(async () => {
        fireEvent.click(within(dialog).getByRole('button', { name: 'Compile' }));
      });
      const alert = await within(dialog).findByRole('alert');
      expect(alert.textContent, code).toBe(text);
      expect(screen.getByRole('dialog')).toBeTruthy();
      cleanup();
    }
    await mount('/', ownerRoutes({ 'POST /api/lessons/w02/compile': () => Promise.reject(new TypeError('offline')) }));
    const dialog = await openDialog();
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Compile' }));
    });
    expect((await within(dialog).findByRole('alert')).textContent).toBe('The server could not be reached. Check the connection and try again.');
  });

  it('refetches the list after lesson_unknown', async () => {
    const calls = await mount('/', ownerRoutes({ 'POST /api/lessons/w02/compile': () => json(404, { error: 'lesson_unknown' }) }));
    const dialog = await openDialog();
    const before = calls.filter((c) => c.url === '/api/lessons').length;
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Compile' }));
    });
    await waitFor(() => expect(calls.filter((c) => c.url === '/api/lessons').length).toBe(before + 1));
  });
});

describe('retry and the link', () => {
  it('resumes a stopped run through its own confirm', async () => {
    let resumed = false;
    const calls = await mount('/', (url, init) => {
      if (url === '/api/me') return json(200, owner);
      if (url === '/api/lessons') return json(200, w02List(SHA));
      if (init?.method === 'POST' && url === '/api/compiles/run-1/resume') {
        resumed = true;
        return json(202, run('running', [done(1, 'draft'), pending(2, 'fact_check', 'queued')], { latest_sha256: SHA }));
      }
      if (url === '/api/compiles')
        return json(200, runs(resumed
          ? run('running', [done(1, 'draft'), pending(2, 'fact_check', 'queued')], { latest_sha256: SHA })
          : run('stage_failed', [done(1, 'draft'), pending(2, 'fact_check', 'dead')], { latest_sha256: SHA, error: 'provider_missing' })));
      return json(404, { error: 'not_found' });
    });
    const row = () => rowOf('Lesson w02');
    await waitFor(() => expect(within(row()).getByRole('button', { name: 'Retry' })).toBeTruthy());
    await act(async () => {
      fireEvent.click(within(row()).getByRole('button', { name: 'Retry' }));
    });
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('heading', { name: 'Resume the compile of lesson 2?' })).toBeTruthy();
    expect(within(dialog).getByText('Spent so far: $0.60 of $8.00.')).toBeTruthy();
    expect(within(dialog).getByText('Stops at $8.00.')).toBeTruthy();
    expect(calls.some((c) => c.url.endsWith('/estimate'))).toBe(false);
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Resume' }));
    });
    expect(calls.filter((c) => c.method === 'POST').map((c) => c.url)).toEqual(['/api/compiles/run-1/resume']);
    expect(await within(row()).findByText('Compiling: checking facts')).toBeTruthy();
    expect(within(row()).getByText(CHIP)).toBeTruthy();
  });

  it('closes on not_failed with a notice', async () => {
    await mount('/', (url, init) => {
      if (url === '/api/me') return json(200, owner);
      if (url === '/api/lessons') return json(200, w02List());
      if (init?.method === 'POST') return json(409, { error: 'not_failed' });
      if (url === '/api/compiles') return json(200, runs(run('stage_failed', [pending(1, 'draft', 'dead')])));
      return json(404, { error: 'not_found' });
    });
    await waitFor(() => expect(within(rowOf('Lesson w02')).getByRole('button', { name: 'Retry' })).toBeTruthy());
    await act(async () => {
      fireEvent.click(within(rowOf('Lesson w02')).getByRole('button', { name: 'Retry' }));
    });
    const dialog = await screen.findByRole('dialog');
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Resume' }));
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(await screen.findByText('This compile is no longer stopped.')).toBeTruthy();
  });

  it('refetches the list when a run reports a revision the row lacks: the row becomes a link with the chip', async () => {
    let reported = false;
    const calls = await mount('/', (url) => {
      if (url === '/api/me') return json(200, owner);
      if (url === '/api/lessons') return json(200, w02List(reported ? SHA : null));
      if (url === '/api/compiles') {
        reported = true;
        return json(200, runs(run('running', [done(1, 'draft'), pending(2, 'fact_check', 'running')], { latest_sha256: SHA })));
      }
      return json(404, { error: 'not_found' });
    });
    await waitFor(() => expect(screen.getByText('Lesson w02').closest('a')?.getAttribute('href')).toBe('/lessons/w02'));
    expect(calls.filter((c) => c.url === '/api/lessons').length).toBeGreaterThanOrEqual(2);
    expect(within(rowOf('Lesson w02')).getByText(CHIP)).toBeTruthy();
    expect(within(rowOf('Lesson w02')).getByText('Compiling: checking facts')).toBeTruthy();
  });

  it('polls a running run and, when it leaves the open runs (converged), refetches the list and drops the chip', async () => {
    let reads = 0;
    const calls = await mount('/', (url) => {
      if (url === '/api/me') return json(200, owner);
      if (url === '/api/lessons') return json(200, w02List(SHA));
      if (url === '/api/compiles') {
        reads += 1;
        return json(200, reads === 1 ? runs(run('running', [done(1, 'draft'), pending(2, 'fact_check', 'running')], { latest_sha256: SHA })) : runs());
      }
      return json(404, { error: 'not_found' });
    });
    await waitFor(() => expect(within(rowOf('Lesson w02')).getByText(CHIP)).toBeTruthy());
    const before = calls.filter((c) => c.url === '/api/lessons').length;
    await waitFor(() => expect(within(rowOf('Lesson w02')).queryByText(CHIP)).toBeNull(), { timeout: 8000 });
    expect(calls.filter((c) => c.url === '/api/compiles')).toHaveLength(2); // one poll at 5 s, then none
    expect(calls.filter((c) => c.url === '/api/lessons').length).toBe(before + 1);
    expect(within(rowOf('Lesson w02')).getByRole('link', { name: 'Lesson w02' })).toBeTruthy();
  }, 15_000);
});
