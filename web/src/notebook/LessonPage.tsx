// The lesson route (WEB-NOTEBOOK FR-NOTEBOOK-1, FR-NOTEBOOK-12): metadata first, then the immutable latest revision;
// one kernel controller per open view, disposed when the view closes (FR-NOTEBOOK-4). Rendered inside the shell's
// single `main` (WEB-UI FR-UI-11).
import { Button } from '@acme/ui/button';
import { Skeleton } from '@acme/ui/skeleton';
import { useQuery } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { useEffect, useMemo } from 'react';
import { ApiError, SIGN_IN_URL } from '../app/api';
import { useServices } from '../app/context';
import { queries } from '../app/queries';
import { AskPanel } from '../tutor/AskPanel';
import { browserSocketFactory, createKernelController } from './kernel';
import { LessonView, type Notebook } from './LessonView';

const BackLink = () => (
  <Link to="/" className="back-link">
    Lessons
  </Link>
);

/** FR-NOTEBOOK-12: the library skeleton in a busy status container. */
const LessonSkeleton = () => (
  <div className="page" aria-busy="true" role="status">
    <span className="visually-hidden">Loading the lesson</span>
    <Skeleton.Root>
      <Skeleton.Item variant="text" width="50%" height="2.25rem" />
      <Skeleton.Item variant="text" width="100%" />
      <Skeleton.Item variant="text" width="95%" />
      <Skeleton.Item variant="text" width="70%" />
    </Skeleton.Root>
  </div>
);

const Failed = ({ error, retry }: { error: unknown; retry: () => void }) => {
  const missing = error instanceof ApiError && error.status === 404;
  return (
    <div className="page state">
      <h1>{missing ? 'Lesson not found' : 'The lesson could not be loaded.'}</h1>
      <p className="state-actions">
        {!missing && (
          <Button variant="outline" color="neutral" onClick={retry}>
            Retry
          </Button>
        )}
        <Link to="/">Back to lessons</Link>
      </p>
    </div>
  );
};

function Lesson({ lessonId, notebook, revision }: { lessonId: string; notebook: Notebook; revision: string }) {
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
  // SRV-TUTOR FR-TUTOR-11: the Ask panel after the cells, bound to the revision on screen and the focused cell.
  const aside = tutor
    ? (cellId: string | null, cellLabel: string | null) => (
        <AskPanel lessonId={lessonId} revision={revision} cellId={cellId} cellLabel={cellLabel} api={tutor} />
      )
    : undefined;
  return (
    <div className="page lesson-page">
      <LessonView notebook={notebook} controller={controller} aside={aside} back={<BackLink />} />
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
  if (sha === null)
    return (
      <div className="page">
        <BackLink />
        <h1>{meta.data.lesson.title}</h1>
        <p className="muted">This lesson is not published yet.</p>
      </div>
    );
  if (rev.isPending) return <LessonSkeleton />;
  if (rev.isError) return <Failed error={rev.error} retry={() => void rev.refetch()} />;
  return <Lesson lessonId={lessonId} notebook={rev.data as unknown as Notebook} revision={sha} />;
}
