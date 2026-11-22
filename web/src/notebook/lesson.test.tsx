// @trace FR-NOTEBOOK-1 FR-NOTEBOOK-2 FR-NOTEBOOK-3 FR-NOTEBOOK-7 FR-NOTEBOOK-9 INV-NOTEBOOK-2 VEC-NOTEBOOK-1
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { readJson } from '../app/testing/fixtures';
import { load } from '../app/testing/load';

afterEach(cleanup);

const notebook = () => readJson<Record<string, unknown>>('spec/fixtures/WEB-NOTEBOOK/lesson-w01.ipynb');
const lessons = () => readJson<{ lesson: { lesson: Record<string, unknown> } }>('spec/fixtures/WEB-UI/lessons.json');

const fakeController = (overrides: Record<string, unknown> = {}) => {
  const listeners = new Set<() => void>();
  const state = { cells: {} as Record<string, unknown>, slot: null, session: null, connection: 'none', resetting: false, resetFailed: false, ...overrides };
  return {
    run: vi.fn(),
    runAll: vi.fn(),
    interrupt: vi.fn(),
    reset: vi.fn(),
    retry: vi.fn(),
    dispose: vi.fn(),
    getState: () => state,
    subscribe: (fn: () => void) => {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
};

const view = async (nb = notebook(), overrides: Record<string, unknown> = {}) => {
  const { LessonView } = await load(() => import('./LessonView'));
  expect(LessonView).toBeTypeOf('function');
  const controller = fakeController(overrides);
  render(<LessonView notebook={nb as never} controller={controller as never} />);
  return controller;
};

describe('lesson view (FR-NOTEBOOK-1)', () => {
  it('loads metadata, then the latest revision, and renders every cell with its kind label', async () => {
    const { AppRoot } = await load(() => import('../app/AppRoot'));
    expect(AppRoot).toBeTypeOf('function');
    const sha = String(lessons().lesson.lesson.latest_sha256);
    const urls: string[] = [];
    const json = (b: unknown) => new Response(JSON.stringify(b), { status: 200, headers: { 'Content-Type': 'application/json' } });
    const fetchFn = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      urls.push(url);
      if (url === '/api/me') return json({ id: '0b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b10', handle: 'ivan', role: 'owner', display_name: 'Ivan', consent: { evaluation: true, tuning: false } });
      if (url === '/api/lessons/w01') return json(lessons().lesson);
      if (url === `/api/lessons/w01/revisions/${sha}`) return json(notebook());
      return new Response(null, { status: 404 });
    });
    await act(async () => {
      render(<AppRoot initialPath="/lessons/w01" fetch={fetchFn as unknown as typeof fetch} navigateAway={vi.fn()} />);
    });
    await waitFor(() => expect(screen.getByText('Digests')).toBeTruthy());
    expect(urls).toEqual(expect.arrayContaining(['/api/lessons/w01', `/api/lessons/w01/revisions/${sha}`]));
    for (const label of ['diagnostic', 'reading route', 'explanation', 'what changed', 'erratum', 'trade-off', 'lab', 'recall', 'notes', 'open question'])
      expect(screen.getAllByText(label).length, label).toBeGreaterThan(0);
  });

  it('shows "not published yet" when latest_sha256 is null and requests no revision', async () => {
    const { AppRoot } = await load(() => import('../app/AppRoot'));
    expect(AppRoot).toBeTypeOf('function');
    const urls: string[] = [];
    const json = (b: unknown) => new Response(JSON.stringify(b), { status: 200, headers: { 'Content-Type': 'application/json' } });
    const fetchFn = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      urls.push(url);
      if (url === '/api/me') return json({ id: '0b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b10', handle: 'ivan', role: 'owner', display_name: 'Ivan', consent: { evaluation: true, tuning: false } });
      return json({ lesson: { ...lessons().lesson.lesson, lesson_id: 'w02', latest_sha256: null }, revisions: [] });
    });
    await act(async () => {
      render(<AppRoot initialPath="/lessons/w02" fetch={fetchFn as unknown as typeof fetch} navigateAway={vi.fn()} />);
    });
    await waitFor(() => expect(screen.getByText(/not published yet/i)).toBeTruthy());
    expect(urls.filter((u) => u.includes('/revisions/'))).toEqual([]);
  });

  it('renders an unknown kind without a label', async () => {
    const nb = notebook() as { cells: Array<Record<string, unknown>> };
    nb.cells = [{ cell_type: 'markdown', id: 'u', metadata: { reader: { kind: 'mystery' } }, source: 'plain text' }];
    await view(nb as never);
    expect(screen.getByText('plain text')).toBeTruthy();
    expect(screen.queryByText('mystery')).toBeNull();
  });
});

describe('references and open findings (FR-NOTEBOOK-2, FR-NOTEBOOK-3)', () => {
  it('shows numbered references under the explain cell and links only https', async () => {
    await view();
    const cell = document.querySelector('[data-cell-id="w01-explain"]') as HTMLElement;
    const refs = within(cell).getByRole('list', { name: /references/i });
    expect(within(refs).getAllByRole('listitem')).toHaveLength(5);
    expect(within(refs).getByText('Fixture material, p. 12')).toBeTruthy();
    expect(within(refs).getByRole('link', { name: 'en.wikipedia.org/wiki/Birthday_attack' }).getAttribute('rel')).toBe('noopener noreferrer');
    expect(within(refs).queryByRole('link', { name: /javascript/ })).toBeNull();
    const erratum = document.querySelector('[data-cell-id="w01-erratum"]') as HTMLElement;
    expect(within(erratum).getByText('The bound is 2^(n/2), not 2^n.')).toBeTruthy();
  });

  it('marks the G2 open cell and counts open findings in the header', async () => {
    await view();
    const open = document.querySelector('[data-cell-id="w01-open"]') as HTMLElement;
    expect(within(open).getByText('not yet confirmed')).toBeTruthy();
    expect(within(open).getByText('claim not confirmed by fact-check')).toBeTruthy();
    const link = screen.getByRole('link', { name: /1 open finding/i });
    expect(link.getAttribute('href')).toBe('#cell-w01-open');
  });
});

describe('controls and editing (FR-NOTEBOOK-7, FR-NOTEBOOK-9)', () => {
  it('Run sends the edited code; Revert restores the revision source', async () => {
    const controller = await view();
    const editor = screen.getByRole('textbox', { name: /lab code/i }) as HTMLTextAreaElement;
    fireEvent.change(editor, { target: { value: 'print(2)' } });
    fireEvent.click(screen.getByRole('button', { name: /^run$/i }));
    expect(controller.run).toHaveBeenCalledWith('w01-lab', 'print(2)');
    fireEvent.click(screen.getByRole('button', { name: /revert/i }));
    expect((screen.getByRole('textbox', { name: /lab code/i }) as HTMLTextAreaElement).value).toContain('hashlib.sha256');
  });

  it('Tab inserts four spaces and Shift+Enter runs', async () => {
    const controller = await view();
    const editor = screen.getByRole('textbox', { name: /lab code/i }) as HTMLTextAreaElement;
    fireEvent.change(editor, { target: { value: 'x' } });
    editor.setSelectionRange(0, 0);
    fireEvent.keyDown(editor, { key: 'Tab' });
    expect(editor.value).toBe('    x');
    fireEvent.keyDown(editor, { key: 'Enter', shiftKey: true });
    expect(controller.run).toHaveBeenCalledWith('w01-lab', '    x');
  });

  it('Reset asks for confirmation that names every open lesson, then resets', async () => {
    const controller = await view();
    fireEvent.click(screen.getByRole('button', { name: /reset kernel/i }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog.textContent).toMatch(/every open lesson/i);
    expect(controller.reset).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: /^reset$/i }));
    expect(controller.reset).toHaveBeenCalledTimes(1);
  });

  it('disables Run, Shift+Enter and Run all while a reset runs', async () => {
    const controller = await view(notebook(), { resetting: true });
    expect((screen.getByRole('button', { name: /^run$/i }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: /run all/i }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.keyDown(screen.getByRole('textbox', { name: /lab code/i }), { key: 'Enter', shiftKey: true });
    expect(controller.run).not.toHaveBeenCalled();
  });

  it('offers Retry reset after a failed reset', async () => {
    const controller = await view(notebook(), { resetFailed: true });
    fireEvent.click(screen.getByRole('button', { name: /retry reset/i }));
    expect(controller.reset).toHaveBeenCalledTimes(1);
  });
});

describe('untrusted text stays text (INV-NOTEBOOK-2)', () => {
  it('canary markup in titles, reasons and outputs is literal text', async () => {
    const canary = '<img src=x onerror=alert(1)>';
    const nb = notebook() as { cells: Array<Record<string, unknown>> };
    nb.cells = [
      {
        cell_type: 'markdown', id: 'e', source: 'x',
        metadata: { reader: { kind: 'explain', claims: [{ text: 't', citations: [{ span: 's:1', material_title: canary }] }] } },
      },
      { cell_type: 'markdown', id: 'o', source: 'y', metadata: { reader: { kind: 'open', status: 'open', finding: { gate: 'G2', reason: canary } } } },
      {
        cell_type: 'code', id: 'l', source: 'z', execution_count: 1,
        metadata: { reader: { kind: 'lab' } },
        outputs: [
          { output_type: 'stream', name: 'stdout', text: canary },
          { output_type: 'error', ename: canary, evalue: canary, traceback: [canary] },
        ],
      },
    ];
    await view(nb as never);
    expect(document.querySelector('img')).toBeNull();
    expect(screen.getAllByText((t) => t.includes(canary)).length).toBeGreaterThanOrEqual(3);
  });
});
