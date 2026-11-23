// The tutor's Ask panel (SRV-TUTOR FR-TUTOR-11): a question about the open lesson (and the focused cell), polled
// until a terminal state; the answer is shown only after the server has checked it (no streaming).
import { Button } from '@acme/ui/button';
import { Fragment, useEffect, useId, useState, type KeyboardEvent } from 'react';
import { ApiError } from '../app/api';
import { groupCitations } from '../notebook/citations';
import type { AnswerBlock, AskBody, HistoryItem, QuestionState, TutorApi } from './api';
import { messageFor } from './messages';
import { TutorText } from './TutorText';

export const QUESTION_CHARS_MAX = 1000;
export const POLL_BACKOFF_MS: readonly number[] = [1000, 2000, 5000];
export const POLL_DEADLINE_S = 630; // the server's question TTL (600 s) + 30 s

type Shown =
  | { kind: 'none' }
  | { kind: 'waiting' }
  | { kind: 'answer'; state: QuestionState }
  | { kind: 'message'; code: string };

interface Current {
  question_id: string;
  question: string;
}

const terminalCode = (s: QuestionState): string => s.error_code ?? (s.state === 'expired' ? 'expired' : 'tutor_failed');

function Block({ block }: { block: AnswerBlock }) {
  return (
    <div className="tutor-block">
      <TutorText text={block.text} />
      {block.citations.length > 0 && (
        <p className="muted tutor-citations">
          {/* One entry per material with page ranges, text only (WEB-NOTEBOOK FR-NOTEBOOK-2). */}
          {groupCitations(block.citations).map((r, i) => (
            <Fragment key={i}>
              {i > 0 ? '; ' : ''}
              <span>{r.text}</span>
            </Fragment>
          ))}
        </p>
      )}
      {!block.supported && <p className="muted tutor-unsupported">not supported by the source</p>}
    </div>
  );
}

function Result({ shown }: { shown: Shown }) {
  if (shown.kind === 'waiting') return <p className="muted">The tutor is answering…</p>;
  if (shown.kind === 'message') return <p>{messageFor(shown.code)}</p>;
  if (shown.kind === 'answer') {
    const answer = shown.state.answer;
    return (
      <div className="tutor-answer">
        {answer?.blocks.map((b, i) => <Block key={i} block={b} />)}
        {shown.state.grounded === false && <p className="tutor-notice">This answer is not fully grounded in the book.</p>}
      </div>
    );
  }
  return null;
}

function HistoryEntry({ item }: { item: HistoryItem }) {
  const status =
    item.state === 'queued'
      ? 'waiting'
      : item.state === 'answered'
        ? item.grounded === false
          ? 'answered, not fully grounded'
          : 'answered'
        : messageFor(item.error_code ?? (item.state === 'expired' ? 'expired' : 'tutor_failed'));
  return (
    <li>
      <span>{item.question}</span> <span className="muted">{status}</span>
    </li>
  );
}

export interface AskPanelProps {
  lessonId: string;
  revision: string | null;
  cellId: string | null;
  /** The focused cell's lesson-contents label (WEB-NOTEBOOK FR-NOTEBOOK-11), shown as "About: …". */
  cellLabel?: string | null;
  api: TutorApi;
}

export function AskPanel({ lessonId, revision, cellId, cellLabel = null, api }: AskPanelProps) {
  const inputId = useId();
  const hintId = useId();
  const [text, setText] = useState('');
  const [asking, setAsking] = useState(false);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [current, setCurrent] = useState<Current | null>(null);
  const [shown, setShown] = useState<Shown>({ kind: 'none' });
  const [target, setTarget] = useState<string | null>(null);

  // On load: the history; a queued question resumes polling.
  useEffect(() => {
    let live = true;
    api.listQuestions(lessonId).then(
      ({ questions }) => {
        if (!live) return;
        const queued = questions.find((q) => q.state === 'queued');
        setHistory(questions.filter((q) => q !== queued));
        if (queued) {
          setCurrent({ question_id: queued.question_id, question: queued.question });
          setShown({ kind: 'waiting' });
          setTarget(queued.question_id);
        }
      },
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, [api, lessonId]);

  // Polling one question: backoff, deadline, paused while the page is hidden, stopped by 401 and 404.
  useEffect(() => {
    if (target === null) return;
    const started = Date.now();
    let polls = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let paused = false;
    let done = false;
    const end = (next: Shown) => {
      done = true;
      setShown(next);
    };
    const schedule = () => {
      timer = setTimeout(() => void fire(), POLL_BACKOFF_MS[Math.min(polls, POLL_BACKOFF_MS.length - 1)]);
    };
    const fire = async () => {
      timer = undefined;
      if (done) return;
      if (Date.now() - started >= POLL_DEADLINE_S * 1000) return end({ kind: 'message', code: 'expired' });
      if (document.hidden) {
        paused = true;
        return;
      }
      polls += 1;
      try {
        const state = await api.getQuestion(lessonId, target);
        if (done) return;
        if (state.state === 'queued') return schedule();
        end(state.state === 'answered' ? { kind: 'answer', state } : { kind: 'message', code: terminalCode(state) });
      } catch (error) {
        if (done) return;
        if (error instanceof ApiError && error.status === 401) return end({ kind: 'none' });
        if (error instanceof ApiError && error.status === 404) return end({ kind: 'message', code: 'not_found' });
        schedule();
      }
    };
    const onVisibility = () => {
      if (!document.hidden && paused && !done) {
        paused = false;
        void fire();
      }
    };
    document.addEventListener('visibilitychange', onVisibility);
    schedule();
    return () => {
      done = true;
      if (timer !== undefined) clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [target, api, lessonId]);

  const question = text.trim();
  const valid = question.length >= 1 && text.length <= QUESTION_CHARS_MAX;

  const submit = async () => {
    if (!valid || asking) return;
    setAsking(true);
    const body: AskBody = { question };
    if (cellId) body.cell_id = cellId;
    if (revision) body.revision = revision;
    try {
      const { question_id } = await api.ask(lessonId, body);
      if (current) {
        const previous: HistoryItem = {
          question_id: current.question_id,
          question: current.question,
          cell_id: null,
          state: shown.kind === 'answer' ? 'answered' : shown.kind === 'message' ? 'failed' : 'queued',
          grounded: shown.kind === 'answer' ? (shown.state.grounded ?? null) : null,
          error_code: shown.kind === 'message' ? shown.code : null,
          created_at: new Date().toISOString(),
        };
        setHistory((h) => [previous, ...h]);
      }
      setCurrent({ question_id, question });
      setText('');
      setShown({ kind: 'waiting' });
      setTarget(question_id);
    } catch (error) {
      setShown({ kind: 'message', code: error instanceof ApiError ? error.code : 'tutor_failed' });
    } finally {
      setAsking(false);
    }
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      void submit();
    }
  };

  return (
    <section className="tutor" id="tutor" aria-label="Tutor" tabIndex={-1}>
      <h2>Tutor</h2>
      {cellId !== null && <p className="muted tutor-about">{`About: ${cellLabel ?? 'the selected cell'}`}</p>}
      <label htmlFor={inputId}>Ask the tutor</label>
      <textarea id={inputId} value={text} rows={3} aria-describedby={hintId} onChange={(e) => setText(e.target.value)} onKeyDown={onKey} />
      <div className="tutor-controls">
        <span className="muted tutor-count" aria-live="off">{`${text.length} / ${QUESTION_CHARS_MAX}`}</span>
        <span className="muted tutor-hint" id={hintId}>
          Ctrl+Enter or ⌘+Enter to ask
        </span>
        <Button size="sm" disabled={!valid || asking} onClick={() => void submit()}>
          Ask
        </Button>
      </div>
      {current && <p className="tutor-question">{current.question}</p>}
      <div aria-live="polite" className="tutor-result">
        <Result shown={shown} />
      </div>
      {history.length > 0 && (
        <>
          <h3>Earlier questions</h3>
          <ul className="tutor-history">
            {history.map((item) => (
              <HistoryEntry key={item.question_id} item={item} />
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
