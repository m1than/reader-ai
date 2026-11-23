// The owner's compile control (WEB-UI FR-UI-15; WEB-NOTEBOOK FR-NOTEBOOK-12): the run state, the draft chip, Compile
// or Retry with a confirm, and polling with a 30-minute stop. Keyed on the run's state, never on `latest_sha256` alone:
// the draft step writes a revision long before the run ends.
import { Button } from '@acme/ui/button';
import { Chip } from '@acme/ui/chip';
import { Dialog } from '@acme/ui/dialog';
import { type QueryClient, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { ApiError, type CompileStatus, type LessonSummary } from '../api';
import { useServices } from '../context';
import { queries } from '../queries';
import {
  COMPILE_POLL_MAX_MS,
  RUN_CAP_USD,
  describeRun,
  estimateLines,
  isOpen,
  money,
  pollInterval,
  polls,
  postOutcome,
} from './model';

export const DRAFT_CHIP = 'draft, compile not finished';

// The start of each query's polling window, per query client (FR-UI-15's 30-minute stop).
const windows = new WeakMap<QueryClient, Map<string, number>>();
const windowStart = (client: QueryClient, key: string): number => {
  let byKey = windows.get(client);
  if (!byKey) {
    byKey = new Map();
    windows.set(client, byKey);
  }
  let start = byKey.get(key);
  if (start === undefined) {
    start = Date.now();
    byKey.set(key, start);
  }
  return start;
};
const restartWindow = (client: QueryClient, key: string) => {
  windowStart(client, key);
  windows.get(client)?.set(key, Date.now());
};

type Source = 'list' | 'page';

interface RunState {
  /** undefined: not known (loading or failed); null: no run (list: no open run). */
  run: CompileStatus | null | undefined;
  failed: boolean;
  /** Whether the query's data asks for polling (before the window). */
  active: boolean;
  updatedAt: number;
  windowKey: string;
}

function useRunState(lesson: LessonSummary, source: Source): RunState {
  const { api } = useServices();
  const client = useQueryClient();
  const q = queries(api);
  const listKey = 'compiles';
  const pageKey = `compile:${lesson.lesson_id}`;
  const list = useQuery({
    ...q.openCompiles(),
    enabled: source === 'list',
    refetchInterval: (query) =>
      pollInterval(query.state.data?.runs.some((r) => polls(r.state)) ? 'running' : null, windowStart(client, listKey), Date.now()),
  });
  const page = useQuery({
    ...q.compile(lesson.lesson_id),
    enabled: source === 'page',
    refetchInterval: (query) => pollInterval(query.state.data?.state ?? null, windowStart(client, pageKey), Date.now()),
  });
  if (source === 'list') {
    const data = list.data;
    return {
      run: data ? (data.runs.find((r) => r.lesson_id === lesson.lesson_id) ?? null) : undefined,
      failed: list.isError,
      active: Boolean(data?.runs.some((r) => polls(r.state))),
      updatedAt: list.dataUpdatedAt,
      windowKey: listKey,
    };
  }
  return {
    run: page.data,
    failed: page.isError,
    active: polls(page.data?.state ?? null),
    updatedAt: page.dataUpdatedAt,
    windowKey: pageKey,
  };
}

function EstimateText({ lessonId, onEmpty }: { lessonId: string; onEmpty: (empty: boolean) => void }) {
  const { api } = useServices();
  const q = useQuery(queries(api).estimate(lessonId));
  const shown = q.data ? estimateLines(q.data) : null;
  const empty = shown?.empty ?? false;
  useEffect(() => onEmpty(empty), [empty, onEmpty]);
  const stops = <p>Stops at ${q.data?.run_cap_usd ?? RUN_CAP_USD}.</p>;
  if (q.isPending)
    return (
      <>
        <p className="muted">Estimating…</p>
        {stops}
      </>
    );
  return (
    <>
      {shown ? shown.lines.map((line) => <p key={line}>{line}</p>) : <p>No estimate for this lesson.</p>}
      {stops}
    </>
  );
}

export interface CompileControlProps {
  lesson: LessonSummary;
  /** `list`: the run state from GET /api/compiles; `page`: the lesson's own status read. */
  source: Source;
  /** Stories only: start with the confirm open. */
  defaultOpen?: boolean;
}

export function CompileControl({ lesson, source, defaultOpen = false }: CompileControlProps) {
  const { api, notify } = useServices();
  const client = useQueryClient();
  const state = useRunState(lesson, source);
  const { run } = state;
  const [open, setOpen] = useState(defaultOpen);
  const [mode, setMode] = useState<'compile' | 'resume'>('compile');
  const [alert, setAlert] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [empty, setEmpty] = useState(false);
  const [focusStatus, setFocusStatus] = useState(false);
  const [, setRefreshed] = useState(0);
  const inFlight = useRef(false);
  const statusRef = useRef<HTMLSpanElement>(null);
  const previous = useRef<CompileStatus | null | undefined>(undefined);
  const invalidatedFor = useRef<string | null>(null);

  const published = lesson.latest_sha256 !== null;
  const known = run !== undefined;
  const described = known ? describeRun(run) : null;
  const openRun = run ? isOpen(run.state) : false;
  const expired =
    state.active &&
    Boolean(run && polls(run.state)) &&
    state.updatedAt > 0 &&
    Date.now() - windowStart(client, state.windowKey) >= COMPILE_POLL_MAX_MS;

  const invalidateLesson = () => {
    void client.invalidateQueries({ queryKey: ['lessons'] });
    void client.invalidateQueries({ queryKey: ['lesson', lesson.lesson_id] });
  };
  const refetchRun = async () => {
    restartWindow(client, state.windowKey);
    await Promise.all([
      client.invalidateQueries({ queryKey: ['compiles'] }),
      client.invalidateQueries({ queryKey: ['compile', lesson.lesson_id] }),
    ]);
  };

  // A newer revision than the row's, or a run that stopped polling (or left the open runs): reload the lesson.
  useEffect(() => {
    if (!known) return;
    const before = previous.current;
    previous.current = run;
    const sha = run?.latest_sha256 ?? null;
    if (sha !== null && sha !== lesson.latest_sha256 && invalidatedFor.current !== sha) {
      invalidatedFor.current = sha;
      invalidateLesson();
      return;
    }
    if (before && polls(before.state) && !(run && polls(run.state))) invalidateLesson();
  }, [run, known]);

  const text = described?.text ?? null;
  useEffect(() => {
    if (focusStatus && !open && text && statusRef.current) {
      statusRef.current.focus();
      setFocusStatus(false);
    }
  }, [focusStatus, open, text]);

  // Published and no open run: nothing beyond FR-UI-6 (the list) or the lesson itself (the page).
  if (published && !openRun) return null;

  const action = described ? described.action : state.failed && !published ? 'compile' : null;
  const fallback = source === 'list' && !published && (!known || state.failed) ? 'not published yet' : null;
  const shownText = text ?? fallback;

  const start = (next: 'compile' | 'resume') => {
    setMode(next);
    setAlert(null);
    setOpen(true);
  };

  const submit = async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setAlert(null);
    try {
      if (mode === 'resume' && run) await api.resumeCompile(run.run_id);
      else await api.startCompile(lesson.lesson_id);
      setOpen(false);
      setFocusStatus(true);
      await refetchRun();
    } catch (e) {
      const outcome = postOutcome(e instanceof ApiError ? e.code : 'unknown');
      if (outcome.kind === 'notice') {
        setOpen(false);
        notify(outcome.code);
        await refetchRun();
      } else {
        setAlert(outcome.text);
        if (outcome.reloadList) void client.invalidateQueries({ queryKey: ['lessons'] });
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };

  const cap = run ? money(run.cap_usd) : RUN_CAP_USD;
  return (
    <span className="compile-control">
      {published && openRun && (
        <Chip.Root size="sm" variant="outline" color="neutral">
          <Chip.Label>{DRAFT_CHIP}</Chip.Label>
        </Chip.Root>
      )}
      {shownText && (
        <span ref={statusRef} tabIndex={-1} className="compile-status">
          {shownText}
        </span>
      )}
      {expired && (
        <Button
          variant="ghost"
          color="neutral"
          size="sm"
          onClick={() => {
            restartWindow(client, state.windowKey);
            setRefreshed((n) => n + 1); // re-render now: the new window is not expired
            void client.refetchQueries({ queryKey: source === 'list' ? ['compiles'] : ['compile', lesson.lesson_id] });
          }}
        >
          Refresh
        </Button>
      )}
      {action && (
        <Button variant="outline" color="neutral" size="sm" onClick={() => start(action === 'retry' ? 'resume' : 'compile')}>
          {action === 'retry' ? 'Retry' : 'Compile'}
        </Button>
      )}
      <Dialog.Root
        open={open}
        onOpenChange={(next) => {
          setOpen(next);
          if (!next) setAlert(null);
        }}
      >
        <Dialog.Portal>
          <Dialog.Backdrop />
          <Dialog.Popup>
            <Dialog.Title>{mode === 'resume' ? `Resume the compile of lesson ${lesson.ordinal}?` : `Compile lesson ${lesson.ordinal}?`}</Dialog.Title>
            <Dialog.Description>{lesson.title}</Dialog.Description>
            {mode === 'resume' && run ? (
              <>
                <p>
                  Spent so far: ${money(run.spent_usd)} of ${cap}.
                </p>
                <p>Stops at ${cap}.</p>
              </>
            ) : (
              <EstimateText lessonId={lesson.lesson_id} onEmpty={setEmpty} />
            )}
            {alert && (
              <p role="alert" className="compile-alert">
                {alert}
              </p>
            )}
            <div className="dialog-actions">
              <Dialog.Close>Cancel</Dialog.Close>
              <Button disabled={busy || (mode === 'compile' && empty)} onClick={() => void submit()}>
                {mode === 'resume' ? 'Resume' : 'Compile'}
              </Button>
            </div>
          </Dialog.Popup>
        </Dialog.Portal>
      </Dialog.Root>
    </span>
  );
}
