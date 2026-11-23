// Route table (WEB-UI FR-UI-3): `/` the lesson list, `/lessons/$lessonId` the lesson view (lazy), anything else
// not-found. A lesson id outside the pattern renders not-found without a request.
import { Button } from '@acme/ui/button';
import { Skeleton } from '@acme/ui/skeleton';
import { useQuery } from '@tanstack/react-query';
import { createRootRoute, createRoute, createRouter, Link, Outlet, type RouterHistory } from '@tanstack/react-router';
import { lazy, Suspense } from 'react';
import { useServices } from './context';
import { LessonList } from './LessonList';
import { queries } from './queries';

const LESSON_ID = /^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$/;

export const isLessonId = (id: string): boolean => LESSON_ID.test(id) && id !== '.' && id !== '..';

const LessonPage = lazy(() => import('../notebook/LessonPage').then((m) => ({ default: m.LessonPage })));

const locale = () => (typeof navigator !== 'undefined' && navigator.language) || 'en';

// Pages render inside the shell's single `main#content` (FR-UI-11).
function NotFound() {
  return (
    <div className="page">
      <h1>Not found</h1>
      <p>
        <Link to="/">Back to lessons</Link>
      </p>
    </div>
  );
}

function Loading({ label }: { label: string }) {
  return (
    <div aria-busy="true" role="status">
      <span className="visually-hidden">{label}</span>
      <Skeleton.Root>
        <Skeleton.Item variant="text" width="60%" />
        <Skeleton.Item variant="text" width="80%" />
        <Skeleton.Item variant="text" width="40%" />
      </Skeleton.Root>
    </div>
  );
}

function ListPage() {
  const { api } = useServices();
  const q = useQuery(queries(api).lessons());
  return (
    <div className="page">
      <h1>Lessons</h1>
      {q.isPending ? (
        <Loading label="Loading the lessons" />
      ) : q.isError ? (
        <div className="error state-actions">
          <p>The lessons could not be loaded.</p>
          <Button variant="outline" color="neutral" onClick={() => void q.refetch()}>
            Retry
          </Button>
        </div>
      ) : (
        <LessonList lessons={q.data.lessons} locale={locale()} />
      )}
    </div>
  );
}

const rootRoute = createRootRoute({ component: () => <Outlet />, notFoundComponent: NotFound });

const indexRoute = createRoute({ getParentRoute: () => rootRoute, path: '/', component: ListPage });

const lessonRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/lessons/$lessonId',
  component: function LessonRoute() {
    const { lessonId } = lessonRoute.useParams();
    if (!isLessonId(lessonId)) return <NotFound />;
    return (
      <Suspense
        fallback={
          <div className="page">
            <Loading label="Loading the lesson" />
          </div>
        }
      >
        <LessonPage lessonId={lessonId} />
      </Suspense>
    );
  },
});

const routeTree = rootRoute.addChildren([indexRoute, lessonRoute]);

export const createAppRouter = (history: RouterHistory) =>
  createRouter({ routeTree, history, defaultNotFoundComponent: NotFound });

/** True when a raw path addresses the lesson route with an id outside the pattern (checked before routing, since a
 * router may normalise `..`). */
export const isBadLessonPath = (path: string): boolean => {
  const m = /^\/lessons\/([^/?#]*)/.exec(path);
  if (!m) return false;
  let id: string;
  try {
    id = decodeURIComponent(m[1] ?? '');
  } catch {
    return true;
  }
  return !isLessonId(id);
};
