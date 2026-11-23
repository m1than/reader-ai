// Route table (WEB-UI FR-UI-3, FR-UI-12): `/` the lesson list, `/lessons/$lessonId` the lesson view (lazy),
// `/import` the import page for the owner (lazy), anything else not-found. A lesson id outside the pattern renders
// not-found without a request.
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
const ImportPage = lazy(() => import('../import/plan/ImportPage').then((m) => ({ default: m.ImportPage })));
// FR-UI-14: WEB-ACCOUNTS's Sign-ups screen, a lazy chunk, for the owner only.
const SignupsPage = lazy(() => import('../accounts/SignupsPage').then((m) => ({ default: m.SignupsPage })));

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
    <div>
      {/* FR-UI-11: no aria-busy, which would silence the status. */}
      <p role="status" className="visually-hidden">
        {label}
      </p>
      <div aria-hidden="true">
        <Skeleton.Root>
          <Skeleton.Item variant="text" width="60%" />
          <Skeleton.Item variant="text" width="80%" />
          <Skeleton.Item variant="text" width="40%" />
        </Skeleton.Root>
      </div>
    </div>
  );
}

function ListPage() {
  const { api } = useServices();
  const q = useQuery(queries(api).lessons());
  const me = useQuery(queries(api).me());
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
        <LessonList lessons={q.data.lessons} locale={locale()} owner={me.data?.role === 'owner'} />
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

// FR-UI-12: the owner's import page; anyone else sees not-found.
const importRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/import',
  component: function ImportRoute() {
    const { api } = useServices();
    const me = useQuery(queries(api).me());
    if (me.isPending) return null;
    if (me.data?.role !== 'owner') return <NotFound />;
    return (
      <Suspense
        fallback={
          <div className="page">
            <Loading label="Loading the import page" />
          </div>
        }
      >
        <ImportPage />
      </Suspense>
    );
  },
});

const signupsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/signups',
  component: function SignupsRoute() {
    const { api } = useServices();
    const me = useQuery(queries(api).me());
    // A non-owner gets not-found without any request to the sign-up routes (FR-UI-14).
    if (me.data?.role !== 'owner') return <NotFound />;
    return (
      <Suspense
        fallback={
          <div className="page">
            <Loading label="Loading sign-ups" />
          </div>
        }
      >
        <SignupsPage />
      </Suspense>
    );
  },
});

const routeTree = rootRoute.addChildren([indexRoute, lessonRoute, importRoute, signupsRoute]);

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
