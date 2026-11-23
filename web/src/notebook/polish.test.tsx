// @trace FR-NOTEBOOK-3 FR-NOTEBOOK-9 FR-NOTEBOOK-10 FR-NOTEBOOK-11 FR-NOTEBOOK-12 VEC-NOTEBOOK-1
// The lesson view's presentation, amended from the live L02 screenshots (2026-11-23, V/WEB-polish).
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { readJson } from '../app/testing/fixtures';
import { load } from '../app/testing/load';

afterEach(cleanup);

type Cell = Record<string, unknown>;
const notebook = () => readJson<{ cells: Cell[]; metadata: Record<string, unknown> }>('spec/fixtures/WEB-NOTEBOOK/lesson-w01.ipynb');
const lessons = () => readJson<{ lesson: { lesson: Record<string, unknown> } }>('spec/fixtures/WEB-UI/lessons.json');
const me = { id: '0b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b10', handle: 'ivan', role: 'owner', display_name: 'Ivan', consent: { evaluation: true, tuning: false } };

const controller = (cells: Record<string, unknown> = {}) => {
  const state = { cells, slot: null, session: null, connection: 'none', resetting: false, resetFailed: false };
  return {
    run: vi.fn(), runAll: vi.fn(), interrupt: vi.fn(), reset: vi.fn(), retry: vi.fn(), dispose: vi.fn(),
    getState: () => state,
    subscribe: () => () => undefined,
  };
};

const view = async (nb: unknown = notebook(), opts: { cells?: Record<string, unknown>; aside?: unknown } = {}) => {
  const { LessonView } = await load(() => import('./LessonView'));
  expect(LessonView).toBeTypeOf('function');
  render(<LessonView notebook={nb as never} controller={controller(opts.cells) as never} aside={opts.aside as never} />);
};

const cellEl = (id: string) => document.querySelector(`[data-cell-id="${id}"]`) as HTMLElement;

describe('open marker (FR-NOTEBOOK-3, amended)', () => {
  it('reads the status once', async () => {
    await view();
    expect(within(cellEl('w01-open')).getAllByText('open', { exact: true })).toHaveLength(1);
  });
});

describe('lab presentation (FR-NOTEBOOK-9, amended)', () => {
  it('does not wrap code and announces the run state', async () => {
    await view(notebook(), { cells: { 'w01-lab': { state: 'done', outputs: [], executionCount: 3, marker: null, notes: [] } } });
    const lab = cellEl('w01-lab');
    expect(within(lab).getByRole('textbox', { name: /lab code/i }).getAttribute('wrap')).toBe('off');
    expect(within(lab).getByRole('status').textContent).toContain('done');
    expect(within(lab).getByText('[3]')).toBeTruthy();
  });
});

describe('metadata-only kinds (FR-NOTEBOOK-10)', () => {
  const nb = () => ({
    ...notebook(),
    cells: [
      { cell_type: 'markdown', id: 'd', source: 'Answer these first.', metadata: { reader: { kind: 'diagnostic', questions: [{ id: 'q1', text: 'First question?' }, 'Second question?', 7] } } },
      { cell_type: 'markdown', id: 'r', source: '', metadata: { reader: { kind: 'recall', front: 'Front side?', back: 'Back side.' } } },
      { cell_type: 'markdown', id: 'n', source: '', metadata: { reader: { kind: 'notes' } } },
    ],
  });

  it('lists diagnostic questions given as strings or objects, ignoring other values', async () => {
    await view(nb());
    const list = within(cellEl('d')).getByRole('list');
    expect(within(list).getAllByRole('listitem').map((li) => li.textContent)).toEqual(['First question?', 'Second question?']);
  });

  it('does not repeat questions the source already contains (W01)', async () => {
    await view();
    expect(within(cellEl('w01-diagnostic')).queryByRole('list')).toBeNull();
  });

  it('shows a recall card front and its back behind a closed "Show answer" disclosure', async () => {
    await view(nb());
    const recall = cellEl('r');
    expect(within(recall).getByText('Front side?')).toBeTruthy();
    const details = recall.querySelector('details') as HTMLDetailsElement;
    expect(details).toBeTruthy();
    expect(details.open).toBe(false);
    expect(within(details).getByText('Show answer').tagName).toBe('SUMMARY');
    expect(within(details).getByText('Back side.')).toBeTruthy();
  });

  it('marks an empty notes cell as not saved', async () => {
    await view(nb());
    expect(within(cellEl('n')).getByText('Notes are not saved in this version.')).toBeTruthy();
  });
});

describe('lesson contents (FR-NOTEBOOK-11)', () => {
  it('lists headings, labs, recall cards and open questions as in-page links, and the tutor when shown', async () => {
    await view(notebook(), { aside: () => <section id="tutor">panel</section> });
    const nav = screen.getByRole('navigation', { name: 'Lesson contents' });
    const links = within(nav).getAllByRole('link');
    expect(links.map((a) => [a.textContent, a.getAttribute('href')])).toEqual([
      ['Before you start', '#cell-w01-diagnostic'],
      ['Digests', '#cell-w01-explain'],
      ['Lab', '#cell-w01-lab'],
      ['Recall cards (1)', '#cell-w01-recall'],
      ['Open questions (1)', '#cell-w01-open'],
      ['Ask the tutor', '#tutor'],
    ]);
  });

  it('strips markdown markup from headings and leaves out the tutor without a panel', async () => {
    const nb = { ...notebook(), cells: [{ cell_type: 'markdown', id: 'h', source: 'intro\n\n## The **bold** `code` [link](https://x.org) _end_\n\n### Later', metadata: { reader: { kind: 'explain' } } }] };
    await view(nb);
    const nav = screen.getByRole('navigation', { name: 'Lesson contents' });
    expect(within(nav).getAllByRole('link').map((a) => a.textContent)).toEqual(['The bold code link end']);
  });

  it('toggles behind a Contents button that reports its state and closes on an entry', async () => {
    await view();
    const toggle = screen.getByRole('button', { name: 'Contents' });
    const nav = screen.getByRole('navigation', { name: 'Lesson contents' });
    expect(toggle.getAttribute('aria-controls')).toBe(nav.id);
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    fireEvent.click(within(nav).getByRole('link', { name: 'Digests' }));
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
  });

  it('passes the focused cell and its contents label to the aside', async () => {
    const aside = vi.fn((id: string | null, label: string | null) => <p>{`${id} ${label}`}</p>);
    await view(notebook(), { aside });
    fireEvent.click(within(cellEl('w01-explain')).getByText('Digests'));
    expect(screen.getByText('w01-explain Digests')).toBeTruthy();
  });
});

describe('lesson page header and states (FR-NOTEBOOK-11, FR-NOTEBOOK-12)', () => {
  const json = (status: number, b: unknown) => new Response(JSON.stringify(b), { status, headers: { 'Content-Type': 'application/json' } });
  const mount = async (routes: (url: string) => Response | Promise<Response>) => {
    const { AppRoot } = await load(() => import('../app/AppRoot'));
    expect(AppRoot).toBeTypeOf('function');
    const urls: string[] = [];
    const fetchFn = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      urls.push(url);
      return url === '/api/me' ? json(200, me) : routes(url);
    });
    await act(async () => {
      render(<AppRoot initialPath="/lessons/w01" fetch={fetchFn as unknown as typeof fetch} navigateAway={vi.fn()} />);
    });
    return urls;
  };

  it('puts a Lessons link and the title on their own line above the controls', async () => {
    const sha = String(lessons().lesson.lesson.latest_sha256);
    await mount((url) => (url === '/api/lessons/w01' ? json(200, lessons().lesson) : url.endsWith(sha) ? json(200, notebook()) : json(404, {})));
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 })).toBeTruthy());
    expect(screen.getByRole('link', { name: 'Lessons' }).getAttribute('href')).toBe('/');
  });

  it('shows a busy skeleton while the lesson loads', async () => {
    await mount(() => new Promise<Response>(() => undefined));
    await waitFor(() => expect(document.querySelector('main[aria-busy="true"]')).toBeTruthy());
  });

  it('offers Retry and a way back when the lesson fails, and Retry refetches', async () => {
    const urls = await mount(() => json(500, { error: 'internal' }));
    await waitFor(() => expect(screen.getByText('The lesson could not be loaded.')).toBeTruthy());
    expect(screen.getByRole('link', { name: 'Back to lessons' }).getAttribute('href')).toBe('/');
    const before = urls.filter((u) => u === '/api/lessons/w01').length;
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    });
    await waitFor(() => expect(urls.filter((u) => u === '/api/lessons/w01').length).toBe(before + 1));
  });

  it('keeps the Lessons link on a lesson that is not published yet', async () => {
    await mount(() => json(200, { lesson: { ...lessons().lesson.lesson, latest_sha256: null }, revisions: [] }));
    await waitFor(() => expect(screen.getByText(/not published yet/i)).toBeTruthy());
    expect(screen.getByRole('link', { name: 'Lessons' }).getAttribute('href')).toBe('/');
  });
});
