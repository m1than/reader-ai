// The lesson route (WEB-NOTEBOOK FR-NOTEBOOK-1, FR-NOTEBOOK-12): metadata first, then the immutable latest revision;
// one kernel controller per open view, disposed when the view closes (FR-NOTEBOOK-4). Breadcrumbs, the not-published
// page with the compile slot and the published neighbours, and state blocks for errors (design pass). Rendered inside
// the shell's single `main` (WEB-UI FR-UI-11).
import { Breadcrumbs } from '@acme/ui/breadcrumbs';
import { Button } from '@acme/ui/button';
import { Skeleton } from '@acme/ui/skeleton';
import { useQuery } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { useEffect, useMemo, useState } from 'react';
import { useOwner } from '../manage/hooks';
import { useLessonManage } from '../manage/LessonActions';
import { BookmarkToggle, BookmarksList, NotesPanel, NotesToggle } from '../manage/Notes';
import { MenuItems, shownItems } from '../manage/primitives';
import { ApiError, SIGN_IN_URL, type LessonSummary } from '../app/api';
import { useServices } from '../app/context';
import { queries } from '../app/queries';
import { createTutorApi } from '../tutor/api';
import { AskPanel } from '../tutor/AskPanel';
import { textLang, useT } from '../ui/i18n';
import { SearchOffIcon, WarningIcon } from '../ui/icons';
import { StateBlock } from '../ui/StateBlock';
import { browserSocketFactory, createKernelController } from './kernel';
import { LessonView, type Notebook, type PanelState } from './LessonView';
import en from './messages.en';

/** FR-NOTEBOOK-11: Lessons › course › lesson (current page). Titles get lang from their own text (WEB-UI FR-UI-32). */
function Crumbs({ lesson }: { lesson: LessonSummary }) {
  const t = useT('notebook', en);
  return (
    <Breadcrumbs.Root aria-label={t('crumbs.label')} className="crumbs">
      <Breadcrumbs.List>
        <Breadcrumbs.Item>
          <Breadcrumbs.Link render={<Link to="/" />}>{t('crumbs.lessons')}</Breadcrumbs.Link>
        </Breadcrumbs.Item>
        <Breadcrumbs.Item>
          <Breadcrumbs.Link render={<Link to="/" hash={`course-${lesson.course_id}`} lang={textLang(lesson.course_title)} />}>
            {lesson.course_title}
          </Breadcrumbs.Link>
        </Breadcrumbs.Item>
        <Breadcrumbs.Item>
          <Breadcrumbs.Page lang={textLang(lesson.title)}>{lesson.title}</Breadcrumbs.Page>
        </Breadcrumbs.Item>
      </Breadcrumbs.List>
    </Breadcrumbs.Root>
  );
}

/** FR-NOTEBOOK-12: a status with hidden text (no aria-busy: it would silence the status) and the library skeleton. */
function LessonSkeleton() {
  const t = useT('notebook', en);
  return (
    <div className="page">
      <p role="status" className="visually-hidden">
        {t('page.loading')}
      </p>
      <div aria-hidden="true">
        <Skeleton.Root>
          <Skeleton.Item variant="text" width="50%" height="2.25rem" />
          <Skeleton.Item variant="text" width="100%" />
          <Skeleton.Item variant="text" width="95%" />
          <Skeleton.Item variant="text" width="70%" />
        </Skeleton.Root>
      </div>
    </div>
  );
}

const Failed = ({ error, retry }: { error: unknown; retry: () => void }) => {
  const t = useT('notebook', en);
  const missing = error instanceof ApiError && error.status === 404;
  return (
    <div className="page state">
      <StateBlock
        tone={missing ? 'neutral' : 'danger'}
        icon={missing ? <SearchOffIcon size={20} /> : <WarningIcon size={20} />}
        title={missing ? t('page.not_found') : t('page.failed')}
        actions={
          <>
            {!missing && (
              <Button variant="outline" color="neutral" onClick={retry}>
                {t('page.retry')}
              </Button>
            )}
            <Link to="/" className="state-link">
              {t('page.back')}
            </Link>
          </>
        }
      >
        <p className="muted">{missing ? t('page.not_found_hint') : t('page.failed_hint')}</p>
      </StateBlock>
    </div>
  );
};

/** FR-NOTEBOOK-1 (design pass): the not-published page, with the compile slot and the nearest published lessons. */
function NotPublished({ lesson }: { lesson: LessonSummary }) {
  const t = useT('notebook', en);
  const { api, compileSlot } = useServices();
  const list = useQuery(queries(api).lessons());
  const Status = compileSlot?.Status;
  const Action = compileSlot?.Action;
  const chip = <span className="chip">{t('unpublished.chip')}</span>;
  const siblings = (list.data?.lessons ?? []).filter((l) => l.course_id === lesson.course_id && l.latest_sha256 !== null);
  const previous = siblings.filter((l) => l.ordinal < lesson.ordinal).sort((a, b) => b.ordinal - a.ordinal)[0];
  const next = siblings.filter((l) => l.ordinal > lesson.ordinal).sort((a, b) => a.ordinal - b.ordinal)[0];
  return (
    <div className="page">
      <Crumbs lesson={lesson} />
      <StateBlock
        title={lesson.title}
        actions={
          <>
            {previous ? (
              <Link to="/lessons/$lessonId" params={{ lessonId: previous.lesson_id }} className="state-link">
                {t('unpublished.previous', { title: previous.title })}
              </Link>
            ) : null}
            {next ? (
              <Link to="/lessons/$lessonId" params={{ lessonId: next.lesson_id }} className="state-link">
                {t('unpublished.next', { title: next.title })}
              </Link>
            ) : null}
            <Link to="/" hash={`course-${lesson.course_id}`} className="state-link">
              {t('unpublished.all', { course: lesson.course_title })}
            </Link>
          </>
        }
      >
        <p className="muted">{t('unpublished.text', { n: String(lesson.ordinal), course: lesson.course_title })}</p>
        {/* WEB-UI FR-UI-6: a div (the slot's parts may be block or interactive); the default chip shows while the
            slot's Status renders nothing. */}
        <div className="state-status">
          {Status ? (
            <span className="lesson-status">
              <span className="slot-status">
                <Status lesson={lesson} place="page" />
              </span>
              <span className="status-default">{chip}</span>
            </span>
          ) : (
            chip
          )}
          {Action ? <Action lesson={lesson} place="page" /> : null}
        </div>
      </StateBlock>
    </div>
  );
}

function Lesson({ lessonId, lesson, notebook, revision }: { lessonId: string; lesson: LessonSummary; notebook: Notebook; revision: string }) {
  const { api, notify, navigateAway, tutor: givenTutor, http, compileSlot, manageSlot } = useServices();
  // NFR-UI-1: the tutor client is built in this lazy chunk from the shell's fetch and 401 hook.
  const tutor = useMemo(() => givenTutor ?? (http ? createTutorApi(http) : undefined), [givenTutor, http]);
  const Status = compileSlot?.Status;
  const controller = useMemo(
    () =>
      createKernelController({
        lessonId,
        api,
        openSocket: browserSocketFactory(window.location.host),
        schedule: (fn, ms) => {
          window.setTimeout(fn, ms);
        },
        navigateToSignIn: () => navigateAway(SIGN_IN_URL),
        notify,
        newMsgId: () => crypto.randomUUID().replaceAll('-', ''),
        now: () => new Date().toISOString(),
      }),
    [lessonId, api, notify, navigateAway],
  );
  useEffect(() => () => controller.dispose(), [controller]);
  // SRV-TUTOR FR-TUTOR-11: the side panel, bound to the revision on screen and the focused cell.
  const aside = tutor
    ? (cellId: string | null, cellLabel: string | null, panel: PanelState) => (
        <AskPanel lessonId={lessonId} revision={revision} cellId={cellId} cellLabel={cellLabel} api={tutor} open={panel.open} onClose={panel.close} />
      )
    : undefined;
  return (
    <div className="page lesson-page">
      {/* FR-NOTEBOOK-12: the owner's compile state above a published draft whose run is open (the slot's Status
          renders nothing otherwise, and app.css hides the empty line). */}
      {Status ? (
        <div className="compile-line">
          <Status lesson={lesson} place="page" />
        </div>
      ) : null}
      {manageSlot ? (
        <ManagedLessonView lessonId={lessonId} lesson={lesson} notebook={notebook} controller={controller} aside={aside} crumbs={<Crumbs lesson={lesson} />} />
      ) : (
        <LessonView notebook={notebook} controller={controller} aside={aside} crumbs={<Crumbs lesson={lesson} />} />
      )}
    </div>
  );
}

/** WEB-MANAGE FR-MANAGE-4 and -9 (paired change): the lesson items in "More lesson actions", the bookmark toggles,
 * the contents' bookmarks and the notes panel. */
function ManagedLessonView({
  lessonId,
  lesson,
  ...view
}: { lessonId: string; lesson: LessonSummary } & Pick<Parameters<typeof LessonView>[0], 'notebook' | 'controller' | 'aside' | 'crumbs'>) {
  const manage = useLessonManage(lesson);
  const owner = useOwner();
  const [notes, setNotes] = useState(false);
  return (
    <>
      <LessonView
        {...view}
        extras={{
          menu: <MenuItems items={shownItems(manage.items, owner)} separatorFirst />,
          cell: (cellId) => <BookmarkToggle lessonId={lessonId} cellId={cellId} />,
          contents: (labels) => <BookmarksList lessonId={lessonId} labels={labels} />,
          toolbar: <NotesToggle open={notes} onToggle={() => setNotes((o) => !o)} />,
          after: (labels) => (notes ? <NotesPanel lessonId={lessonId} labels={labels} onClose={() => setNotes(false)} /> : null),
        }}
      />
      {manage.node}
    </>
  );
}

export function LessonPage({ lessonId }: { lessonId: string }) {
  const { api } = useServices();
  const q = queries(api);
  const meta = useQuery(q.lesson(lessonId));
  const sha = meta.data?.lesson.latest_sha256 ?? null;
  const rev = useQuery({ ...q.revision(lessonId, sha ?? ''), enabled: sha !== null });
  if (meta.isPending) return <LessonSkeleton />;
  if (meta.isError) return <Failed error={meta.error} retry={() => void meta.refetch()} />;
  if (sha === null) return <NotPublished lesson={meta.data.lesson} />;
  if (rev.isPending) return <LessonSkeleton />;
  if (rev.isError) return <Failed error={rev.error} retry={() => void rev.refetch()} />;
  return <Lesson lessonId={lessonId} lesson={meta.data.lesson} notebook={rev.data as unknown as Notebook} revision={sha} />;
}
