// The tutor's Ask panel (SRV-TUTOR FR-TUTOR-11): a question about the open lesson (and the focused cell), polled
// until a terminal state; the answer is shown only after the server has checked it (no streaming).
import { Button } from '@acme/ui/button';
import { Fragment, useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import { ApiError } from '../app/api';
import { textLang, useT } from '../ui/i18n';
import { CloseIcon } from '../ui/icons';
import { groupCitations } from '../notebook/citations';
import notebookEn from '../notebook/messages.en';
import type { AnswerBlock, AskBody, HistoryItem, QuestionState, TutorApi } from './api';
import { messageFor } from './messages';
import en from './messages.en';
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
  const t = useT('tutor', en);
  const tn = useT('notebook', notebookEn);
  return (
    <div className="tutor-block">
      <TutorText text={block.text} />
      {block.citations.length > 0 && (
        <p className="muted tutor-citations">
          {/* One entry per material with page ranges, text only (WEB-NOTEBOOK FR-NOTEBOOK-2). */}
          {groupCitations(block.citations, undefined, tn).map((r, i) => (
            <Fragment key={i}>
              {i > 0 ? '; ' : ''}
              <span>{r.text}</span>
            </Fragment>
          ))}
        </p>
      )}
      {!block.supported && <p className="muted tutor-unsupported">{t('unsupported')}</p>}
    </div>
  );
}

function Result({ shown }: { shown: Shown }) {
  const t = useT('tutor', en);
  if (shown.kind === 'waiting') return <p className="muted">{t('answering')}</p>;
  if (shown.kind === 'message') return <p>{messageFor(shown.code, t)}</p>;
  if (shown.kind === 'answer') {
    const answer = shown.state.answer;
    return (
      <div className="tutor-answer">
        {answer?.blocks.map((b, i) => <Block key={i} block={b} />)}
        {shown.state.grounded === false && <p className="tutor-notice">{t('not_grounded')}</p>}
      </div>
    );
  }
  return null;
}

/** History shown before "Show n earlier questions" (FR-TUTOR-11, design pass): a structural bound. */
export const HISTORY_SHOWN = 3;

type Loaded = { kind: 'idle' } | { kind: 'loading' } | { kind: 'done'; shown: Shown } | { kind: 'failed' };

/** One earlier question: a disclosure whose answer is fetched once, the first time it opens (FR-TUTOR-11). */
function HistoryEntry({ item, lessonId, api }: { item: HistoryItem; lessonId: string; api: TutorApi }) {
  const [open, setOpen] = useState(false);
  const [loaded, setLoaded] = useState<Loaded>({ kind: 'idle' });
  const panelId = useId();
  const t = useT('tutor', en);
  const status =
    item.state === 'queued'
      ? t('history.waiting')
      : item.state === 'answered'
        ? item.grounded === false
          ? t('history.answered_ungrounded')
          : t('history.answered')
        : messageFor(item.error_code ?? (item.state === 'expired' ? 'expired' : 'tutor_failed'), t);
  const [polls, setPolls] = useState(0);
  const fetchOnce = () => {
    setLoaded((l) => (l.kind === 'done' ? l : { kind: 'loading' }));
    api.getQuestion(lessonId, item.question_id).then(
      (state) =>
        setLoaded({
          kind: 'done',
          shown: state.state === 'answered' ? { kind: 'answer', state } : state.state === 'queued' ? { kind: 'waiting' } : { kind: 'message', code: terminalCode(state) },
        }),
      () => setLoaded({ kind: 'failed' }),
    );
  };
  // A queued entry keeps being fetched with the panel's backoff while it is open and still waiting (code review).
  const waiting = loaded.kind === 'done' && loaded.shown.kind === 'waiting';
  useEffect(() => {
    if (!open || !waiting) return undefined;
    const timer = setTimeout(() => {
      setPolls((n) => n + 1);
      fetchOnce();
    }, POLL_BACKOFF_MS[Math.min(polls, POLL_BACKOFF_MS.length - 1)]);
    return () => clearTimeout(timer);
  }, [open, waiting, loaded]);
  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && (loaded.kind === 'idle' || waiting)) fetchOnce();
  };
  return (
    <li>
      <button type="button" className="history-toggle" aria-expanded={open} aria-controls={panelId} onClick={toggle}>
        <span lang={textLang(item.question)}>{item.question}</span> <span className="muted">{status}</span>
      </button>
      <div id={panelId} className="history-answer" hidden={!open}>
        {!open ? null : loaded.kind === 'done' && loaded.shown.kind === 'waiting' ? (
          <p className="muted">{t('history.waiting_answer')}</p>
        ) : loaded.kind === 'done' ? (
          <Result shown={loaded.shown} />
        ) : loaded.kind === 'failed' ? (
          <p>
            {messageFor('tutor_failed', t)}{' '}
            <Button size="sm" variant="ghost" color="neutral" onClick={fetchOnce}>
              {t('retry')}
            </Button>
          </p>
        ) : (
          <p className="muted">{t('history.loading')}</p>
        )}
      </div>
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
  /** The lesson view's panel state (design pass); without it the panel renders open, as a standalone panel. */
  open?: boolean;
  onClose?: () => void;
}

export function AskPanel({ lessonId, revision, cellId, cellLabel = null, api, open = true, onClose }: AskPanelProps) {
  const t = useT('tutor', en);
  const inputId = useId();
  const hintId = useId();
  const historyId = useId();
  const area = useRef<HTMLTextAreaElement>(null);
  const [allHistory, setAllHistory] = useState(false);
  // Opening focuses the text area (FR-TUTOR-11); the first render of an open standalone panel does not steal focus.
  const wasOpen = useRef(open);
  useEffect(() => {
    if (open && !wasOpen.current) area.current?.focus();
    wasOpen.current = open;
  }, [open]);
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

  const shownHistory = allHistory ? history : history.slice(0, HISTORY_SHOWN);
  return (
    <section
      className="tutor"
      id="tutor"
      aria-label={t('panel.label')}
      tabIndex={-1}
      hidden={!open}
      onKeyDown={(e) => {
        if (e.key === 'Escape' && onClose) {
          e.stopPropagation();
          onClose();
        }
      }}
    >
      <div className="tutor-head">
        <h2>{t('panel.heading')}</h2>
        {onClose ? (
          <Button size="sm" variant="ghost" color="neutral" aria-label={t('panel.close')} onClick={onClose}>
            <CloseIcon />
          </Button>
        ) : null}
      </div>
      {cellId !== null && (
        <p className="muted tutor-about">{cellLabel !== null ? t('panel.about', { label: cellLabel }) : t('panel.about_selected')}</p>
      )}
      <label htmlFor={inputId}>{t('panel.ask_label')}</label>
      <textarea ref={area} id={inputId} value={text} rows={3} aria-describedby={hintId} onChange={(e) => setText(e.target.value)} onKeyDown={onKey} />
      <div className="tutor-controls">
        <span className="muted tutor-count" aria-live="off">
          {t('panel.count', { n: String(text.length), max: String(QUESTION_CHARS_MAX) })}
        </span>
        <span className="muted tutor-hint" id={hintId}>
          {t('panel.hint')}
        </span>
        <Button size="sm" disabled={!valid || asking} onClick={() => void submit()}>
          {t('panel.ask')}
        </Button>
      </div>
      {current && (
        <p className="tutor-question" lang={textLang(current.question)}>
          {current.question}
        </p>
      )}
      <div aria-live="polite" className="tutor-result">
        <Result shown={shown} />
      </div>
      {history.length > 0 && (
        <>
          <h3 id={historyId}>{t('history.heading')}</h3>
          <ul className="tutor-history" role="list" aria-labelledby={historyId}>
            {shownHistory.map((item) => (
              <HistoryEntry key={item.question_id} item={item} lessonId={lessonId} api={api} />
            ))}
          </ul>
          {history.length > HISTORY_SHOWN ? (
            <Button size="sm" variant="ghost" color="neutral" onClick={() => setAllHistory((a) => !a)}>
              {allHistory ? t('history.fewer') : t('history.more', { count: history.length - HISTORY_SHOWN })}
            </Button>
          ) : null}
        </>
      )}
    </section>
  );
}
