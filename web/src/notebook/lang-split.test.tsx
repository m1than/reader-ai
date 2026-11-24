// @trace FR-UI-32
// WEB-UI FR-UI-32: `html[lang]` is the interface language; the lesson's language sits on its content nodes (the title
// h1, every [data-markdown] block, every output area), never on a container that also holds chrome. A user-written
// title in chrome gets lang from the Cyrillic rule over that text. Synthetic fixture W01 and invented text only.
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { readJson } from '../app/testing/fixtures';
import { load } from '../app/testing/load';

afterEach(() => {
  cleanup();
  localStorage.removeItem('reader-ai.locale');
  document.documentElement.lang = 'en';
});

type Cell = Record<string, unknown>;
type Nb = { cells: Cell[]; metadata: Record<string, unknown> };
const notebook = () => readJson<Nb>('spec/fixtures/WEB-NOTEBOOK/lesson-w01.ipynb');
const RU_TEXT = 'Децентрализация распределяет управление между узлами сети.';

const russian = (): Nb => {
  const nb = notebook();
  nb.cells = nb.cells.map((c) => (c.cell_type === 'markdown' ? { ...c, source: `${RU_TEXT}\n` } : c));
  return withTitle(nb, 'Децентрализация');
};
const withTitle = (nb: Nb, title: string): Nb => {
  const reader = (nb.metadata.reader ?? {}) as Record<string, unknown>;
  nb.metadata = { ...nb.metadata, reader: { ...reader, title } };
  return nb;
};

const controller = () => {
  const state = {
    cells: { 'w01-lab': { state: 'done', outputs: [{ output_type: 'stream', name: 'stdout', text: 'ok\n' }], executionCount: 1, marker: null, notes: [] } },
    slot: null, session: null, connection: 'none', resetting: false, resetFailed: false,
  };
  return {
    run: vi.fn(), runAll: vi.fn(), interrupt: vi.fn(), reset: vi.fn(), retry: vi.fn(), dispose: vi.fn(),
    getState: () => state,
    subscribe: () => () => undefined,
  };
};

const I18N_PATH = '../ui/i18n';
const langOf = (el: Element | null | undefined) => el?.closest('[lang]')?.getAttribute('lang') ?? null;

const COMBOS = [
  ['en', 'en'],
  ['en', 'ru'],
  ['ru', 'en'],
  ['ru', 'ru'],
] as const;

describe('chrome and content language (FR-UI-32)', () => {
  it.each(COMBOS)('interface %s, lesson %s', async (ui, lesson) => {
    const { LessonView } = await load(() => import('./LessonView'));
    const { I18nProvider } = await load(() => import(/* @vite-ignore */ I18N_PATH) as Promise<{ I18nProvider?: (p: { children: ReactNode; languages?: readonly string[] }) => ReactNode }>);
    expect(LessonView).toBeTypeOf('function');
    expect(I18nProvider).toBeTypeOf('function');
    if (!LessonView || !I18nProvider) return;
    localStorage.setItem('reader-ai.locale', ui);
    const nb = lesson === 'ru' ? russian() : withTitle(notebook(), 'Digests and hashes');
    const props = { notebook: nb as never, controller: controller() as never, loadEditor: () => new Promise<never>(() => undefined) };
    await act(async () => {
      render(
        <I18nProvider languages={['en-US']}>
          <LessonView {...(props as Parameters<typeof LessonView>[0])} />
        </I18nProvider>,
      );
    });
    await waitFor(() => expect(document.documentElement.lang).toBe(ui));
    const root = document.querySelector('div.lesson') as HTMLElement;
    expect(root).toBeTruthy();
    // The container that holds chrome carries no lang of its own: the toolbar inherits the interface language.
    expect(root.hasAttribute('lang')).toBe(false);
    const toolbar = root.querySelector('.lesson-toolbar');
    expect(toolbar).toBeTruthy();
    expect(langOf(toolbar)).toBe(ui);
    // Content nodes carry the lesson's language.
    const h1 = within(root).getByRole('heading', { level: 1 });
    expect(h1.getAttribute('lang')).toBe(lesson);
    const blocks = root.querySelectorAll('[data-markdown]');
    expect(blocks.length).toBeGreaterThan(0);
    for (const b of blocks) expect(langOf(b)).toBe(lesson);
    const outputs = root.querySelector('.outputs');
    expect(outputs).toBeTruthy();
    expect(langOf(outputs)).toBe(lesson);
    // A cell's chrome (its toolbar buttons) stays in the interface language.
    const run = root.querySelector('.cell[data-kind="lab"] button');
    expect(langOf(run)).toBe(ui);
  });
});

describe('user-written text in chrome (FR-UI-32)', () => {
  it('a Russian course title in an English list carries lang="ru"; an English one carries none other than en', async () => {
    const { AppRoot } = await load(() => import('../app/AppRoot'));
    expect(AppRoot).toBeTypeOf('function');
    const me = { id: '0b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b10', handle: 'ivan', role: 'learner', display_name: 'Ivan', consent: { evaluation: true, tuning: false } };
    const l = (id: string, course: string, title: string) => ({ lesson_id: id, course_id: `c-${id}`, course_title: course, ordinal: 1, title, updated_at: '2026-11-01T00:00:00Z', latest_sha256: '1'.repeat(64) });
    const list = { lessons: [l('a', 'Распределённые системы', 'Консенсус'), l('b', 'Databases', 'Indexes')] };
    const json = (b: unknown) => new Response(JSON.stringify(b), { status: 200, headers: { 'Content-Type': 'application/json' } });
    const fetchFn = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/api/me') return json(me);
      if (url === '/api/lessons') return json(list);
      return new Response(null, { status: 404 });
    });
    await act(async () => {
      render(<AppRoot initialPath="/" fetch={fetchFn as unknown as typeof fetch} navigateAway={vi.fn()} />);
    });
    const main = await waitFor(() => {
      const m = document.querySelector('main#content') as HTMLElement | null;
      if (!m || !within(m).queryByRole('heading', { name: 'Распределённые системы' })) throw new Error('not yet');
      return m;
    });
    expect(document.documentElement.lang).toBe('en');
    expect(langOf(within(main).getByRole('heading', { name: 'Распределённые системы' }))).toBe('ru');
    expect(langOf(within(main).getAllByText('Консенсус')[0])).toBe('ru');
    expect(langOf(within(main).getByRole('heading', { name: 'Databases' }))).toBe('en');
    expect(screen.getAllByText('Indexes').length).toBeGreaterThan(0);
  });
});
