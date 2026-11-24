// WEB-MANAGE FR-MANAGE-3: the owner's course actions on each course group header of `/` (WEB-UI FR-UI-6's manage
// slot) and the closed "Archived (n)" disclosure at the end of the list. Every request follows a click on its own
// control and, for archive and delete, Undo is offered (FR-MANAGE-1, INV-MANAGE-4).
import { Button } from '@acme/ui/button';
import { Dialog } from '@acme/ui/dialog';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useId, useRef, useState } from 'react';
import { createPlanApi, type Preview } from '../import/plan/api';
import { useServices } from '../app/context';
import { textLang, useLocale } from '../ui/i18n';
import type { Course } from './api';
import { useCourses, useManageApi, useOwner, useRefresh } from './hooks';
import { problem, weeksFor, type Problem } from './model';
import { ActionsMenu, codeOf, ConfirmDialog, ProblemLine, RenameField, useManageT, type MenuItemSpec } from './primitives';
import { showUndo } from './toast';

type Mode = null | 'rename' | 'describe' | 'dates' | 'delete';

const nfcLen = (s: string) => [...s].length;

export function CourseActions({ courseId, title }: { courseId: string; title: string }) {
  const t = useManageT();
  const api = useManageApi();
  const refresh = useRefresh();
  const owner = useOwner();
  const courses = useCourses('active', owner);
  const [mode, setMode] = useState<Mode>(null);
  const [issue, setIssue] = useState<Problem | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  if (!owner) return null;
  const list = courses.data?.courses ?? [];
  const index = list.findIndex((c) => c.course_id === courseId);
  const course = index >= 0 ? list[index] : undefined;
  const fail = (e: unknown) => {
    const p = problem(codeOf(e), 'course', t);
    setIssue(p);
    if (p.refetch) void refresh();
  };
  const move = async (delta: number) => {
    const ids = list.map((c) => c.course_id);
    const [it] = ids.splice(index, 1);
    ids.splice(index + delta, 0, it!);
    try {
      await api.orderCourses(ids);
      await refresh();
    } catch (e) {
      fail(e);
    }
  };
  const archive = async () => {
    setIssue(null);
    try {
      await api.archive(courseId);
      await refresh();
      showUndo({
        text: t('toast.course.archived'),
        restoredText: t('toast.course.restored'),
        onUndo: async () => {
          await api.unarchive(courseId, 'previous');
          await refresh();
        },
      });
    } catch (e) {
      fail(e);
    }
  };
  const items: MenuItemSpec[] = [
    { label: t('rename'), onSelect: () => setMode('rename') },
    { label: t('course.describe'), onSelect: () => setMode('describe') },
    { label: t('course.dates'), onSelect: () => setMode('dates'), hidden: !course?.plan },
    { label: t('move_up'), onSelect: () => void move(-1), hidden: !course || index === 0 },
    { label: t('move_down'), onSelect: () => void move(1), hidden: !course || index === list.length - 1 },
    { label: t('course.archive'), onSelect: () => void archive() },
    { label: t('delete_ellipsis'), onSelect: () => setMode('delete'), danger: true },
  ];
  return (
    <span className="course-actions">
      <ActionsMenu label={title} items={items} triggerRef={trigger} />
      {mode === 'rename' ? (
        <RenameField
          value={course?.title ?? title}
          label={t('course.title_label')}
          hint={t('course.rename_hint')}
          returnFocus={trigger}
          onCancel={() => setMode(null)}
          onSave={async (next) => {
            await api.editCourse(courseId, { version: course?.version ?? 1, title: next });
            setMode(null);
            await refresh();
          }}
        />
      ) : null}
      <ProblemLine problem={mode === null ? issue : null} />
      {mode === 'describe' && course ? <DescribeDialog course={course} onClose={() => setMode(null)} /> : null}
      {mode === 'dates' && course ? <DatesDialog course={course} onClose={() => setMode(null)} /> : null}
      {mode === 'delete' ? <DeleteCourseDialog courseId={courseId} title={course?.title ?? title} onClose={() => setMode(null)} /> : null}
    </span>
  );
}

function DescribeDialog({ course, onClose }: { course: Course; onClose: () => void }) {
  const t = useManageT();
  const api = useManageApi();
  const refresh = useRefresh();
  const [text, setText] = useState(course.description ?? '');
  const [issue, setIssue] = useState<Problem | null>(null);
  const id = useId();
  const save = async () => {
    const value = text.trim();
    if (nfcLen(value) > 2000) {
      setIssue({ text: t('course.description_invalid') });
      return;
    }
    if (value === (course.description ?? '')) return onClose();
    try {
      await api.editCourse(course.course_id, { version: course.version, description: value });
      await refresh();
      onClose();
    } catch (e) {
      setIssue(problem(codeOf(e), 'course', t));
    }
  };
  return (
    <Dialog.Root open onOpenChange={(o) => (o ? undefined : onClose())}>
      <Dialog.Portal>
        <Dialog.Backdrop />
        <Dialog.Popup className="manage-dialog">
          <Dialog.Title>{t('course.description_title', { title: course.title })}</Dialog.Title>
          <p className="manage-field">
            <label htmlFor={id}>{t('course.description')}</label>
            <textarea id={id} className="manage-input" rows={6} maxLength={2000} lang={textLang(text)} value={text} onChange={(e) => setText(e.target.value)} />
          </p>
          <ProblemLine problem={issue} />
          <div className="dialog-actions">
            <Dialog.Close>{t('cancel')}</Dialog.Close>
            <Button onClick={() => void save()}>{t('save')}</Button>
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

const today = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
};

function DatesDialog({ course, onClose }: { course: Course; onClose: () => void }) {
  const t = useManageT();
  const api = useManageApi();
  const { http } = useServices();
  const { formatLocale } = useLocale();
  const refresh = useRefresh();
  const plan = useQuery({ queryKey: ['manage', 'plan', course.course_id], queryFn: () => api.storedPlan(course.course_id), retry: false });
  const [weeks, setWeeks] = useState<string | null>(null);
  const [minutes, setMinutes] = useState<string | null>(null);
  const [target, setTarget] = useState('');
  const [preview, setPreview] = useState<Preview | null>(null);
  const [issue, setIssue] = useState<{ p: Problem; restore?: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const planApi = createPlanApi(http ?? { fetch: (...a) => globalThis.fetch(...a), onUnauthorized: () => undefined });
  const inputs = plan.data?.inputs;
  const w = weeks ?? (inputs ? String(inputs.total_weeks) : '');
  const m = minutes ?? (inputs ? String(inputs.minutes_per_day) : '');
  const ids = { weeks: useId(), minutes: useId(), target: useId() };
  const fail = (e: unknown) => {
    const code = codeOf(e);
    if (code === 'course_deleted') setIssue({ p: { text: t('dates.course_deleted') }, restore: true });
    else if (code === 'course_archived') setIssue({ p: { text: t('dates.archived') } });
    else setIssue({ p: problem(code, 'course', t) });
  };
  useEffect(() => {
    if (!inputs || weeks === null) return;
    const tw = Number(w);
    const mp = Number(m);
    if (!Number.isInteger(tw) || tw < 1 || tw > 104 || !Number.isInteger(mp)) return;
    let live = true;
    setPreview(null);
    const timer = window.setTimeout(() => {
      planApi
        .preview({ material_id: inputs.material_id, total_weeks: tw, minutes_per_day: mp, rate: inputs.rate, today: today() })
        .then((p) => live && setPreview(p))
        .catch((e) => live && fail(e));
    }, 250);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
    // the client is rebuilt per render; the inputs drive the preview
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [w, m, inputs, weeks]);
  const apply = async () => {
    if (!preview || !inputs) return;
    setBusy(true);
    try {
      await planApi.confirm({
        material_id: inputs.material_id,
        total_weeks: preview.inputs.total_weeks,
        minutes_per_day: preview.inputs.minutes_per_day,
        rate: preview.inputs.rate,
        today: preview.inputs.today,
        plan_sha256: preview.plan_sha256,
        compile_first: false,
      });
      await refresh();
      onClose();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };
  const date = (iso: string | null | undefined) => (iso ? new Date(`${iso}T00:00:00`).toLocaleDateString(formatLocale, { dateStyle: 'medium' }) : '');
  return (
    <Dialog.Root open onOpenChange={(o) => (o ? undefined : onClose())}>
      <Dialog.Portal>
        <Dialog.Backdrop />
        <Dialog.Popup className="manage-dialog">
          <Dialog.Title>{t('dates.title', { title: course.title })}</Dialog.Title>
          <p>{t('dates.note')}</p>
          {inputs ? (
            <div className="manage-fields">
              <p className="manage-field">
                <label htmlFor={ids.weeks}>{t('dates.weeks')}</label>
                <input id={ids.weeks} className="manage-input" type="number" min={1} max={104} step={1} value={w} onChange={(e) => setWeeks(e.target.value)} />
              </p>
              <p className="manage-field">
                <label htmlFor={ids.minutes}>{t('dates.minutes')}</label>
                <input
                  id={ids.minutes}
                  className="manage-input"
                  type="number"
                  min={10}
                  max={240}
                  step={5}
                  value={m}
                  onChange={(e) => {
                    setMinutes(e.target.value);
                    if (weeks === null) setWeeks(w);
                  }}
                />
              </p>
              <p className="manage-field">
                <label htmlFor={ids.target}>{t('dates.target')}</label>
                <input
                  id={ids.target}
                  className="manage-input"
                  type="date"
                  value={target}
                  onChange={(e) => {
                    setTarget(e.target.value);
                    if (e.target.value) setWeeks(String(weeksFor(inputs.start_date, e.target.value)));
                  }}
                />
              </p>
            </div>
          ) : plan.isError ? (
            <ProblemLine problem={problem(codeOf(plan.error), 'course', t)} />
          ) : (
            <p role="status">{t('loading')}</p>
          )}
          {preview ? (
            <p role="status">
              {preview.summary.finish_by ? `${t('dates.preview', { date: date(preview.summary.finish_by) })} ` : ''}
              {t('dates.lessons', { count: preview.summary.lesson_count })}
              {preview.summary.fits ? '' : ` ${t('dates.unfit')}`}
            </p>
          ) : null}
          {issue ? (
            <ProblemLine problem={issue.p}>
              {issue.restore ? (
                <Button
                  variant="outline"
                  color="neutral"
                  size="sm"
                  onClick={() => void api.restoreCourse(course.course_id, 'first').then(refresh, fail)}
                >
                  {t('dates.restore_course')}
                </Button>
              ) : null}
            </ProblemLine>
          ) : null}
          <div className="dialog-actions">
            <Dialog.Close>{t('cancel')}</Dialog.Close>
            <Button disabled={!preview || !preview.summary.fits || busy} onClick={() => void apply()}>
              {t('dates.apply')}
            </Button>
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export function DeleteCourseDialog({ courseId, title, onClose }: { courseId: string; title: string; onClose: () => void }) {
  const t = useManageT();
  const api = useManageApi();
  const { api: app } = useServices();
  const refresh = useRefresh();
  // FR-MANAGE-2: "Cancel compile" cancels the course's open runs (SRV-PIPELINE FR-PIPELINE-20), then the delete can
  // be confirmed again.
  const cancelRuns = async () => {
    try {
      const [{ runs }, { lessons }] = await Promise.all([app.openCompiles(), app.listLessons()]);
      const ours = new Set(lessons.filter((l) => l.course_id === courseId).map((l) => l.lesson_id));
      await Promise.all(runs.filter((r) => ours.has(r.lesson_id)).map((r) => api.cancelRun(r.run_id)));
      setRunning(false);
      setAlert(null);
      await refresh();
    } catch (e) {
      setAlert(problem(codeOf(e), 'compile', t));
    }
  };
  const impact = useQuery({ queryKey: ['manage', 'impact', courseId], queryFn: () => api.courseImpact(courseId), retry: false });
  const [alert, setAlert] = useState<Problem | null>(null);
  const [running, setRunning] = useState(false);
  const i = impact.data;
  return (
    <ConfirmDialog
      open
      title={t('course.delete_title', { title })}
      confirmLabel={t('delete')}
      typed={title}
      alert={alert}
      onClose={onClose}
      extra={
        running ? (
          <Button variant="outline" color="neutral" onClick={() => void cancelRuns()}>
            {t('course.cancel_compile')}
          </Button>
        ) : null
      }
      onConfirm={async (confirm) => {
        setAlert(null);
        try {
          await api.deleteCourse(courseId, confirm ?? '');
          onClose();
          await refresh();
          showUndo({
            text: t('toast.course.deleted'),
            restoredText: t('toast.course.restored'),
            onUndo: async () => {
              await api.restoreCourse(courseId, 'previous');
              await refresh();
            },
          });
        } catch (e) {
          const code = codeOf(e);
          setRunning(code === 'compile_running');
          setAlert(problem(code, 'course', t));
        }
      }}
    >
      {i ? (
        <p>
          {t('course.impact_line', { lessons: i.lessons, revisions: i.revisions, questions: i.tutor_questions, runs: i.compile_runs })}{' '}
          {t('course.stays')}
        </p>
      ) : (
        <p>{t('course.stays')}</p>
      )}
    </ConfirmDialog>
  );
}

/** The closed "Archived (n)" disclosure at the end of `/` (owner only). */
export function ArchivedCourses() {
  const t = useManageT();
  const api = useManageApi();
  const refresh = useRefresh();
  const owner = useOwner();
  const archived = useCourses('archived', owner);
  const [open, setOpen] = useState(false);
  const [deleting, setDeleting] = useState<Course | null>(null);
  const [issue, setIssue] = useState<Problem | null>(null);
  const listId = useId();
  const courses = archived.data?.courses ?? [];
  if (!owner || courses.length === 0) return null;
  return (
    <section className="archived-courses" aria-label={t('archived.toggle', { count: courses.length })}>
      <Button variant="ghost" color="neutral" size="sm" aria-expanded={open} aria-controls={listId} onClick={() => setOpen((o) => !o)}>
        {t('archived.toggle', { count: courses.length })}
      </Button>
      <ul id={listId} role="list" hidden={!open}>
        {open
          ? courses.map((c) => (
              <li key={c.course_id} className="archived-row">
                <span lang={textLang(c.title)}>{c.title}</span>
                <span className="row-buttons">
                  <Button
                    variant="outline"
                    color="neutral"
                    size="sm"
                    onClick={() =>
                      void api.unarchive(c.course_id, 'first').then(refresh, (e: unknown) => setIssue(problem(codeOf(e), 'course', t)))
                    }
                  >
                    {t('archived.unarchive')}
                  </Button>
                  <Button variant="ghost" color="neutral" size="sm" onClick={() => setDeleting(c)}>
                    {t('delete_ellipsis')}
                  </Button>
                </span>
              </li>
            ))
          : null}
      </ul>
      <ProblemLine problem={issue} />
      {deleting ? <DeleteCourseDialog courseId={deleting.course_id} title={deleting.title} onClose={() => setDeleting(null)} /> : null}
    </section>
  );
}
