// @trace FR-IMPORT-4 FR-IMPORT-5 FR-IMPORT-6 FR-IMPORT-8
// The planning part of /import: the study-time form, the live estimate, confirm and "Your sources". Synthetic bodies
// only (titles "Synthetic chapter N").
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { load } from '../../app/testing/load';
import type { PlanApi, Preview } from './api';

afterEach(cleanup);

const preview = (over: Partial<Preview['summary']> = {}, est: Partial<Preview['estimate']> = {}): Preview => ({
  plan_sha256: 'a'.repeat(64),
  inputs: { material_id: 'synthetic', total_weeks: 2, minutes_per_day: 60, rate: '2.64', start_date: '2026-01-01', today: '2026-01-01' },
  lessons: [
    { id: 'p1-2', pages: [1, 2], minutes: 12, fixed: false, title: 'Synthetic chapter 1' },
    { id: 'p3-3', pages: [3, 3], minutes: 6, fixed: false, title: 'Synthetic chapter 2' },
  ],
  summary: {
    lesson_count: 14,
    fixed_count: 0,
    new_count: 14,
    study_days: 14,
    start_date: '2026-01-01',
    end: '2026-01-14',
    days_left: 14,
    spare_days: 0,
    minutes_mean: 9,
    minutes_max: 12,
    required_minutes: 126,
    finish_by: '2026-01-14',
    fits: true,
    reasons: [],
    suggest: null,
    ...over,
  },
  estimate: {
    course_low_usd: '1.20',
    course_high_usd: '2.80',
    first_lesson: { low_usd: '0.09', high_usd: '0.20' },
    run_cap_usd: '8.00',
    basis: 'L02',
    basis_kchars: '58.938',
    mean_lesson_kchars: '4.000',
    extrapolated: true,
    ...est,
  },
});

const fakeApi = (over: Partial<PlanApi> = {}): PlanApi & { calls: unknown[] } => {
  const calls: unknown[] = [];
  return {
    calls,
    materials: vi.fn(async () => ({ materials: [] })),
    stored: vi.fn(async () => null),
    preview: vi.fn(async (body) => {
      calls.push(body);
      return preview();
    }),
    confirm: vi.fn(async () => ({
      course_id: 'c',
      plan_course_id: 'plan-x',
      lessons: [{ plan_lesson_id: 'p1-2', lesson_id: 'l1', ordinal: 1 }],
      created: 1,
      removed: 0,
      replayed: false,
      compile: null,
    })),
    ...over,
  };
};

const section = async (api: PlanApi, stored: Parameters<typeof import('./PlanSection').PlanSection>[0]['stored'] = null) => {
  const { PlanSection } = await load(() => import('./PlanSection'));
  expect(PlanSection).toBeTypeOf('function');
  await act(async () => {
    render(<PlanSection api={api} materialId="synthetic" stored={stored} locale="en-GB" today={() => '2026-01-01'} />);
  });
};

const weeks = () => screen.getByLabelText('Total weeks') as HTMLInputElement;
const minutes = () => screen.getByLabelText('Minutes per day') as HTMLInputElement;

describe('study-time form (FR-IMPORT-4)', () => {
  it('has the two required fields with their bounds, defaults and a collapsed study pace', async () => {
    await section(fakeApi());
    expect(screen.getByRole('heading', { name: 'How long do you want to study this source?' })).toBeTruthy();
    expect(weeks().value).toBe('');
    expect([weeks().min, weeks().max, weeks().required]).toEqual(['1', '104', true]);
    expect([minutes().value, minutes().min, minutes().max, minutes().step]).toEqual(['60', '10', '240', '5']);
    const rate = screen.getByLabelText('Minutes per 1,000 characters') as HTMLInputElement;
    expect(rate.value).toBe('2.64');
    expect((rate.closest('details') as HTMLDetailsElement).open).toBe(false);
    expect(screen.getByText('From your own pilot plan.')).toBeTruthy();
  });

  it('shows a field error on blur, never while typing, linked by aria-describedby', async () => {
    const { check } = await load(() => import('./model'));
    expect(check).toBeTypeOf('function');
    await section(fakeApi());
    fireEvent.change(weeks(), { target: { value: '0' } });
    expect(weeks().getAttribute('aria-invalid')).toBeNull();
    fireEvent.blur(weeks());
    expect(weeks().getAttribute('aria-invalid')).toBe('true');
    const error = document.getElementById(weeks().getAttribute('aria-describedby') ?? '');
    expect(error?.textContent).toBe('Enter whole weeks from 1 to 104.');
    for (const [f, ok] of [
      [{ weeks: '104', minutes: '240', rate: '20.00' }, true],
      [{ weeks: '105', minutes: '60', rate: '2.64' }, false],
      [{ weeks: '2', minutes: '62', rate: '2.64' }, false],
      [{ weeks: '2', minutes: '60', rate: '0.09' }, false],
      [{ weeks: '2', minutes: '60', rate: '1.234' }, false],
      [{ weeks: '2.5', minutes: '60', rate: '2.64' }, false],
    ] as const)
      expect(check(f).value !== null).toBe(ok);
  });
});

describe('estimate preview (FR-IMPORT-5)', () => {
  it('previews after a pause with the local date and shows the estimate lines', async () => {
    const api = fakeApi();
    await section(api);
    fireEvent.change(weeks(), { target: { value: '2' } });
    await waitFor(() => expect(screen.getByText(/14 lessons, one a day/)).toBeTruthy());
    expect(api.calls).toEqual([{ material_id: 'synthetic', total_weeks: 2, minutes_per_day: 60, rate: '2.64', today: '2026-01-01' }]);
    // Design pass: the headline is the status text; the figures are a definition list; the notes follow it.
    expect(screen.getByRole('status').textContent).toBe('Estimate updated: 14 lessons, one a day, about 9 min each (at most 12), finish by 14 January 2026');
    const dl = document.querySelector('.plan-estimate dl') as HTMLElement;
    const pairs = [...dl.querySelectorAll('dt')].map((dt) => [dt.textContent, dt.nextElementSibling?.textContent]);
    expect(pairs).toEqual([
      ['Lessons', '14'],
      ['Each lesson', 'about 9 min, at most 12'],
      ['Finish by', '14 January 2026'],
      ['Compile, whole course', '$1.20–$2.80'],
      ['Lesson 1', '$0.09–$0.20, stops at $8.00'],
    ]);
    expect(screen.getByText('Reading time only; labs and review are not included.')).toBeTruthy();
    expect(screen.getByText('Costs are an estimate from one measured lesson.')).toBeTruthy();
    expect(
      screen.getByText('Lessons are 14.7× shorter than the measured one; real cost per lesson is likely higher than this range.'),
    ).toBeTruthy();
    expect(screen.getByText('Synthetic chapter 1')).toBeTruthy();
  });

  it('says when a small source fills fewer days, and shows a re-plan as started + new', async () => {
    const { estimateLines } = await load(() => import('./model'));
    const small = estimateLines(preview({ new_count: 3, spare_days: 11, days_left: 14, lesson_count: 3 }), 'en');
    expect(small.fills).toBe('This source fills 3 of 14 days.');
    const replan = estimateLines(preview({ fixed_count: 80, new_count: 5, lesson_count: 85, end: '2026-03-25' }), 'en-GB');
    expect(replan.headline.startsWith('80 started + 5 new lessons, one a day')).toBe(true);
    expect(replan.deadline).toBe('Deadline kept: 25 March 2026');
  });

  it('when it does not fit, shows the reason and suggestion buttons that set the field and preview again', async () => {
    const api = fakeApi({
      preview: vi.fn(async (body: { minutes_per_day: number }) =>
        body.minutes_per_day === 60
          ? preview({ fits: false, reasons: ['minutes'], minutes_max: 70, suggest: { minutes_per_day: 70, total_weeks: 3 } })
          : preview(),
      ),
    });
    await section(api);
    fireEvent.change(weeks(), { target: { value: '2' } });
    await waitFor(() => expect(screen.getByText("A day's lesson would take 70 min, more than your 60 min.")).toBeTruthy());
    expect((screen.getByRole('button', { name: 'Create the course' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole('button', { name: 'Use 3 weeks' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Use 70 min a day' }));
    expect(minutes().value).toBe('70');
    await waitFor(() => expect((screen.getByRole('button', { name: 'Create the course' }) as HTMLButtonElement).disabled).toBe(false));
  });

  it('drops an out-of-order answer and shows only the latest', async () => {
    let release: (p: Preview) => void = () => undefined;
    const api = fakeApi({
      preview: vi.fn((body: { total_weeks: number }) =>
        body.total_weeks === 2 ? new Promise<Preview>((r) => (release = r)) : Promise.resolve(preview({ new_count: 21, lesson_count: 21 })),
      ),
    });
    await section(api);
    fireEvent.change(weeks(), { target: { value: '2' } });
    await waitFor(() => expect(api.preview).toHaveBeenCalledTimes(1));
    fireEvent.change(weeks(), { target: { value: '3' } });
    await waitFor(() => expect(screen.getByText(/^21 lessons/)).toBeTruthy());
    await act(async () => release(preview()));
    expect(screen.queryByText(/^14 lessons/)).toBeNull();
  });

  it('shows no_text instead of the card', async () => {
    const { ApiError } = await import('../../app/api');
    await section(fakeApi({ preview: vi.fn(async () => Promise.reject(new ApiError(422, 'no_text'))) }));
    fireEvent.change(weeks(), { target: { value: '2' } });
    await waitFor(() => expect(screen.getByText('This source has no text layer to plan from.')).toBeTruthy());
  });
});

describe('confirm (FR-IMPORT-6)', () => {
  it('Enter inside the debounce does not confirm the previous plan; the confirm posts the previewed inputs', async () => {
    const api = fakeApi();
    await section(api);
    fireEvent.change(weeks(), { target: { value: '2' } });
    await waitFor(() => expect((screen.getByRole('button', { name: 'Create the course' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.change(weeks(), { target: { value: '3' } }); // a new preview is pending
    expect((screen.getByRole('button', { name: 'Create the course' }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => fireEvent.submit(weeks().closest('form') as HTMLFormElement));
    expect(api.confirm).not.toHaveBeenCalled();
    await waitFor(() => expect(api.preview).toHaveBeenCalledTimes(2));
    await waitFor(() => expect((screen.getByRole('button', { name: 'Create the course' }) as HTMLButtonElement).disabled).toBe(false));
    await act(async () => fireEvent.submit(weeks().closest('form') as HTMLFormElement));
    expect(api.confirm).toHaveBeenCalledWith(
      expect.objectContaining({ total_weeks: 3, minutes_per_day: 60, rate: '2.64', today: '2026-01-01' }),
    );
  });

  it('a failed preview blocks confirm, also by Enter', async () => {
    const { ApiError } = await import('../../app/api');
    let fail = false;
    const api = fakeApi({
      preview: vi.fn(async () => (fail ? Promise.reject(new ApiError(0, 'network')) : preview())),
    });
    await section(api);
    fireEvent.change(weeks(), { target: { value: '2' } });
    await waitFor(() => expect((screen.getByRole('button', { name: 'Create the course' }) as HTMLButtonElement).disabled).toBe(false));
    fail = true;
    fireEvent.change(weeks(), { target: { value: '4' } });
    await waitFor(() => expect(screen.getByText(/could not be reached/)).toBeTruthy());
    fireEvent.change(weeks(), { target: { value: '2' } }); // back to the shown inputs, but the new preview fails too
    await waitFor(() => expect(api.preview).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(screen.getByText(/could not be reached/)).toBeTruthy());
    expect((screen.getByRole('button', { name: 'Create the course' }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => fireEvent.submit(weeks().closest('form') as HTMLFormElement));
    expect(api.confirm).not.toHaveBeenCalled();
  });

  it('hides compile-first when the stored plan has a started lesson', async () => {
    const api = fakeApi();
    await section(api, {
      course_id: 'c',
      plan_course_id: 'plan-x',
      inputs: { material_id: 'synthetic', total_weeks: 2, minutes_per_day: 60, rate: '2.64', start_date: '2026-01-01' },
      plan_sha256: 'a'.repeat(64),
      started: 1,
    });
    await waitFor(() => expect((screen.getByRole('button', { name: 'Create the course' }) as HTMLButtonElement).disabled).toBe(false));
    expect(screen.queryByRole('checkbox')).toBeNull();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Create the course' })));
    expect(api.confirm).toHaveBeenCalledWith(expect.objectContaining({ compile_first: false }));
  });

  it('posts the shown sha with compile_first checked by default and reports the compile', async () => {
    const api = fakeApi({
      confirm: vi.fn(async () => ({
        course_id: 'c',
        plan_course_id: 'plan-x',
        lessons: [{ plan_lesson_id: 'p1-2', lesson_id: 'l1', ordinal: 1 }],
        created: 1,
        removed: 0,
        replayed: false,
        compile: { run_id: 'r1' },
      })),
    });
    await section(api);
    fireEvent.change(weeks(), { target: { value: '2' } });
    const box = await waitFor(() => screen.getByLabelText('Start compiling lesson 1 now ($0.09–$0.20, stops at $8.00)') as HTMLInputElement);
    expect(box.checked).toBe(true);
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Create the course' })));
    expect(api.confirm).toHaveBeenCalledWith(expect.objectContaining({ plan_sha256: 'a'.repeat(64), compile_first: true, total_weeks: 2 }));
    expect(screen.getByText('Course created: 1 lesson.')).toBeTruthy();
    expect(screen.getByText('Lesson 1 is compiling.')).toBeTruthy();
  });

  it('unchecked sends compile_first false; plan_changed asks again and previews again', async () => {
    const { ApiError } = await import('../../app/api');
    const api = fakeApi({ confirm: vi.fn(async () => Promise.reject(new ApiError(409, 'plan_changed'))) });
    await section(api);
    fireEvent.change(weeks(), { target: { value: '2' } });
    await waitFor(() => expect((screen.getByRole('button', { name: 'Create the course' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('checkbox'));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Create the course' })));
    expect(api.confirm).toHaveBeenCalledWith(expect.objectContaining({ compile_first: false }));
    expect(screen.getByText('The plan changed; check it and confirm again.')).toBeTruthy();
    await waitFor(() => expect(api.preview).toHaveBeenCalledTimes(2));
  });

  it('shows already_started as a message while the course is still created', async () => {
    const api = fakeApi({
      confirm: vi.fn(async () => ({
        course_id: 'c',
        plan_course_id: 'plan-x',
        lessons: [],
        created: 0,
        removed: 0,
        replayed: true,
        compile: { error: 'already_started' },
      })),
    });
    await section(api);
    fireEvent.change(weeks(), { target: { value: '2' } });
    await waitFor(() => expect((screen.getByRole('button', { name: 'Create the course' }) as HTMLButtonElement).disabled).toBe(false));
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Create the course' })));
    expect(screen.getByText('A lesson of this course is already compiled or compiling.')).toBeTruthy();
  });
});

describe('your sources (FR-IMPORT-8)', () => {
  const mountPage = async (api: PlanApi) => {
    const { ImportPage } = await load(() => import('./ImportPage'));
    const { ServicesContext } = await import('../../app/context');
    expect(ImportPage).toBeTypeOf('function');
    const services = { api: {} as never, notify: vi.fn(), navigateAway: vi.fn(), plan: api };
    await act(async () => {
      render(
        <ServicesContext.Provider value={services}>
          <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
            <ImportPage />
          </QueryClientProvider>
        </ServicesContext.Provider>,
      );
    });
  };

  it('lists sources, offers Plan study time for an imported one, and Re-plan pre-filled when a plan exists', async () => {
    const api = fakeApi({
      materials: vi.fn(async () => ({
        materials: [
          { id: 'synthetic', status: 'imported', page_count: 30 },
          { id: 'queued-one', status: 'queued', page_count: null },
          { id: 'planned', status: 'imported', page_count: 12 },
        ],
      })),
      stored: vi.fn(async (id: string) =>
        id === 'planned'
          ? {
              course_id: 'c',
              plan_course_id: 'plan-y',
              inputs: { material_id: 'planned', total_weeks: 6, minutes_per_day: 45, rate: '2.64', start_date: '2026-01-01' },
              plan_sha256: 'b'.repeat(64),
              started: 3,
            }
          : null,
      ),
    });
    await mountPage(api);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Re-plan' })).toBeTruthy());
    expect(screen.getAllByRole('button', { name: 'Plan study time' })).toHaveLength(1);
    // Design pass: a not-imported status is a chip in sentence case.
    expect(screen.getByText('Queued')).toBeTruthy();
    expect(screen.queryByText(/not imported|error/i)).toBeNull(); // a 404 is "no plan yet", not an error
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Re-plan' })));
    await waitFor(() => expect((screen.getByLabelText('Total weeks') as HTMLInputElement).value).toBe('6'));
    expect((screen.getByLabelText('Minutes per day') as HTMLInputElement).value).toBe('45');
    expect(screen.getByText('3 started lessons keep their pages.')).toBeTruthy();
  });

  it('shows not-imported statuses as sentence-case chips and marks the chosen row current (design pass)', async () => {
    const api = fakeApi({
      materials: vi.fn(async () => ({
        materials: [
          { id: 'synthetic', status: 'imported', page_count: 30 },
          { id: 'broken', status: 'import_failed', page_count: null },
        ],
      })),
    });
    await mountPage(api);
    const chip = await screen.findByText('Import failed');
    expect(chip.closest('.acme-chip, [data-chip], .chip')).toBeTruthy();
    const rows = () => [...document.querySelectorAll('.source-row')] as HTMLElement[];
    expect(rows().map((r) => r.getAttribute('aria-current'))).toEqual([null, null]);
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Plan study time' })));
    expect(rows().map((r) => r.getAttribute('aria-current'))).toEqual(['true', null]);
  });
});

describe('design pass (FR-IMPORT-4, FR-IMPORT-5, FR-IMPORT-6)', () => {
  const button = () => screen.getByRole('button', { name: 'Create the course' }) as HTMLButtonElement;
  const reason = () => {
    const id = button().getAttribute('aria-describedby');
    return id ? document.getElementById(id)?.textContent ?? null : null;
  };
  const pairs = () =>
    [...document.querySelectorAll('.plan-estimate dl dt')].map((dt) => [dt.textContent, dt.nextElementSibling?.textContent]);

  it('marks the study pace disclosure with a hidden chevron icon', async () => {
    await section(fakeApi());
    const summary = screen.getByText('Study pace').closest('summary') as HTMLElement;
    expect(summary.querySelector('svg[aria-hidden="true"]')).toBeTruthy();
  });

  it('lists a re-plan as started + new with the kept deadline, and omits Lesson 1 without a first lesson', async () => {
    const api = fakeApi({
      preview: vi.fn(async () => preview({ fixed_count: 80, new_count: 5, lesson_count: 85, end: '2026-03-25' }, { first_lesson: null })),
    });
    await section(api);
    fireEvent.change(weeks(), { target: { value: '2' } });
    await waitFor(() => expect(pairs().length).toBeGreaterThan(0));
    expect(pairs()).toEqual([
      ['Lessons', '80 started + 5 new'],
      ['Each lesson', 'about 9 min, at most 12'],
      ['Finish by', '14 January 2026'],
      ['Deadline kept', '25 March 2026'],
      ['Compile, whole course', '$1.20–$2.80'],
    ]);
  });

  it('orders the notes: reading time, fills, the cost basis, the extrapolation', async () => {
    const api = fakeApi({ preview: vi.fn(async () => preview({ new_count: 3, spare_days: 11, days_left: 14, lesson_count: 3 })) });
    await section(api);
    fireEvent.change(weeks(), { target: { value: '2' } });
    await waitFor(() => expect(pairs().length).toBeGreaterThan(0));
    const notes = [...document.querySelectorAll('.plan-notes p')].map((p) => p.textContent);
    expect(notes).toEqual([
      'Reading time only; labs and review are not included.',
      'This source fills 3 of 14 days.',
      'Costs are an estimate from one measured lesson.',
      'Lessons are 14.7× shorter than the measured one; real cost per lesson is likely higher than this range.',
    ]);
  });

  it('puts the does-not-fit callout before the figures', async () => {
    const api = fakeApi({ preview: vi.fn(async () => preview({ fits: false, reasons: ['minutes'], minutes_max: 70, suggest: { minutes_per_day: 70, total_weeks: null } })) });
    await section(api);
    fireEvent.change(weeks(), { target: { value: '2' } });
    const notice = await screen.findByText("A day's lesson would take 70 min, more than your 60 min.");
    const callout = notice.closest('.callout') as HTMLElement;
    expect(callout).toBeTruthy();
    const dl = document.querySelector('.plan-estimate dl') as HTMLElement;
    expect(callout.compareDocumentPosition(dl) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(callout.querySelector('svg[aria-hidden="true"]')).toBeTruthy();
  });

  it('says why Create the course is disabled, and nothing once it is enabled', async () => {
    let fail = false;
    let fits = true;
    const api = fakeApi({
      preview: vi.fn(async () => {
        if (fail) throw new Error('boom');
        return fits ? preview() : preview({ fits: false, reasons: ['days'], suggest: null });
      }),
    });
    await section(api);
    expect(button().disabled).toBe(true);
    expect(reason()).toBe('Enter the total weeks first.');
    fireEvent.change(minutes(), { target: { value: '7' } });
    fireEvent.change(weeks(), { target: { value: '2' } });
    expect(reason()).toBe('Fix the fields above first.');
    fireEvent.change(minutes(), { target: { value: '60' } });
    expect(reason()).toBe('Updating the estimate…');
    await waitFor(() => expect(button().disabled).toBe(false));
    expect(button().getAttribute('aria-describedby')).toBeNull();
    fail = true;
    fireEvent.change(weeks(), { target: { value: '3' } });
    await waitFor(() => expect(reason()).toBe('The estimate failed; change a field to try again.'));
    fail = false;
    fits = false;
    fireEvent.change(weeks(), { target: { value: '4' } });
    await waitFor(() => expect(reason()).toBe('The plan does not fit; use a suggestion or change the fields.'));
  });

  it('says Creating the course… while a confirm is in flight', async () => {
    const api = fakeApi({ confirm: vi.fn(() => new Promise<never>(() => undefined)) });
    await section(api);
    fireEvent.change(weeks(), { target: { value: '2' } });
    await waitFor(() => expect(button().disabled).toBe(false));
    await act(async () => fireEvent.click(button()));
    expect(reason()).toBe('Creating the course…');
  });
});
