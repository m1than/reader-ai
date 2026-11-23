// Route table (WEB-UI FR-UI-3, FR-UI-12): `/` the lesson list, `/lessons/$lessonId` the lesson view (lazy),
// `/import` the import page for the owner (lazy), anything else not-found. A lesson id outside the pattern renders
// not-found without a request.
import { Button } from '@acme/ui/button';
import { Skeleton } from '@acme/ui/skeleton';
import { useQueries, useQuery } from '@tanstack/react-query';
import { createRootRoute, createRoute, createRouter, Link, Outlet, useNavigate, type RouterHistory } from '@tanstack/react-router';
import { lazy, Suspense, useEffect } from 'react';
import type { LessonSummary } from './api';
import { useServices } from './context';
import { LessonList } from './LessonList';
import { queries } from './queries';

const LESSON_ID = /^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$/;

export const isLessonId = (id: string): boolean => LESSON_ID.test(id) && id !== '.' && id !== '..';

const LessonPage = lazy(() => import('../notebook/LessonPage').then((m) => ({ default: m.LessonPage })));
// FR-UI-13 mount note: WEB-IMPORT's page composes WEB-PLAN's page with the upload sections.
const ImportPage = lazy(() => import('../import/ImportPage').then((m) => ({ default: m.ImportPage })));

// FR-UI-13: at most 10 compile queries per render (one plan confirm starts one compile); 10 s while compiling.
const COMPILE_QUERIES = 10;
const COMPILE_REFETCH_MS = 10_000;
const COMPILING = new Set(['running', 'stalled']);
const STOPPED = new Set(['stage_failed', 'budget_exhausted']);

export const compileLabel = (state: string | null | undefined): string => {
  if (state === null) return 'not compiled yet';
  if (state !== undefined && COMPILING.has(state)) return 'Compiling';
  if (state !== undefined && STOPPED.has(state)) return 'Compile stopped';
  return 'not published yet';
};

/** FR-UI-13 first run: an owner with no lessons and no material at all lands on /import (history replace). */
function useFirstRun(lessons: LessonSummary[] | undefined) {
  const { api, plan } = useServices();
  const me = useQuery(queries(api).me());
  const navigate = useNavigate();
  const owner = me.data?.role === 'owner';
  const materials = useQuery({
    queryKey: ['plan', 'materials'],
    queryFn: () => plan!.materials(),
    enabled: owner && plan !== undefined && lessons !== undefined && lessons.length === 0,
  });
  const empty = materials.isSuccess && materials.data.materials.length === 0;
  useEffect(() => {
    if (empty) void navigate({ to: '/import', replace: true });
  }, [empty, navigate]);
}

function useCompileStatus(lessons: LessonSummary[]): Record<string, string> {
  const { api } = useServices();
  const asked = lessons.filter((l) => !l.latest_sha256).slice(0, COMPILE_QUERIES);
  const results = useQueries({
    queries: asked.map((l) => ({
      queryKey: ['compile', l.lesson_id],
      queryFn: () => api.compileState(l.lesson_id),
      refetchInterval: (q: { state: { data: string | null | undefined } }) =>
        q.state.data && COMPILING.has(q.state.data) ? COMPILE_REFETCH_MS : false,
    })),
  });
  return Object.fromEntries(asked.map((l, i) => [l.lesson_id, compileLabel(results[i]?.isSuccess ? results[i].data : undefined)]));
}

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
  useFirstRun(q.data?.lessons);
  const status = useCompileStatus(q.data?.lessons ?? []);
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
        <LessonList lessons={q.data.lessons} locale={locale()} status={status} />
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

const routeTree = rootRoute.addChildren([indexRoute, lessonRoute, importRoute]);

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
