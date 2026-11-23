// The lesson route (WEB-NOTEBOOK FR-NOTEBOOK-1, FR-NOTEBOOK-12): metadata first, then the immutable latest revision;
// one kernel controller per open view, disposed when the view closes (FR-NOTEBOOK-4). Breadcrumbs, the not-published
// page with the compile slot and the published neighbours, and state blocks for errors (design pass). Rendered inside
// the shell's single `main` (WEB-UI FR-UI-11).
import { Breadcrumbs } from '@acme/ui/breadcrumbs';
import { Button } from '@acme/ui/button';
import { Skeleton } from '@acme/ui/skeleton';
import { useQuery } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { useEffect, useMemo } from 'react';
import { ApiError, SIGN_IN_URL, type LessonSummary } from '../app/api';
import { useServices } from '../app/context';
import { queries } from '../app/queries';
import { AskPanel } from '../tutor/AskPanel';
import { SearchOffIcon, WarningIcon } from '../ui/icons';
import { StateBlock } from '../ui/StateBlock';
import { browserSocketFactory, createKernelController } from './kernel';
import { LessonView, type Notebook, type PanelState } from './LessonView';

/** FR-NOTEBOOK-11: Lessons › course › lesson (current page). */
function Crumbs({ lesson }: { lesson: LessonSummary }) {
  return (
    <Breadcrumbs.Root aria-label="Breadcrumb" className="crumbs">
      <Breadcrumbs.List>
        <Breadcrumbs.Item>
          <Breadcrumbs.Link render={<Link to="/" />}>Lessons</Breadcrumbs.Link>
        </Breadcrumbs.Item>
        <Breadcrumbs.Item>
          <Breadcrumbs.Link render={<Link to="/" hash={`course-${lesson.course_id}`} />}>{lesson.course_title}</Breadcrumbs.Link>
        </Breadcrumbs.Item>
        <Breadcrumbs.Item>
          <Breadcrumbs.Page>{lesson.title}</Breadcrumbs.Page>
        </Breadcrumbs.Item>
      </Breadcrumbs.List>
    </Breadcrumbs.Root>
  );
}

/** FR-NOTEBOOK-12: a status with hidden text (no aria-busy: it would silence the status) and the library skeleton. */
const LessonSkeleton = () => (
  <div className="page">
    <p role="status" className="visually-hidden">
      Loading the lesson
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

const Failed = ({ error, retry }: { error: unknown; retry: () => void }) => {
  const missing = error instanceof ApiError && error.status === 404;
  return (
    <div className="page state">
      <StateBlock
        tone={missing ? 'neutral' : 'danger'}
        icon={missing ? <SearchOffIcon size={20} /> : <WarningIcon size={20} />}
        title={missing ? 'Lesson not found' : 'The lesson could not be loaded.'}
        actions={
          <>
            {!missing && (
              <Button variant="outline" color="neutral" onClick={retry}>
                Retry
              </Button>
            )}
            <Link to="/" className="state-link">
              Back to lessons
            </Link>
          </>
        }
      >
        <p className="muted">{missing ? 'The lesson may have been removed or the link is wrong.' : 'Check the connection, then retry.'}</p>
      </StateBlock>
    </div>
  );
};

/** FR-NOTEBOOK-1 (design pass): the not-published page, with the compile slot and the nearest published lessons. */
function NotPublished({ lesson }: { lesson: LessonSummary }) {
  const { api, compileSlot } = useServices();
  const list = useQuery(queries(api).lessons());
  const Status = compileSlot?.Status;
  const Action = compileSlot?.Action;
  const chip = <span className="chip">Not published yet</span>;
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
                {`Previous: ${previous.title}`}
              </Link>
            ) : null}
            {next ? (
              <Link to="/lessons/$lessonId" params={{ lessonId: next.lesson_id }} className="state-link">
                {`Next: ${next.title}`}
              </Link>
            ) : null}
            <Link to="/" hash={`course-${lesson.course_id}`} className="state-link">
              {`All lessons of ${lesson.course_title}`}
            </Link>
          </>
        }
      >
        <p className="muted">{`Lesson ${lesson.ordinal} of ${lesson.course_title} is not published yet.`}</p>
        {/* WEB-UI FR-UI-6: a div (the slot's parts may be block or interactive); the default chip shows while the
            slot's Status renders nothing. */}
        <div className="state-status">
          {Status ? (
            <span className="lesson-status">
              <span className="slot-status">
                <Status lesson={lesson} />
              </span>
              <span className="status-default">{chip}</span>
            </span>
          ) : (
            chip
          )}
          {Action ? <Action lesson={lesson} /> : null}
        </div>
      </StateBlock>
    </div>
  );
}

function Lesson({ lessonId, lesson, notebook, revision }: { lessonId: string; lesson: LessonSummary; notebook: Notebook; revision: string }) {
  const { api, notify, navigateAway, tutor } = useServices();
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
      <LessonView notebook={notebook} controller={controller} aside={aside} crumbs={<Crumbs lesson={lesson} />} />
    </div>
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
