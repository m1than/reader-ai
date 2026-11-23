// @trace FR-TUTOR-11
// Design pass (2026-11-23, V/design-pass, web only): the tutor as a closable side panel that keeps polling while
// closed, and the capped history whose entries expand to their answers. Synthetic questions and answers only.
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { readJson } from '../app/testing/fixtures';
import { load } from '../app/testing/load';
import { AskPanel } from './AskPanel';
import type { HistoryItem, QuestionState, TutorApi } from './api';
import { messageFor } from './messages';

const LESSON = '11111111-1111-4111-8111-111111111111';
const QID = '22222222-2222-4222-8222-222222222222';
const REV = 'a'.repeat(64);
const qid = (n: number) => `${String(n).padStart(8, '0')}-4444-4444-8444-444444444444`;

const answer = (text: string): QuestionState => ({
  state: 'answered',
  grounded: true,
  answer: {
    blocks: [{ text, supported: true, quote: 'q', citations: [{ span: `${'c'.repeat(64)}:0`, page: 7, material_title: 'Sample material' }] }],
    insufficient: false,
    retrieval: 'ok',
    prompt_sha256: 'b'.repeat(64),
    model: 'claude-sonnet-5-5',
  },
});
const item = (n: number, state: HistoryItem['state'] = 'answered'): HistoryItem => ({
  question_id: qid(n), question: `Synthetic question ${n}?`, cell_id: null, state, grounded: state === 'answered' ? true : null,
  error_code: state === 'failed' ? 'tutor_failed' : null, created_at: `2026-11-22T10:${String(59 - n).padStart(2, '0')}:00Z`,
});

const fakeApi = (history: HistoryItem[], byId: (id: string) => QuestionState | Error = () => answer('Synthetic answer.')) => {
  const api = {
    ask: vi.fn(async () => ({ question_id: QID })),
    getQuestion: vi.fn(async (_l: string, id: string) => {
      const r = byId(id);
      if (r instanceof Error) throw r;
      return r;
    }),
    listQuestions: vi.fn(async () => ({ questions: history })),
  };
  return api as typeof api & TutorApi;
};
const flush = async (ms = 0) => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
};
type PanelProps = Parameters<typeof AskPanel>[0] & { open?: boolean; onClose?: () => void };
const panel = (props: PanelProps) => <AskPanel {...(props as Parameters<typeof AskPanel>[0])} />;

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('side panel (FR-TUTOR-11, design pass)', () => {
  it('is hidden but mounted when closed, and keeps polling a question asked before closing', async () => {
    const api = fakeApi([], () => ({ state: 'queued' }) as QuestionState);
    const { rerender } = render(panel({ lessonId: LESSON, revision: REV, cellId: null, api, open: true, onClose: vi.fn() }));
    await flush();
    fireEvent.change(screen.getByLabelText('Ask the tutor'), { target: { value: 'Why?' } });
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }));
    await flush();
    rerender(panel({ lessonId: LESSON, revision: REV, cellId: null, api, open: false, onClose: vi.fn() }));
    const section = document.getElementById('tutor') as HTMLElement;
    expect(section.hidden).toBe(true);
    const before = api.getQuestion.mock.calls.length;
    await flush(3000);
    expect(api.getQuestion.mock.calls.length).toBeGreaterThan(before);
  });

  it('focuses the text area when it opens', async () => {
    const api = fakeApi([]);
    const { rerender } = render(panel({ lessonId: LESSON, revision: REV, cellId: null, api, open: false, onClose: vi.fn() }));
    await flush();
    rerender(panel({ lessonId: LESSON, revision: REV, cellId: null, api, open: true, onClose: vi.fn() }));
    await flush();
    expect(document.activeElement).toBe(screen.getByLabelText('Ask the tutor'));
  });

  it('closes with its Close button and with Escape inside it', async () => {
    const onClose = vi.fn();
    render(panel({ lessonId: LESSON, revision: REV, cellId: null, api: fakeApi([]), open: true, onClose }));
    await flush();
    fireEvent.click(screen.getByRole('button', { name: 'Close the tutor' }));
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(screen.getByLabelText('Ask the tutor'), { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it('is non-modal: no backdrop, the lesson stays clickable, and closing returns focus to the opener', async () => {
    const { LessonView } = await load(() => import('../notebook/LessonView'));
    const nb = readJson<Record<string, unknown>>('spec/fixtures/WEB-NOTEBOOK/lesson-w01.ipynb');
    const state = { cells: {}, slot: null, session: null, connection: 'none', resetting: false, resetFailed: false };
    const controller = { run: vi.fn(), runAll: vi.fn(), interrupt: vi.fn(), reset: vi.fn(), retry: vi.fn(), dispose: vi.fn(), getState: () => state, subscribe: () => () => undefined };
    const api = fakeApi([]);
    const aside = (cellId: string | null, label: string | null, p: { open: boolean; close: () => void }): ReactNode =>
      panel({ lessonId: LESSON, revision: REV, cellId, cellLabel: label, api, open: p.open, onClose: p.close });
    const props = { notebook: nb as never, controller: controller as never, aside, loadEditor: () => new Promise<never>(() => undefined) };
    render(<LessonView {...(props as unknown as Parameters<typeof LessonView>[0])} />);
    await flush();
    const opener = within(document.querySelector('.lesson-toolbar') as HTMLElement).getByRole('button', { name: 'Ask the tutor' });
    opener.focus();
    fireEvent.click(opener);
    await flush();
    expect((document.getElementById('tutor') as HTMLElement).hidden).toBe(false);
    expect(document.querySelector('[data-backdrop], .acme-backdrop, [class*="backdrop"]')).toBeNull();
    fireEvent.click(within(document.querySelector('.lesson-toolbar') as HTMLElement).getByRole('button', { name: /run all/i }));
    expect(controller.runAll).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Close the tutor' }));
    await flush();
    expect((document.getElementById('tutor') as HTMLElement).hidden).toBe(true);
    expect(document.activeElement).toBe(opener);
  });

  const lessonWithPanel = async () => {
    const { LessonView } = await load(() => import('../notebook/LessonView'));
    const nb = readJson<Record<string, unknown>>('spec/fixtures/WEB-NOTEBOOK/lesson-w01.ipynb');
    const state = { cells: {}, slot: null, session: null, connection: 'none', resetting: false, resetFailed: false };
    const controller = { run: vi.fn(), runAll: vi.fn(), interrupt: vi.fn(), reset: vi.fn(), retry: vi.fn(), dispose: vi.fn(), getState: () => state, subscribe: () => () => undefined };
    const api = fakeApi([]);
    const aside = (cellId: string | null, label: string | null, p: { open: boolean; close: () => void }): ReactNode =>
      panel({ lessonId: LESSON, revision: REV, cellId, cellLabel: label, api, open: p.open, onClose: p.close });
    const props = { notebook: nb as never, controller: controller as never, aside, loadEditor: () => new Promise<never>(() => undefined) };
    render(<LessonView {...(props as unknown as Parameters<typeof LessonView>[0])} />);
    await flush();
  };
  const tutorHidden = () => (document.getElementById('tutor') as HTMLElement).hidden;

  it('closes on Escape from the page outside an editable field, but not from inside a text area', async () => {
    await lessonWithPanel();
    fireEvent.click(within(document.querySelector('.lesson-toolbar') as HTMLElement).getByRole('button', { name: 'Ask the tutor' }));
    await flush();
    const area = within(document.querySelector('[data-cell-id="w01-lab"]') as HTMLElement).getByRole('textbox', { name: 'Lab 1 code' });
    area.focus();
    fireEvent.keyDown(area, { key: 'Escape' });
    await flush();
    expect(tutorHidden()).toBe(false);
    const cell = document.querySelector('[data-cell-id="w01-route"]') as HTMLElement;
    cell.focus();
    fireEvent.keyDown(cell, { key: 'Escape' });
    await flush();
    expect(tutorHidden()).toBe(true);
  });

  it('returns focus to the toolbar\'s "Ask the tutor" when the opener is gone', async () => {
    await lessonWithPanel();
    const cell = document.querySelector('[data-cell-id="w01-explain"]') as HTMLElement;
    const cellAsk = within(cell).getByRole('button', { name: 'Ask the tutor about this cell' });
    cellAsk.focus();
    fireEvent.click(cellAsk);
    await flush();
    cellAsk.remove();
    fireEvent.click(screen.getByRole('button', { name: 'Close the tutor' }));
    await flush();
    expect(document.activeElement).toBe(within(document.querySelector('.lesson-toolbar') as HTMLElement).getByRole('button', { name: 'Ask the tutor' }));
  });
});

describe('history (FR-TUTOR-11, design pass)', () => {
  const entries = () => within(screen.getByRole('list', { name: 'Earlier questions' })).getAllByRole('listitem');

  it('shows the 3 most recent, then the rest behind "Show n earlier questions"', async () => {
    const history = [1, 2, 3, 4, 5, 6, 7, 8].map((n) => item(n));
    render(panel({ lessonId: LESSON, revision: REV, cellId: null, api: fakeApi(history) }));
    await flush();
    expect(entries()).toHaveLength(3);
    expect(entries()[0]!.textContent).toContain('Synthetic question 1?');
    fireEvent.click(screen.getByRole('button', { name: 'Show 5 earlier questions' }));
    expect(entries()).toHaveLength(8);
    fireEvent.click(screen.getByRole('button', { name: 'Show fewer' }));
    expect(entries()).toHaveLength(3);
  });

  it('expands an entry to its answer, fetching it once', async () => {
    const api = fakeApi([item(1), item(2)], (id) => answer(id === qid(2) ? 'Second synthetic answer.' : 'First synthetic answer.'));
    render(panel({ lessonId: LESSON, revision: REV, cellId: null, api }));
    await flush();
    const toggle = within(entries()[1]!).getByRole('button', { name: /Synthetic question 2\?/ });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(toggle);
    await flush();
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(within(entries()[1]!).getByText('Second synthetic answer.')).toBeTruthy();
    expect(within(entries()[1]!).getByText('Sample material, p. 7')).toBeTruthy();
    fireEvent.click(toggle);
    fireEvent.click(toggle);
    await flush();
    expect(api.getQuestion.mock.calls.filter((c) => c[1] === qid(2))).toHaveLength(1);
  });

  it('shows tutor_failed with Retry when the fetch fails, and Retry fetches again', async () => {
    let fail = true;
    const api = fakeApi([item(1)], () => (fail ? new Error('down') : answer('Recovered synthetic answer.')));
    render(panel({ lessonId: LESSON, revision: REV, cellId: null, api }));
    await flush();
    fireEvent.click(within(entries()[0]!).getByRole('button', { name: /Synthetic question 1\?/ }));
    await flush();
    expect(within(entries()[0]!).getByText(messageFor('tutor_failed'))).toBeTruthy();
    fail = false;
    fireEvent.click(within(entries()[0]!).getByRole('button', { name: 'Retry' }));
    await flush();
    expect(within(entries()[0]!).getByText('Recovered synthetic answer.')).toBeTruthy();
  });

  it('has the AskPanel stories, each rendering without an error', async () => {
    vi.useRealTimers();
    const stories = import.meta.glob('./*.stories.tsx');
    const importer = stories['./AskPanel.stories.tsx'];
    expect(importer, 'web/src/tutor/AskPanel.stories.tsx').toBeTypeOf('function');
    if (!importer) return;
    const { composeStories } = await import('@storybook/react-vite');
    const composed = composeStories((await importer()) as Parameters<typeof composeStories>[0]) as Record<string, React.ComponentType>;
    expect(Object.keys(composed)).toEqual(expect.arrayContaining(['Light', 'Dark', 'Closed', 'OpenEmpty', 'Waiting', 'Ungrounded', 'Failed', 'History']));
    for (const Story of Object.values(composed)) {
      const { unmount } = render(<Story />);
      unmount();
    }
  });

  it('renders an expanded stored answer without links or images and fetches nothing else', async () => {
    const hostile = answer('See [x](https://example.test) and ![i](https://example.test/i.png) here.');
    const api = fakeApi([item(1)], () => hostile);
    const fetchSpy = vi.fn(async () => new Response(null, { status: 599 }));
    vi.stubGlobal('fetch', fetchSpy);
    try {
      render(panel({ lessonId: LESSON, revision: REV, cellId: null, api }));
      await flush();
      const entry = entries()[0]!;
      fireEvent.click(within(entry).getByRole('button', { name: /Synthetic question 1\?/ }));
      await flush();
      expect(entry.textContent).toContain('https://example.test');
      expect(entry.querySelectorAll('a')).toHaveLength(0);
      expect(entry.querySelectorAll('img')).toHaveLength(0);
      expect(api.getQuestion.mock.calls.map((c) => c[1])).toEqual([qid(1)]);
      expect(fetchSpy).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('expands a queued entry to "Waiting for the answer"', async () => {
    // The first queued question resumes as the current one; a later queued one stays in the history.
    const resumed = { ...item(3, 'queued'), question_id: qid(3) };
    const queued = { ...item(1, 'queued'), question_id: qid(1) };
    const api = fakeApi([item(2), resumed, queued], () => ({ state: 'queued' }) as QuestionState);
    render(panel({ lessonId: LESSON, revision: REV, cellId: null, api }));
    await flush();
    const entry = entries().find((e) => e.textContent?.includes('Synthetic question 1?'))!;
    fireEvent.click(within(entry).getByRole('button', { name: /Synthetic question 1\?/ }));
    await flush();
    expect(within(entry).getByText('Waiting for the answer')).toBeTruthy();
  });
});
