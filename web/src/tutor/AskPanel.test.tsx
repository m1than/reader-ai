// @trace FR-TUTOR-11 VEC-TUTOR-5 VEC-TUTOR-6
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../app/api';
import { readYaml } from '../app/testing/fixtures';
import { AskPanel, POLL_BACKOFF_MS, POLL_DEADLINE_S, QUESTION_CHARS_MAX } from './AskPanel';
import type { AskBody, HistoryItem, QuestionState, TutorApi } from './api';
import { messageFor } from './messages';
import { TutorText } from './TutorText';

type Bounds = { question_chars_max: number; poll_backoff_ms: number[]; poll_deadline_s: number; question_ttl_s: number };
const spec = () => readYaml<{ bounds: Bounds; errors: string[] }>('spec/modules/SRV-TUTOR.yaml');
const catalogue = () => readYaml<{ codes: Record<string, { message: string }> }>('spec/fixtures/SRV-TUTOR/errors.yaml').codes;
type RenderCase = { name: string; text: string; expect: Record<string, number | string> };
const renderCases = () => readYaml<{ cases: RenderCase[] }>('spec/fixtures/SRV-TUTOR/render.yaml').cases;

const LESSON = '11111111-1111-4111-8111-111111111111';
const QID = '22222222-2222-4222-8222-222222222222';
const REV = 'a'.repeat(64);

const answered: QuestionState = {
  state: 'answered',
  grounded: false,
  answer: {
    blocks: [
      { text: 'Miners vary the **nonce**.', supported: true, quote: 'Miners vary the nonce until the hash', citations: [{ span: `${'c'.repeat(64)}:0`, page: 61, material_title: 'Sample material' }] },
      { text: 'An invented claim.', supported: false, quote: 'nothing like it', citations: [] },
    ],
    insufficient: false,
    retrieval: 'ok',
    prompt_sha256: 'b'.repeat(64),
    model: 'claude-sonnet-5-5',
  },
};

interface Fake extends TutorApi {
  asked: Array<{ lessonId: string; body: Record<string, unknown> }>;
  gets: number[];
}

const fakeApi = (states: Array<QuestionState | Error>, history: HistoryItem[] = []): Fake => {
  const api: Fake = {
    asked: [],
    gets: [],
    ask: vi.fn(async (lessonId: string, body: AskBody) => {
      api.asked.push({ lessonId, body: { ...body } });
      return { question_id: QID };
    }),
    getQuestion: vi.fn(async () => {
      api.gets.push(Date.now());
      const next = states.length > 1 ? states.shift()! : states[0]!;
      if (next instanceof Error) throw next;
      return next;
    }),
    listQuestions: vi.fn(async () => ({ questions: history })),
  } as Fake;
  return api;
};

const flush = async (ms = 0) => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
};

const setHidden = (hidden: boolean) => {
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => (hidden ? 'hidden' : 'visible') });
  document.dispatchEvent(new Event('visibilitychange'));
};

const ask = async (text = 'What is a nonce?') => {
  fireEvent.change(screen.getByLabelText('Ask the tutor'), { target: { value: text } });
  fireEvent.click(screen.getByRole('button', { name: 'Ask' }));
  await flush();
};

beforeEach(() => {
  vi.useFakeTimers();
  setHidden(false);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('AskPanel (FR-TUTOR-11)', () => {
  it('uses the spec bounds', () => {
    const b = spec().bounds;
    expect(QUESTION_CHARS_MAX).toBe(b.question_chars_max);
    expect(POLL_BACKOFF_MS).toEqual(b.poll_backoff_ms);
    expect(POLL_DEADLINE_S).toBe(b.poll_deadline_s);
    expect(POLL_DEADLINE_S).toBe(b.question_ttl_s + 30);
  });

  it('has a labelled text area with a counter and refuses empty or over-long questions', async () => {
    render(<AskPanel lessonId={LESSON} revision={REV} cellId={null} api={fakeApi([answered])} />);
    await flush();
    const box = screen.getByLabelText('Ask the tutor');
    const button = screen.getByRole('button', { name: 'Ask' }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.change(box, { target: { value: '   ' } });
    expect(button.disabled).toBe(true);
    fireEvent.change(box, { target: { value: 'abc' } });
    expect(screen.getByText('3 / 1000')).toBeTruthy();
    expect(button.disabled).toBe(false);
    fireEvent.change(box, { target: { value: 'x'.repeat(1001) } });
    expect(button.disabled).toBe(true);
    expect(screen.getByText('1001 / 1000')).toBeTruthy();
  });

  it('asks with the focused cell and revision, polls with backoff and renders the answer', async () => {
    const api = fakeApi([{ state: 'queued' }, { state: 'queued' }, { state: 'queued' }, { state: 'queued' }, answered]);
    render(<AskPanel lessonId={LESSON} revision={REV} cellId="f01-explain" api={api} />);
    await flush();
    await ask();
    expect(api.asked).toEqual([{ lessonId: LESSON, body: { question: 'What is a nonce?', cell_id: 'f01-explain', revision: REV } }]);
    expect(api.getQuestion).toHaveBeenCalledTimes(0);
    await flush(999);
    expect(api.getQuestion).toHaveBeenCalledTimes(0);
    await flush(1);
    expect(api.getQuestion).toHaveBeenCalledTimes(1);
    await flush(2000);
    expect(api.getQuestion).toHaveBeenCalledTimes(2);
    await flush(4999);
    expect(api.getQuestion).toHaveBeenCalledTimes(2);
    await flush(1);
    expect(api.getQuestion).toHaveBeenCalledTimes(3);
    await flush(5000);
    expect(api.getQuestion).toHaveBeenCalledTimes(4);
    await flush(5000);
    expect(api.getQuestion).toHaveBeenCalledTimes(5);
    await flush(20_000);
    expect(api.getQuestion).toHaveBeenCalledTimes(5); // terminal: no more polling
    const live = document.querySelector('[aria-live="polite"]') as HTMLElement;
    expect(live).toBeTruthy();
    expect(within(live).getByText('nonce').tagName).toBe('STRONG');
    expect(within(live).getByText('Sample material, p. 61')).toBeTruthy();
    expect(within(live).getByText('not supported by the source')).toBeTruthy();
    expect(within(live).getByText('This answer is not fully grounded in the book.')).toBeTruthy();
  });

  it('asks with Ctrl+Enter and Cmd+Enter', async () => {
    const api = fakeApi([answered]);
    render(<AskPanel lessonId={LESSON} revision={null} cellId={null} api={api} />);
    await flush();
    const box = screen.getByLabelText('Ask the tutor');
    fireEvent.change(box, { target: { value: 'one' } });
    fireEvent.keyDown(box, { key: 'Enter', ctrlKey: true });
    await flush();
    fireEvent.change(box, { target: { value: 'two' } });
    fireEvent.keyDown(box, { key: 'Enter', metaKey: true });
    await flush();
    fireEvent.keyDown(box, { key: 'Enter' }); // a plain Enter is a newline
    await flush();
    expect(api.asked.map((a) => a.body.question)).toEqual(['one', 'two']);
    expect(api.asked[0]!.body).toEqual({ question: 'one' });
  });

  it('pauses polling while the page is hidden and resumes when visible', async () => {
    const api = fakeApi([{ state: 'queued' }]);
    render(<AskPanel lessonId={LESSON} revision={REV} cellId={null} api={api} />);
    await flush();
    await ask();
    await flush(1000);
    expect(api.getQuestion).toHaveBeenCalledTimes(1);
    act(() => setHidden(true));
    await flush(60_000);
    expect(api.getQuestion).toHaveBeenCalledTimes(1);
    act(() => setHidden(false));
    await flush(0);
    expect(api.getQuestion).toHaveBeenCalledTimes(2);
  });

  it('stops polling on 404 and on 401', async () => {
    for (const error of [new ApiError(404, 'not_found'), new ApiError(401, 'unauthenticated')]) {
      const api = fakeApi([error]);
      render(<AskPanel lessonId={LESSON} revision={REV} cellId={null} api={api} />);
      await flush();
      await ask();
      await flush(1000);
      await flush(60_000);
      expect(api.getQuestion).toHaveBeenCalledTimes(1);
      cleanup();
    }
  });

  it('stops at the poll deadline', async () => {
    const api = fakeApi([{ state: 'queued' }]);
    render(<AskPanel lessonId={LESSON} revision={REV} cellId={null} api={api} />);
    await flush();
    await ask();
    await flush(POLL_DEADLINE_S * 1000);
    const calls = (api.getQuestion as ReturnType<typeof vi.fn>).mock.calls.length;
    await flush(60_000);
    expect((api.getQuestion as ReturnType<typeof vi.fn>).mock.calls.length).toBe(calls);
    expect(screen.getByText(messageFor('expired'))).toBeTruthy();
  });

  it('resumes polling a queued history item on load and lists the history', async () => {
    const history: HistoryItem[] = [
      { question_id: QID, question: 'Pending?', cell_id: null, state: 'queued', grounded: null, error_code: null, created_at: '2026-11-22T10:00:00Z' },
      { question_id: '33333333-3333-4333-8333-333333333333', question: 'Old one', cell_id: 'f01-lab', state: 'failed', grounded: null, error_code: 'rate_limited', created_at: '2026-11-22T09:00:00Z' },
    ];
    const api = fakeApi([answered], history);
    render(<AskPanel lessonId={LESSON} revision={REV} cellId={null} api={api} />);
    await flush();
    expect(screen.getByText('Pending?')).toBeTruthy();
    expect(screen.getByText('Old one')).toBeTruthy();
    expect(screen.getByText(messageFor('rate_limited'))).toBeTruthy();
    await flush(1000);
    expect(api.getQuestion).toHaveBeenCalledWith(LESSON, QID);
  });

  it('shows the catalogue message of a refused ask and of a failed question', async () => {
    const api = fakeApi([{ state: 'refused', error_code: 'daily_budget_exhausted' }]);
    (api.ask as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new ApiError(409, 'tutor_busy'));
    render(<AskPanel lessonId={LESSON} revision={REV} cellId={null} api={api} />);
    await flush();
    await ask();
    expect(screen.getByText(messageFor('tutor_busy'))).toBeTruthy();
    await ask('again');
    await flush(1000);
    expect(screen.getByText(messageFor('daily_budget_exhausted'))).toBeTruthy();
  });
});

describe('error catalogue (VEC-TUTOR-5)', () => {
  it('maps every code to its message and unknown codes to tutor_failed', () => {
    const codes = catalogue();
    expect(Object.keys(codes)).toEqual(spec().errors);
    for (const [code, { message }] of Object.entries(codes)) expect(messageFor(code)).toBe(message);
    expect(messageFor('no_such_code')).toBe(codes.tutor_failed!.message);
    expect(messageFor('constructor')).toBe(codes.tutor_failed!.message);
  });
});

describe('panel layout and grouped citations (FR-TUTOR-11, amended 2026-11-23)', () => {
  const m = 'c'.repeat(64);
  const many: QuestionState = {
    ...answered,
    answer: {
      ...answered.answer!,
      blocks: [
        {
          text: 'One block.', supported: true, quote: 'q',
          citations: [60, 60, 61, 63, 60].map((page, i) => ({ span: `${m}:${i}`, page, material_title: 'Sample material' })),
        },
      ],
    },
  };

  it('shows one citation entry per material with page ranges', async () => {
    render(<AskPanel lessonId={LESSON} revision={REV} cellId={null} api={fakeApi([many])} />);
    await flush();
    await ask();
    await flush(1000);
    const live = document.querySelector('[aria-live="polite"]') as HTMLElement;
    expect(within(live).getByText('Sample material, pp. 60–61, 63')).toBeTruthy();
    expect(within(live).queryAllByText(/Sample material/)).toHaveLength(1);
  });

  it('has the id the lesson contents link to, the heading Tutor, a shortcut hint and the focused cell label', async () => {
    render(<AskPanel lessonId={LESSON} revision={REV} cellId="ex-intro" cellLabel="Sample heading" api={fakeApi([answered])} />);
    await flush();
    const panel = screen.getByRole('region', { name: 'Tutor' });
    expect(panel.id).toBe('tutor');
    expect(within(panel).getByRole('heading', { level: 2 }).textContent).toBe('Tutor');
    expect(within(panel).getByText('Ctrl+Enter or ⌘+Enter to ask')).toBeTruthy();
    expect(within(panel).getByText('About: Sample heading')).toBeTruthy();
  });

  it('names an unlabelled focused cell generically and shows nothing without a focused cell', async () => {
    const { rerender } = render(<AskPanel lessonId={LESSON} revision={REV} cellId="x" cellLabel={null} api={fakeApi([answered])} />);
    await flush();
    expect(screen.getByText('About: the selected cell')).toBeTruthy();
    rerender(<AskPanel lessonId={LESSON} revision={REV} cellId={null} cellLabel={null} api={fakeApi([answered])} />);
    await flush();
    expect(screen.queryByText(/^About:/)).toBeNull();
  });
});

describe('answer text rendering (VEC-TUTOR-6)', () => {
  for (const c of renderCases()) {
    it(c.name, async () => {
      vi.useRealTimers();
      const fetchSpy = vi.spyOn(globalThis, 'fetch');
      const { container } = render(<TutorText text={c.text} />);
      const e = c.expect;
      if (e.anchors !== undefined) expect(container.querySelectorAll('a').length).toBe(e.anchors);
      if (e.images !== undefined) expect(container.querySelectorAll('img, picture, source').length).toBe(e.images);
      if (e.scripts !== undefined) expect(container.querySelectorAll('script').length).toBe(e.scripts);
      if (e.text_contains !== undefined) expect(container.textContent).toContain(e.text_contains);
      if (e.katex !== undefined) expect(container.querySelectorAll('.katex').length).toBe(e.katex);
      if (e.code !== undefined) expect(container.querySelectorAll('code').length).toBe(e.code);
      if (e.elements_from_input !== undefined) expect(container.querySelectorAll('b, script').length).toBe(e.elements_from_input);
      if (e.requests !== undefined) expect(fetchSpy).not.toHaveBeenCalled();
      fetchSpy.mockRestore();
    });
  }
});
