// WEB-MANAGE FR-MANAGE-4: lesson actions, on each lesson row of `/` (WEB-UI FR-UI-6's manage slot) and in the lesson
// page's "More lesson actions" menu (WEB-NOTEBOOK paired change). Every reader: done and clear tutor history; the owner
// also: download, rename, move, skip, revisions and revert, compile history, cancel compile and delete.
import { Button } from '@acme/ui/button';
import { Dialog } from '@acme/ui/dialog';
import { useQuery } from '@tanstack/react-query';
import { useRef, useState, type ReactNode, type RefObject } from 'react';
import type { LessonSummary } from '../app/api';
import { useServices } from '../app/context';
import { queries } from '../app/queries';
import { textLang, useLocale } from '../ui/i18n';
import { useManageApi, useOwner, useRefresh } from './hooks';
import { downloadName, positions, problem, type Problem } from './model';
import { ActionsMenu, codeOf, ConfirmDialog, ProblemLine, RenameField, useManageT, type MenuItemSpec } from './primitives';
import { showUndo } from './toast';

type Mode = null | 'rename' | 'delete' | 'clear' | 'cancel' | 'revisions' | 'history';
const OPEN = new Set(['running', 'stalled', 'stage_failed', 'budget_exhausted']);

/** The items and the dialogs of one lesson, for its row or its page. */
export function useLessonManage(lesson: LessonSummary): { items: MenuItemSpec[]; node: ReactNode; trigger: RefObject<HTMLButtonElement | null> } {
  const t = useManageT();
  const api = useManageApi();
  const { api: app } = useServices();
  const owner = useOwner();
  const refresh = useRefresh();
  const list = useQuery(queries(app).lessons());
  const runs = useQuery({ ...queries(app).openCompiles(), enabled: owner });
  const [mode, setMode] = useState<Mode>(null);
  const [issue, setIssue] = useState<Problem | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const row = list.data?.lessons.find((l) => l.lesson_id === lesson.lesson_id);
  const current = { ...lesson, ...row };
  const siblings = (list.data?.lessons ?? []).filter((l) => l.course_id === current.course_id).sort((a, b) => a.ordinal - b.ordinal);
  const index = siblings.findIndex((l) => l.lesson_id === lesson.lesson_id);
  const position = positions(list.data?.lessons ?? [current]).get(lesson.lesson_id) ?? 1;
  const openRun = (runs.data?.runs ?? []).find((r) => r.lesson_id === lesson.lesson_id && OPEN.has(r.state));
  const sha = current.latest_sha256;
  const done = current.done === true;
  const skipped = current.skipped === true;
  const run = async (f: () => Promise<unknown>, object: 'lesson' | 'compile' = 'lesson') => {
    setIssue(null);
    try {
      await f();
      await refresh();
    } catch (e) {
      const p = problem(codeOf(e), object, t);
      setIssue(p);
      if (p.refetch) void refresh();
    }
  };
  const move = (delta: number) => {
    const ids = siblings.map((l) => l.lesson_id);
    const [it] = ids.splice(index, 1);
    ids.splice(index + delta, 0, it!);
    void run(() => api.orderLessons(current.course_id, ids));
  };
  const items: MenuItemSpec[] = [
    { label: done ? t('lesson.not_done') : t('lesson.done'), onSelect: () => void run(() => api.progress(lesson.lesson_id, !done)) },
    { label: t('lesson.clear'), onSelect: () => setMode('clear') },
    ...(owner
      ? [
          {
            label: t('lesson.download'),
            href: sha ? `/api/lessons/${encodeURIComponent(lesson.lesson_id)}/revisions/${sha}` : '',
            download: downloadName(position, current.title),
            hidden: !sha,
          },
          { label: t('rename'), onSelect: () => setMode('rename') },
          { label: t('move_up'), onSelect: () => move(-1), hidden: index <= 0 },
          { label: t('move_down'), onSelect: () => move(1), hidden: index < 0 || index === siblings.length - 1 },
          { label: skipped ? t('lesson.unskip') : t('lesson.skip'), onSelect: () => void run(() => api.skip(lesson.lesson_id, !skipped)) },
          { label: t('lesson.revisions'), onSelect: () => setMode('revisions'), hidden: !sha },
          { label: t('lesson.history'), onSelect: () => setMode('history') },
          { label: t('lesson.cancel'), onSelect: () => setMode('cancel'), hidden: !openRun },
          { label: t('delete_ellipsis'), onSelect: () => setMode('delete'), danger: true },
        ]
      : []),
  ];
  const close = () => setMode(null);
  const node = (
    <>
      {mode === 'rename' ? (
        <RenameField
          value={current.title}
          label={t('lesson.title_label')}
          hint={t('lesson.rename_hint')}
          object="lesson"
          returnFocus={trigger}
          onCancel={close}
          onSave={async (title) => {
            await api.renameLesson(lesson.lesson_id, current.version ?? 1, title);
            close();
            await refresh();
          }}
        />
      ) : null}
      <ProblemLine problem={issue} />
      {mode === 'clear' ? (
        <ConfirmDialog
          open
          title={t('clear.title', { title: current.title })}
          confirmLabel={t('clear.confirm')}
          onClose={close}
          onConfirm={async () => {
            close();
            await run(() => api.clearTutor(lesson.lesson_id));
          }}
        >
          <p>{t('clear.body')}</p>
        </ConfirmDialog>
      ) : null}
      {mode === 'cancel' && openRun ? (
        <ConfirmDialog
          open
          title={t('cancel.title', { title: current.title })}
          confirmLabel={t('cancel.confirm')}
          onClose={close}
          onConfirm={async () => {
            close();
            await run(() => api.cancelRun(openRun.run_id), 'compile');
          }}
        >
          <p>{t('cancel.body')}</p>
        </ConfirmDialog>
      ) : null}
      {mode === 'delete' ? (
        <ConfirmDialog
          open
          title={t('lesson.delete_title', { title: current.title })}
          confirmLabel={t('delete')}
          onClose={close}
          onConfirm={async () => {
            try {
              await api.deleteLesson(lesson.lesson_id);
              close();
              await refresh();
              showUndo({
                text: t('toast.lesson.deleted'),
                restoredText: t('toast.lesson.restored'),
                onUndo: async () => {
                  await api.restoreLesson(lesson.lesson_id);
                  await refresh();
                },
              });
            } catch (e) {
              close();
              setIssue(problem(codeOf(e), 'lesson', t));
            }
          }}
        >
          <p>{t('lesson.delete_body')}</p>
        </ConfirmDialog>
      ) : null}
      {mode === 'revisions' ? <RevisionsDialog lesson={current} onClose={close} /> : null}
      {mode === 'history' ? <HistoryDialog lesson={current} onClose={close} /> : null}
    </>
  );
  return { items, node, trigger };
}

/** The row's menu (FR-MANAGE-4), in FR-UI-6's manage slot. */
export function LessonActions({ lesson }: { lesson: LessonSummary }) {
  const { items, node, trigger } = useLessonManage(lesson);
  return (
    <span className="lesson-actions">
      <ActionsMenu label={lesson.title} items={items} triggerRef={trigger} />
      {node}
    </span>
  );
}

function RevisionsDialog({ lesson, onClose }: { lesson: LessonSummary; onClose: () => void }) {
  const t = useManageT();
  const api = useManageApi();
  const { api: app } = useServices();
  const { formatLocale } = useLocale();
  const refresh = useRefresh();
  const detail = useQuery(queries(app).lesson(lesson.lesson_id));
  const [asking, setAsking] = useState<string | null>(null);
  const [issue, setIssue] = useState<Problem | null>(null);
  const [done, setDone] = useState(false);
  const current = detail.data?.lesson.latest_sha256 ?? lesson.latest_sha256;
  const revisions = (detail.data?.revisions ?? []).slice(0, 100);
  const revert = async (sha: string) => {
    setIssue(null);
    try {
      await api.revert(lesson.lesson_id, sha);
      setAsking(null);
      setDone(true);
      await refresh();
    } catch (e) {
      const code = codeOf(e);
      setIssue(code === 'notes_cell_removed' ? { text: t('revert.notes_removed') } : problem(code, 'lesson', t));
    }
  };
  return (
    <Dialog.Root open onOpenChange={(o) => (o ? undefined : onClose())}>
      <Dialog.Portal>
        <Dialog.Backdrop />
        <Dialog.Popup className="manage-dialog">
          <Dialog.Title>{t('revisions.title')}</Dialog.Title>
          <Dialog.Description lang={textLang(lesson.title)}>{lesson.title}</Dialog.Description>
          {detail.isPending ? <p role="status">{t('loading')}</p> : null}
          {revisions.length === 0 && !detail.isPending ? <p className="muted">{t('revisions.none')}</p> : null}
          <ul role="list" className="manage-list">
            {revisions.map((r) => (
              <li key={r.sha256} className="manage-row">
                <span>
                  {t('revisions.row', {
                    date: new Date(r.created_at).toLocaleString(formatLocale, { dateStyle: 'medium', timeStyle: 'short' }),
                    stages: r.stages.join(', '),
                    cells: String(r.cell_count),
                  })}
                </span>
                {r.sha256 === current ? (
                  <span className="chip">{t('revisions.current')}</span>
                ) : (
                  <Button variant="outline" color="neutral" size="sm" onClick={() => setAsking(r.sha256)}>
                    {t('revisions.revert')}
                  </Button>
                )}
              </li>
            ))}
          </ul>
          {asking ? (
            <div className="manage-confirm" role="group" aria-label={t('revert.title')}>
              <p>{t('revert.body')}</p>
              <div className="dialog-actions">
                <Button variant="outline" color="neutral" onClick={() => setAsking(null)}>
                  {t('cancel')}
                </Button>
                <Button onClick={() => void revert(asking)}>{t('revert.confirm')}</Button>
              </div>
            </div>
          ) : null}
          {done ? <p role="status">{t('revert.done')}</p> : null}
          <ProblemLine problem={issue} />
          <div className="dialog-actions">
            <Dialog.Close>{t('close')}</Dialog.Close>
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function HistoryDialog({ lesson, onClose }: { lesson: LessonSummary; onClose: () => void }) {
  const t = useManageT();
  const api = useManageApi();
  const { formatLocale } = useLocale();
  const history = useQuery({ queryKey: ['manage', 'history', lesson.lesson_id], queryFn: () => api.history(lesson.lesson_id) });
  const when = (iso: string) => new Date(iso).toLocaleString(formatLocale, { dateStyle: 'medium', timeStyle: 'short' });
  const duration = (a: string, b: string) => {
    const s = Math.max(0, Math.round((Date.parse(b) - Date.parse(a)) / 1000));
    return t('history.duration', { m: String(Math.floor(s / 60)), s: String(s % 60) });
  };
  const runs = history.data?.runs ?? [];
  return (
    <Dialog.Root open onOpenChange={(o) => (o ? undefined : onClose())}>
      <Dialog.Portal>
        <Dialog.Backdrop />
        <Dialog.Popup className="manage-dialog">
          <Dialog.Title>{t('history.title')}</Dialog.Title>
          <Dialog.Description lang={textLang(lesson.title)}>{lesson.title}</Dialog.Description>
          {history.isPending ? <p role="status">{t('loading')}</p> : null}
          {history.isError ? <ProblemLine problem={problem(codeOf(history.error), 'lesson', t)} /> : null}
          {history.isSuccess && runs.length === 0 ? <p className="muted">{t('history.none')}</p> : null}
          <ul role="list" className="manage-list">
            {runs.map((r) => (
              <li key={r.run_id} className="manage-row">
                {r.finished_at
                  ? t('history.row', { state: t.enum('run_state', r.state), spent: r.spent_usd, cap: r.cap_usd, duration: duration(r.created_at, r.finished_at), finished: when(r.finished_at) })
                  : t('history.row_open', { state: t.enum('run_state', r.state), spent: r.spent_usd, cap: r.cap_usd, started: when(r.created_at) })}
              </li>
            ))}
          </ul>
          <div className="dialog-actions">
            <Dialog.Close>{t('close')}</Dialog.Close>
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export default LessonActions;
