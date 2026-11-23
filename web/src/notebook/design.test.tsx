// @trace FR-NOTEBOOK-1 FR-NOTEBOOK-3 FR-NOTEBOOK-7 FR-NOTEBOOK-9 FR-NOTEBOOK-10 FR-NOTEBOOK-11 FR-NOTEBOOK-12 FR-UI-10 VEC-NOTEBOOK-1
// Design pass (2026-11-23, V/design-pass): the not-published page, the open-question callout, the More menu, the lab
// block and its editor seam, the recall deck, breadcrumbs, the current section, the cell toolbar, keyboard shortcuts,
// state blocks and the lesson's language. Synthetic fixture W01 and invented cells only.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { readJson } from '../app/testing/fixtures';
import { load } from '../app/testing/load';

afterEach(cleanup);

type Cell = Record<string, unknown>;
type Nb = { cells: Cell[]; metadata: Record<string, unknown> };
const notebook = () => readJson<Nb>('spec/fixtures/WEB-NOTEBOOK/lesson-w01.ipynb');
const me = { id: '0b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b10', handle: 'ivan', role: 'owner', display_name: 'Ivan', consent: { evaluation: true, tuning: false } };
const json = (status: number, b: unknown) => new Response(JSON.stringify(b), { status, headers: { 'Content-Type': 'application/json' } });
const dyn = <T,>(path: string) => load<Partial<T>>(() => import(/* @vite-ignore */ path) as Promise<T>);

const controller = (cells: Record<string, unknown> = {}, extra: Record<string, unknown> = {}) => {
  const state = { cells, slot: null, session: null, connection: 'none', resetting: false, resetFailed: false, ...extra };
  return {
    run: vi.fn(), runAll: vi.fn(), interrupt: vi.fn(), reset: vi.fn(), retry: vi.fn(), dispose: vi.fn(),
    getState: () => state,
    subscribe: () => () => undefined,
  };
};
type Panel = { open: boolean; close: () => void };
type Aside = (cellId: string | null, label: string | null, panel: Panel) => ReactNode;
const pending = () => new Promise<never>(() => undefined);

const view = async (nb: unknown = notebook(), opts: { cells?: Record<string, unknown>; aside?: Aside; loadEditor?: () => Promise<unknown> } = {}) => {
  const { LessonView } = await load(() => import('./LessonView'));
  expect(LessonView).toBeTypeOf('function');
  const ctl = controller(opts.cells);
  const props = { notebook: nb as never, controller: ctl as never, aside: opts.aside as never, loadEditor: opts.loadEditor ?? pending };
  render(<LessonView {...(props as Parameters<typeof LessonView>[0])} />);
  return ctl;
};
const cellEl = (id: string) => document.querySelector(`[data-cell-id="${id}"]`) as HTMLElement;
const stubClipboard = (fail = false) => {
  const writeText = vi.fn(async () => {
    if (fail) throw new Error('denied');
  });
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
  return writeText;
};

// ---- Lessons for the page-level tests (AppRoot) ----
interface L {
  lesson_id: string;
  course_id: string;
  course_title: string;
  ordinal: number;
  title: string;
  updated_at: string;
  latest_sha256: string | null;
}
const row = (n: number, published: boolean): L => ({
  lesson_id: `p-${n}`, course_id: 'c1', course_title: 'Sample course', ordinal: n, title: `Sample lesson ${n}`,
  updated_at: '2026-11-20T09:00:00Z', latest_sha256: published ? 'a'.repeat(64) : null,
});
const mountPage = async (path: string, list: L[], route: (url: string) => Response | Promise<Response>, compileAction?: (l: L) => ReactNode) => {
  const { AppRoot } = await load(() => import('../app/AppRoot'));
  expect(AppRoot).toBeTypeOf('function');
  const fetchFn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === '/api/me') return json(200, me);
    if (url === '/api/lessons') return json(200, { lessons: list });
    return route(url);
  });
  const props = { initialPath: path, fetch: fetchFn as unknown as typeof fetch, navigateAway: vi.fn(), compileAction };
  await act(async () => {
    render(<AppRoot {...(props as Parameters<typeof AppRoot>[0])} />);
  });
  await waitFor(() => expect(document.getElementById('content')).toBeTruthy());
  return fetchFn;
};
const page = () => document.getElementById('content') as HTMLElement;
const meta = (l: L) => json(200, { lesson: l, revisions: [] });

describe('not-published page (FR-NOTEBOOK-1, design pass)', () => {
  const list = [row(1, true), row(2, false), row(3, false), row(4, true), row(5, false)];
  const at = (n: number) => (url: string) => (url === `/api/lessons/p-${n}` ? meta(list[n - 1]!) : json(404, {}));

  it('shows the title, the sentence, the chip and both published neighbours', async () => {
    await mountPage('/lessons/p-3', list, at(3));
    const h1 = await within(page()).findByRole('heading', { level: 1, name: 'Sample lesson 3' });
    expect(h1.closest('.state-block')).toBeTruthy();
    expect(within(page()).getByText('Lesson 3 of Sample course is not published yet.')).toBeTruthy();
    expect(within(page()).getByText('Not published yet')).toBeTruthy();
    expect(within(page()).getByRole('link', { name: 'Previous: Sample lesson 1' }).getAttribute('href')).toBe('/lessons/p-1');
    expect(within(page()).getByRole('link', { name: 'Next: Sample lesson 4' }).getAttribute('href')).toBe('/lessons/p-4');
    expect(within(page()).getByRole('link', { name: 'All lessons of Sample course' }).getAttribute('href')).toBe('/#course-c1');
  });

  it('omits a missing neighbour', async () => {
    await mountPage('/lessons/p-5', list, at(5));
    await within(page()).findByRole('heading', { level: 1, name: 'Sample lesson 5' });
    expect(within(page()).getByRole('link', { name: 'Previous: Sample lesson 4' })).toBeTruthy();
    expect(within(page()).queryByRole('link', { name: /^Next:/ })).toBeNull();
    cleanup();
    await mountPage('/lessons/p-2', [row(2, false)], (url) => (url === '/api/lessons/p-2' ? meta(row(2, false)) : json(404, {})));
    await within(page()).findByRole('heading', { level: 1, name: 'Sample lesson 2' });
    expect(within(page()).queryByRole('link', { name: /^(Previous|Next):/ })).toBeNull();
  });

  it('renders the compile slot when present and nothing otherwise', async () => {
    await mountPage('/lessons/p-3', list, at(3), (l) => <button type="button">{`Compile ${l.lesson_id}`}</button>);
    await within(page()).findByRole('heading', { level: 1, name: 'Sample lesson 3' });
    expect(within(page()).getByRole('button', { name: 'Compile p-3' })).toBeTruthy();
    cleanup();
    await mountPage('/lessons/p-3', list, at(3));
    await within(page()).findByRole('heading', { level: 1, name: 'Sample lesson 3' });
    expect(within(page()).queryByRole('button', { name: /^Compile/ })).toBeNull();
  });
});

describe('kind labels and the open-question callout (FR-NOTEBOOK-1, FR-NOTEBOOK-3, design pass)', () => {
  it('labels no explanation cell', async () => {
    await view();
    expect(within(cellEl('w01-explain')).queryByText('explanation')).toBeNull();
    expect(cellEl('w01-explain').querySelector('.cell-kind')).toBeNull();
  });

  it('titles the open cell "Open question" with a sentence-case chip', async () => {
    const nb = notebook();
    const open = nb.cells.find((c) => c.id === 'w01-open')!;
    (open.metadata as { reader: Record<string, unknown> }).reader.status = 'needs_review';
    await view(nb);
    const cell = cellEl('w01-open');
    expect(within(cell).getByText('Open question')).toBeTruthy();
    expect(within(cell).getByText('Needs review')).toBeTruthy();
    expect(cell.querySelector('.callout')).toBeTruthy();
  });
});

describe('lesson toolbar (FR-NOTEBOOK-7, design pass)', () => {
  it('has no solid button and keeps Reset kernel in the More menu', async () => {
    await view();
    const toolbar = document.querySelector('.lesson-toolbar') as HTMLElement;
    expect(toolbar.querySelector('[data-variant="solid"]')).toBeNull();
    expect(within(toolbar).queryByRole('button', { name: /reset kernel/i })).toBeNull();
    expect(within(toolbar).getByRole('button', { name: 'More lesson actions' })).toBeTruthy();
    expect(within(toolbar).getByRole('button', { name: /run all/i }).getAttribute('data-variant')).toBe('ghost');
  });
});

describe('lab block (FR-NOTEBOOK-9, design pass)', () => {
  const lab = () => cellEl('w01-lab');

  it('names the block and shows the header actions; Revert is disabled until an edit', async () => {
    await view(notebook(), { cells: { 'w01-lab': { state: 'done', outputs: [], executionCount: 2, marker: null, notes: [] } } });
    expect(within(lab()).getByText('Python · Lab 1')).toBeTruthy();
    expect(within(lab()).getByText('[2]')).toBeTruthy();
    const run = within(lab()).getByRole('button', { name: /^run$/i });
    expect(run.getAttribute('data-variant')).toBe('solid');
    expect(run.getAttribute('title')).toBe('Run (Shift+Enter)');
    expect(within(lab()).getByRole('button', { name: /^copy$/i })).toBeTruthy();
    const revert = within(lab()).getByRole('button', { name: /revert/i }) as HTMLButtonElement;
    expect(revert.disabled).toBe(true);
    expect(within(lab()).queryByRole('button', { name: /interrupt/i })).toBeNull();
    fireEvent.change(within(lab()).getByRole('textbox', { name: 'Lab 1 code' }), { target: { value: 'print(9)' } });
    expect((within(lab()).getByRole('button', { name: /revert/i }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('shows Interrupt while running', async () => {
    await view(notebook(), { cells: { 'w01-lab': { state: 'running', outputs: [], executionCount: null, marker: null, notes: [] } } });
    expect(within(lab()).getByRole('button', { name: /interrupt/i })).toBeTruthy();
  });

  it('copies the current code and says so', async () => {
    const writeText = stubClipboard();
    await view();
    fireEvent.change(within(lab()).getByRole('textbox', { name: 'Lab 1 code' }), { target: { value: 'x = 1' } });
    await act(async () => {
      fireEvent.click(within(lab()).getByRole('button', { name: /^copy$/i }));
    });
    expect(writeText).toHaveBeenCalledWith('x = 1');
    expect(within(lab()).getByRole('button', { name: /^copied$/i })).toBeTruthy();
    expect(within(lab()).getAllByRole('status').some((s) => s.textContent === 'Code copied')).toBe(true);
  });

  it('reports a failed copy', async () => {
    stubClipboard(true);
    await view();
    await act(async () => {
      fireEvent.click(within(lab()).getByRole('button', { name: /^copy$/i }));
    });
    expect(within(lab()).getAllByRole('status').some((s) => s.textContent === 'Copy failed')).toBe(true);
  });

  it('keeps the text area while the editor chunk loads and when it fails', async () => {
    await view(notebook(), { loadEditor: pending });
    expect(within(lab()).getByRole('textbox', { name: 'Lab 1 code' })).toBeTruthy();
    cleanup();
    await view(notebook(), { loadEditor: () => Promise.reject(new Error('chunk failed')) });
    await act(async () => undefined);
    expect(within(lab()).getByRole('textbox', { name: 'Lab 1 code' })).toBeTruthy();
  });

  it('mounts the editor from the chunk and runs its text on its run key', async () => {
    const mounted: { host?: HTMLElement; opts?: { doc: string; label: string; onChange: (t: string) => void; onRun: (t: string) => void } } = {};
    const handle = { setDoc: vi.fn(), focus: vi.fn(), destroy: vi.fn() };
    const mod = {
      mountEditor: vi.fn((host: HTMLElement, opts: NonNullable<typeof mounted.opts>) => {
        mounted.host = host;
        mounted.opts = opts;
        return handle;
      }),
    };
    const ctl = await view(notebook(), { loadEditor: () => Promise.resolve(mod) });
    await waitFor(() => expect(mod.mountEditor).toHaveBeenCalledTimes(1));
    expect(within(lab()).queryByRole('textbox', { name: 'Lab 1 code' })).toBeNull();
    expect(mounted.opts?.doc).toContain('hashlib.sha256');
    expect(mounted.opts?.label).toBe('Lab 1 code');
    expect(lab().contains(mounted.host ?? null)).toBe(true);
    act(() => mounted.opts?.onChange('print(5)'));
    act(() => mounted.opts?.onRun('print(5)'));
    expect(ctl.run).toHaveBeenCalledWith('w01-lab', 'print(5)');
    fireEvent.click(within(lab()).getByRole('button', { name: /revert/i }));
    expect(handle.setDoc).toHaveBeenCalledWith(expect.stringContaining('hashlib.sha256'));
    cleanup();
    expect(handle.destroy).toHaveBeenCalled();
  });
});

describe('recall deck (FR-NOTEBOOK-10, design pass)', () => {
  const recall = (id: string, back?: string) => ({ cell_type: 'markdown', id, source: '', metadata: { reader: { kind: 'recall', front: `Front ${id}?`, ...(back ? { back } : {}) } } });
  const text = (id: string) => ({ cell_type: 'markdown', id, source: `Text ${id}.`, metadata: { reader: { kind: 'explain' } } });
  const decks = () => [...document.querySelectorAll('section.recall-deck')] as HTMLElement[];

  it('groups adjacent recall cells into one deck and keeps each card a cell target', async () => {
    await view({ ...notebook(), cells: [text('a'), recall('r1', 'B1.'), recall('r2', 'B2.'), text('b'), recall('r3', 'B3.')] });
    expect(decks()).toHaveLength(2);
    expect(within(decks()[0]!).getByRole('heading', { name: 'Recall cards (2)' })).toBeTruthy();
    expect(within(decks()[1]!).getByRole('heading', { name: 'Recall cards (1)' })).toBeTruthy();
    for (const id of ['r1', 'r2', 'r3']) {
      const card = document.getElementById(`cell-${id}`) as HTMLElement;
      expect(card.getAttribute('data-cell-id')).toBe(id);
      expect(card.getAttribute('tabindex')).toBe('-1');
    }
  });

  it('opens and closes every answer of a deck with one toggle', async () => {
    await view({ ...notebook(), cells: [recall('r1', 'B1.'), recall('r2', 'B2.')] });
    const deck = decks()[0]!;
    const details = () => [...deck.querySelectorAll('details')] as HTMLDetailsElement[];
    expect(details().every((d) => !d.open)).toBe(true);
    fireEvent.click(within(deck).getByRole('button', { name: 'Show all answers' }));
    expect(details().every((d) => d.open)).toBe(true);
    fireEvent.click(within(deck).getByRole('button', { name: 'Hide all answers' }));
    expect(details().every((d) => !d.open)).toBe(true);
  });

  it('has no toggle when no card has an answer', async () => {
    await view({ ...notebook(), cells: [recall('r1'), recall('r2')] });
    expect(within(decks()[0]!).queryByRole('button', { name: /all answers/ })).toBeNull();
  });
});

describe('navigation (FR-NOTEBOOK-11, design pass)', () => {
  let observers: { cb: IntersectionObserverCallback; targets: Element[] }[] = [];
  beforeEach(() => {
    observers = [];
    class IO {
      targets: Element[] = [];
      constructor(public cb: IntersectionObserverCallback) {
        observers.push(this);
      }
      observe(t: Element) {
        this.targets.push(t);
      }
      unobserve() {}
      disconnect() {}
      takeRecords() {
        return [];
      }
    }
    vi.stubGlobal('IntersectionObserver', IO);
  });
  afterEach(() => vi.unstubAllGlobals());

  const current = () =>
    [...document.querySelectorAll('nav[aria-label="Lesson contents"] a')].filter((a) => a.getAttribute('aria-current') === 'location').map((a) => a.textContent);
  const report = (id: string) =>
    act(() => {
      const target = document.getElementById(id)!;
      for (const o of observers)
        if (o.targets.includes(target))
          o.cb([{ target, isIntersecting: true, intersectionRatio: 1, boundingClientRect: { top: 10 } } as unknown as IntersectionObserverEntry], o as unknown as IntersectionObserver);
    });

  it('marks exactly one contents entry as the current section and moves it', async () => {
    await view();
    expect(current()).toHaveLength(1);
    expect(observers.length).toBeGreaterThan(0);
    report('cell-w01-explain');
    expect(current()).toEqual(['Digests']);
    report('cell-w01-lab');
    expect(current()).toEqual(['Lab 1']);
  });

  it('gives every non-lab cell a toolbar with Copy and Ask', async () => {
    const writeText = stubClipboard();
    const aside = vi.fn<Aside>(() => <p>panel</p>);
    await view(notebook(), { aside });
    expect(within(cellEl('w01-lab')).queryByRole('button', { name: 'Copy cell text' })).toBeNull();
    const explain = cellEl('w01-explain');
    await act(async () => {
      fireEvent.click(within(explain).getByRole('button', { name: 'Copy cell text' }));
    });
    expect(writeText).toHaveBeenCalledWith(expect.stringContaining('## Digests'));
    expect(within(explain).getAllByRole('status').some((s) => s.textContent === 'Cell text copied')).toBe(true);
    fireEvent.click(within(explain).getByRole('button', { name: 'Ask the tutor about this cell' }));
    const last = aside.mock.calls.at(-1)!;
    expect(last[0]).toBe('w01-explain');
    expect(last[2].open).toBe(true);
  });

  it('opens the tutor from the toolbar and the contents entry; close closes it', async () => {
    const aside = vi.fn<Aside>(() => <p>panel</p>);
    await view(notebook(), { aside });
    expect(aside.mock.calls.at(-1)![2].open).toBe(false);
    fireEvent.click(within(document.querySelector('.lesson-toolbar') as HTMLElement).getByRole('button', { name: 'Ask the tutor' }));
    expect(aside.mock.calls.at(-1)![2].open).toBe(true);
    act(() => aside.mock.calls.at(-1)![2].close());
    expect(aside.mock.calls.at(-1)![2].open).toBe(false);
    fireEvent.click(screen.getByRole('link', { name: 'Ask the tutor' }));
    expect(aside.mock.calls.at(-1)![2].open).toBe(true);
  });

  it('moves between cells with j and k, opens the tutor with / and the shortcuts with ?', async () => {
    const aside = vi.fn<Aside>(() => <p>panel</p>);
    await view(notebook(), { aside });
    cellEl('w01-route').focus();
    fireEvent.keyDown(document.activeElement!, { key: 'j' });
    expect(document.activeElement).toBe(cellEl('w01-explain'));
    fireEvent.keyDown(document.activeElement!, { key: 'k' });
    expect(document.activeElement).toBe(cellEl('w01-route'));
    fireEvent.keyDown(document.activeElement!, { key: '/' });
    expect(aside.mock.calls.at(-1)![2].open).toBe(true);
    fireEvent.keyDown(document.body, { key: '?' });
    const dialog = await screen.findByRole('dialog', { name: 'Keyboard shortcuts' });
    for (const key of ['j', 'k', '/', '?', 'Shift+Enter', 'Escape']) expect(dialog.textContent).toContain(key);
  });

  it('ignores shortcuts inside a text field and with a modifier', async () => {
    await view();
    cellEl('w01-route').focus();
    fireEvent.keyDown(document.activeElement!, { key: 'j', ctrlKey: true });
    expect(document.activeElement).toBe(cellEl('w01-route'));
    const area = within(cellEl('w01-lab')).getByRole('textbox', { name: 'Lab 1 code' });
    area.focus();
    fireEvent.keyDown(area, { key: 'j' });
    expect(document.activeElement).toBe(area);
  });

  it('shows breadcrumbs with the course and the lesson as the current page', async () => {
    const lessonRow = { lesson_id: 'w01', course_id: 'c-fixture', course_title: 'Fixture course', ordinal: 1, title: 'Fixture lesson W01: one cell of every kind', updated_at: '2026-11-21T09:00:00Z', latest_sha256: '1'.repeat(64) };
    await mountPage('/lessons/w01', [lessonRow], (url) =>
      url === '/api/lessons/w01' ? json(200, { lesson: lessonRow, revisions: [] }) : url.includes('/revisions/') ? json(200, notebook()) : json(404, {}),
    );
    const crumbs = await within(page()).findByRole('navigation', { name: 'Breadcrumb' });
    const links = within(crumbs).getAllByRole('link');
    expect(links.map((a) => [a.textContent, a.getAttribute('href')])).toEqual([
      ['Lessons', '/'],
      ['Fixture course', '/#course-c-fixture'],
    ]);
    expect(within(crumbs).getByText('Fixture lesson W01: one cell of every kind').getAttribute('aria-current')).toBe('page');
  });
});

describe('page states (FR-NOTEBOOK-12, design pass)', () => {
  it('renders not-found and load errors through the state block with their explanations', async () => {
    await mountPage('/lessons/w01', [], () => json(404, { error: 'not_found' }));
    const nf = await within(page()).findByRole('heading', { level: 1, name: 'Lesson not found' });
    expect(nf.closest('.state-block')).toBeTruthy();
    expect(within(page()).getByText('The lesson may have been removed or the link is wrong.')).toBeTruthy();
    cleanup();
    await mountPage('/lessons/w01', [], () => json(500, { error: 'internal' }));
    const err = await within(page()).findByRole('heading', { level: 1, name: 'The lesson could not be loaded.' });
    expect(err.closest('.state-block')).toBeTruthy();
    expect(within(page()).getByText('Check the connection, then retry.')).toBeTruthy();
  });
});

describe('lesson language (FR-UI-10, design pass)', () => {
  interface LangModule {
    lessonLang: (nb: { cells: { cell_type: string; source: string | string[] }[]; metadata?: Record<string, unknown> }) => string;
  }
  const md = (source: string) => ({ cell_type: 'markdown', source });

  it('takes a well-formed metadata language, else the Cyrillic majority, else en', async () => {
    const { lessonLang } = await dyn<LangModule>('./lang');
    expect(lessonLang).toBeTypeOf('function');
    if (!lessonLang) return;
    expect(lessonLang({ cells: [md('Plain English text.')], metadata: { reader: { language: 'de-AT' } } })).toBe('de-AT');
    expect(lessonLang({ cells: [md('Децентрализация — ключевая идея, а hash и API встречаются.')] })).toBe('ru');
    expect(lessonLang({ cells: [md('Mostly English with одно слово.')] })).toBe('en');
    expect(lessonLang({ cells: [md('Текст по-русски.')], metadata: { reader: { language: 'Russian!' } } })).toBe('ru');
    expect(lessonLang({ cells: [{ cell_type: 'code', source: 'печать = 1' }, md('English prose here.')] })).toBe('en');
  });

  it('puts lang on the lesson article only', async () => {
    const nb = notebook();
    nb.cells = [{ cell_type: 'markdown', id: 'ru', source: 'Децентрализация распределяет управление между узлами.', metadata: { reader: { kind: 'explain' } } }];
    await view(nb);
    const article = document.querySelector('article.lesson-article, .lesson[lang], [lang="ru"]') as HTMLElement;
    expect(article?.getAttribute('lang')).toBe('ru');
    expect(article.contains(cellEl('ru'))).toBe(true);
  });
});
