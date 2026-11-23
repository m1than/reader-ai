// @trace FR-UI-13
// The owner's Compile action and compile status in the lesson list (V/compile-ui). Everything runs against a stub
// fetch; nothing here starts a real compile.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { readYaml } from './testing/fixtures';
import { load } from './testing/load';

afterEach(cleanup);

const owner = { id: '0b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b10', handle: 'ivan', role: 'owner', display_name: 'Ivan', consent: { evaluation: true, tuning: false } };
const learner = { ...owner, handle: 'anna', role: 'learner', display_name: 'Anna' };
const SHA = '3'.repeat(64);

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

const run = (state: string, steps: unknown[], extra: Record<string, unknown> = {}) => ({
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
  ...extra,
});
const done = (step: number, stage: string) => ({ step, stage, model: 'claude-opus-5-5', cost_usd: '0.600000', duration_s: 12.5, reused: false });
const pending = (step: number, stage: string, status: string) => ({ step, stage, status, reused: false });

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
const w02List = () => listOf([lesson('w01', 1, '1'.repeat(64)), lesson('w02', 2)]);

const rowOf = (title: string) => screen.getByText(title).closest('li') as HTMLElement;

describe('who sees the Compile action', () => {
  it('a learner sees FR-UI-6 unchanged and makes no compile or estimate request', async () => {
    const calls = await mount('/', (url) => (url === '/api/me' ? json(200, learner) : url === '/api/lessons' ? json(200, w02List()) : json(404, { error: 'not_found' })));
    await waitFor(() => expect(screen.getByText('Lesson w02')).toBeTruthy());
    expect(within(rowOf('Lesson w02')).getByText('not published yet')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Compile' })).toBeNull();
    expect(calls.filter((c) => /\/compile$|\/estimate$|\/api\/compiles\//.test(c.url))).toEqual([]);
  });

  it('the owner gets a Compile button and "not compiled yet" for an unpublished lesson without a run', async () => {
    await mount('/', (url) => (url === '/api/me' ? json(200, owner) : url === '/api/lessons' ? json(200, w02List()) : json(404, { error: 'not_found' })));
    await waitFor(() => expect(within(rowOf('Lesson w02')).getByText('not compiled yet')).toBeTruthy());
    expect(within(rowOf('Lesson w02')).getByRole('button', { name: 'Compile' })).toBeTruthy();
    expect(within(rowOf('Lesson w01')).queryByRole('button')).toBeNull();
  });

  it('reads the status of at most 10 unpublished lessons, in list order; the rest keep "not published yet" and a button', async () => {
    const many = listOf(Array.from({ length: 12 }, (_, i) => lesson(`u${String(i + 1).padStart(2, '0')}`, i + 1)));
    const calls = await mount('/', (url) => (url === '/api/me' ? json(200, owner) : url === '/api/lessons' ? json(200, many) : json(404, { error: 'not_found' })));
    await waitFor(() => expect(screen.getAllByText('not compiled yet')).toHaveLength(10));
    const reads = calls.filter((c) => c.method === 'GET' && c.url.endsWith('/compile')).map((c) => c.url);
    expect(reads).toEqual(Array.from({ length: 10 }, (_, i) => `/api/lessons/u${String(i + 1).padStart(2, '0')}/compile`));
    expect(within(rowOf('Lesson u11')).getByText('not published yet')).toBeTruthy();
    expect(within(rowOf('Lesson u12')).getByRole('button', { name: 'Compile' })).toBeTruthy();
    expect(screen.getAllByRole('button', { name: 'Compile' })).toHaveLength(12);
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
    // no pending row: the last step's stage
    expect(describeRun(run('running', [done(1, 'draft')])).text).toBe('Compiling: drafting');
  });

  it('maps not compiled, stopped (with and without a code), budget refusals and finished runs', async () => {
    const { describeRun } = await model();
    expect(describeRun(null)).toEqual({ text: 'not compiled yet', action: 'compile' });
    const failed = (error: string | null) => run('stage_failed', [pending(1, 'draft', 'dead')], { error });
    expect(describeRun(failed('provider_missing'))).toEqual({
      text: 'Compile stopped at drafting. No AI provider is connected for your account. Connect one, then retry.',
      action: 'retry',
    });
    expect(describeRun(failed(null))).toEqual({ text: 'Compile stopped at drafting', action: 'retry' });
    expect(describeRun(failed('sdk_error'))).toEqual({ text: 'Compile stopped at drafting', action: 'retry' });
    // a status without the `error` field (a server before FR-PIPELINE-16) maps the same way
    const { error: _omit, ...noField } = failed(null) as Record<string, unknown>;
    expect(describeRun(noField as never)).toEqual({ text: 'Compile stopped at drafting', action: 'retry' });
    const budget = (code: string) => run('budget_exhausted', [done(1, 'draft')], { refusals: [code], spent_usd: '7.95' });
    expect(describeRun(budget('budget_exhausted'))).toEqual({ text: 'Compile stopped: it reached its $8.00 cap. Spent $7.95.', action: 'compile' });
    expect(describeRun(budget('task_budget_exhausted')).text).toBe('Compile stopped: a stage reached its own budget. Spent $7.95.');
    expect(describeRun(budget('monthly_budget_exhausted')).text).toBe('Compile stopped: the monthly budget is used up. Spent $7.95.');
    expect(describeRun(budget('daily_budget_exhausted')).text).toBe('Compile stopped: the daily budget is used up. Spent $7.95.');
    expect(describeRun(budget('something_new')).text).toBe('Compile stopped: a budget was reached. Spent $7.95.');
    for (const state of ['converged', 'open_findings']) expect(describeRun(run(state, [done(1, 'draft')]))).toEqual({ text: null, action: null });
  });

  it('polls only a running or stalled run, every 5 s', async () => {
    const { pollInterval, COMPILE_POLL_MS } = await model();
    expect(COMPILE_POLL_MS).toBe(5000);
    expect(pollInterval('running')).toBe(5000);
    expect(pollInterval('stalled')).toBe(5000);
    for (const s of ['stage_failed', 'budget_exhausted', 'converged', 'open_findings', null]) expect(pollInterval(s)).toBe(false);
  });

  it('pins RUN_CAP_USD to SRV-PIPELINE bounds.run_cap_usd and the status bound to 10', async () => {
    const { RUN_CAP_USD, STATUS_READS_MAX } = await model();
    const spec = readYaml<{ bounds: { run_cap_usd: number } }>('spec/modules/SRV-PIPELINE.yaml');
    expect(RUN_CAP_USD).toBe(spec.bounds.run_cap_usd.toFixed(2));
    expect(STATUS_READS_MAX).toBe(10);
  });
});

describe('the confirm', () => {
  const ownerRoutes = (over: Record<string, Handler> = {}): Handler => (url, init) => {
    const key = `${init?.method ?? 'GET'} ${url}`;
    if (over[key]) return over[key](url, init);
    if (url === '/api/me') return json(200, owner);
    if (url === '/api/lessons') return json(200, w02List());
    if (url === '/api/lessons/w02/estimate') return json(200, estimate());
    return json(404, { error: 'not_found' });
  };

  const openDialog = async () => {
    await waitFor(() => expect(within(rowOf('Lesson w02')).getByRole('button', { name: 'Compile' })).toBeTruthy());
    await act(async () => {
      fireEvent.click(within(rowOf('Lesson w02')).getByRole('button', { name: 'Compile' }));
    });
    return screen.findByRole('dialog');
  };

  it('shows the lesson, the estimate labelled as one, the extrapolation and the cap, then posts without a body', async () => {
    let posted = false;
    const calls = await mount(
      '/',
      ownerRoutes({
        'POST /api/lessons/w02/compile': () => {
          posted = true;
          return json(202, { run_id: 'run-1' });
        },
        'GET /api/lessons/w02/compile': () => (posted ? json(200, run('running', [pending(1, 'draft', 'running')])) : json(404, { error: 'not_found' })),
      }),
    );
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
    const posts = calls.filter((c) => c.method === 'POST');
    expect(posts).toEqual([{ method: 'POST', url: '/api/lessons/w02/compile', body: undefined }]);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    const status = await within(rowOf('Lesson w02')).findByText('Compiling: drafting');
    expect(document.activeElement).toBe(status);
    expect(status.getAttribute('tabindex')).toBe('-1');
  });

  it('says when the lesson is longer than the measured one, and when there is no estimate', async () => {
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
  });

  it('sends one request for a double click and cancels without one', async () => {
    let release: (r: Response) => void = () => undefined;
    const calls = await mount(
      '/',
      ownerRoutes({ 'POST /api/lessons/w02/compile': () => new Promise<Response>((r) => (release = r)) }),
    );
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
    });
    await act(async () => {
      fireEvent.click(confirm);
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
      let reads = 0;
      await mount(
        '/',
        ownerRoutes({
          'POST /api/lessons/w02/compile': () => json(409, { error: code }),
          'GET /api/lessons/w02/compile': () => (++reads > 1 ? json(200, run('running', [pending(1, 'draft', 'running')])) : json(404, { error: 'not_found' })),
        }),
      );
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

describe('retry, polling and the link', () => {
  it('resumes a stopped run through its own confirm', async () => {
    let resumed = false;
    const calls = await mount('/', (url, init) => {
      if (url === '/api/me') return json(200, owner);
      if (url === '/api/lessons') return json(200, w02List());
      if (init?.method === 'POST' && url === '/api/compiles/run-1/resume') {
        resumed = true;
        return json(202, run('running', [pending(1, 'draft', 'queued')]));
      }
      if (url === '/api/lessons/w02/compile')
        return json(200, resumed ? run('running', [pending(1, 'draft', 'queued')]) : run('stage_failed', [pending(1, 'draft', 'dead')], { error: 'provider_missing' }));
      return json(404, { error: 'not_found' });
    });
    const row = () => rowOf('Lesson w02');
    await waitFor(() =>
      expect(within(row()).getByText('Compile stopped at drafting. No AI provider is connected for your account. Connect one, then retry.')).toBeTruthy(),
    );
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
    expect(await within(row()).findByText('Compiling: drafting')).toBeTruthy();
  });

  it('closes on not_failed with a notice', async () => {
    await mount('/', (url, init) => {
      if (url === '/api/me') return json(200, owner);
      if (url === '/api/lessons') return json(200, w02List());
      if (init?.method === 'POST') return json(409, { error: 'not_failed' });
      if (url === '/api/lessons/w02/compile') return json(200, run('stage_failed', [pending(1, 'draft', 'dead')]));
      return json(404, { error: 'not_found' });
    });
    await act(async () => {
      fireEvent.click(await within(rowOf('Lesson w02')).findByRole('button', { name: 'Retry' }));
    });
    const dialog = await screen.findByRole('dialog');
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Resume' }));
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(await screen.findByText('This compile is no longer stopped.')).toBeTruthy();
  });

  it('turns the row into a link when the status reports a revision, and refetches the list', async () => {
    let published = false;
    const calls = await mount('/', (url) => {
      if (url === '/api/me') return json(200, owner);
      if (url === '/api/lessons') return json(200, listOf([lesson('w01', 1, '1'.repeat(64)), lesson('w02', 2, published ? SHA : null)]));
      if (url === '/api/lessons/w02/compile') {
        published = true;
        return json(200, run('converged', [done(1, 'draft')], { latest_sha256: SHA }));
      }
      return json(404, { error: 'not_found' });
    });
    await waitFor(() => expect(screen.getByText('Lesson w02').closest('a')?.getAttribute('href')).toBe('/lessons/w02'));
    expect(calls.filter((c) => c.url === '/api/lessons').length).toBeGreaterThanOrEqual(2);
    expect(within(rowOf('Lesson w02')).queryByRole('button')).toBeNull();
  });

  it('keeps "Compiling" after the link while a run with a revision still runs', async () => {
    let reads = 0;
    await mount('/', (url) => {
      if (url === '/api/me') return json(200, owner);
      if (url === '/api/lessons') return json(200, listOf([lesson('w02', 2, reads > 0 ? SHA : null)]));
      if (url === '/api/lessons/w02/compile') {
        reads += 1;
        return json(200, run('running', [done(1, 'draft'), pending(2, 'fact_check', 'running')], { latest_sha256: SHA }));
      }
      return json(404, { error: 'not_found' });
    });
    await waitFor(() => expect(screen.getByText('Lesson w02').closest('a')).toBeTruthy());
    expect(within(rowOf('Lesson w02')).getByText('Compiling: checking facts')).toBeTruthy();
  });
});
