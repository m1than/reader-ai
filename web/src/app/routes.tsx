// Route table (WEB-UI FR-UI-3, FR-UI-12): `/` the lesson list, `/lessons/$lessonId` the lesson view (lazy),
// `/import` the import page for the owner (lazy), anything else not-found. A lesson id outside the pattern renders
// not-found without a request. The root route renders the shell (FR-UI-11): skip link, header, course sidebar and the
// single `main#content`, so header and sidebar links are router links.
import { Button } from '@acme/ui/button';
import { Skeleton } from '@acme/ui/skeleton';
import { useQuery } from '@tanstack/react-query';
import { createRootRoute, createRoute, createRouter, Link, Outlet, useRouterState, type RouterHistory } from '@tanstack/react-router';
import { lazy, Suspense, useContext, useState } from 'react';
import { SearchOffIcon, WarningIcon } from '../ui/icons';
import { StateBlock } from '../ui/StateBlock';
import { useServices } from './context';
import { CourseSidebar } from './CourseSidebar';
import { Header } from './Header';
import { LessonList } from './LessonList';
import { queries } from './queries';
import { ShellContext } from './shell';

const LESSON_ID = /^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$/;

export const isLessonId = (id: string): boolean => LESSON_ID.test(id) && id !== '.' && id !== '..';

const LessonPage = lazy(() => import('../notebook/LessonPage').then((m) => ({ default: m.LessonPage })));
const ImportPage = lazy(() => import('../import/plan/ImportPage').then((m) => ({ default: m.ImportPage })));
// FR-UI-14: WEB-ACCOUNTS's Sign-ups screen, a lazy chunk, for the owner only.
const SignupsPage = lazy(() => import('../accounts/SignupsPage').then((m) => ({ default: m.SignupsPage })));
// FR-UI-18: the observability page, a lazy chunk, for the owner only.
const ObservabilityPage = lazy(() => import('./observability/ObservabilityPage').then((m) => ({ default: m.ObservabilityPage })));

const locale = () => (typeof navigator !== 'undefined' && navigator.language) || 'en';

// Pages render inside the shell's single `main#content` (FR-UI-11).
function NotFound() {
  return (
    <div className="page">
      <StateBlock
        icon={<SearchOffIcon size={20} />}
        title="Not found"
        actions={
          <Link to="/" className="state-link">
            Back to lessons
          </Link>
        }
      >
        <p className="muted">Nothing lives at this address.</p>
      </StateBlock>
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

/** FR-UI-11: the first Tab stop; following it focuses the shell's single `main`. */
function SkipLink() {
  return (
    <a
      className="skip-link"
      href="#content"
      onClick={() => {
        const main = document.getElementById('content');
        if (main) window.setTimeout(() => main.focus({ preventScroll: true }), 0);
      }}
    >
      Skip to content
    </a>
  );
}

const lessonOf = (path: string): string | null => {
  const m = /^\/lessons\/([^/?#]+)/.exec(path);
  if (!m) return null;
  try {
    return decodeURIComponent(m[1]!);
  } catch {
    return null;
  }
};

function Shell() {
  const shell = useContext(ShellContext);
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const [firstPath] = useState(pathname);
  const bad = Boolean(shell?.badFirstPath) && pathname === firstPath;
  const current = lessonOf(pathname);
  return (
    <>
      <SkipLink />
      {shell ? (
        <Header displayName={shell.displayName} owner={shell.owner} path={pathname} currentLesson={current} onSignOut={shell.onSignOut} />
      ) : null}
      <div className="shell">
        <aside className="shell-sidebar">
          <CourseSidebar key={current ?? '-'} current={current} prefix="sidebar" />
        </aside>
        <main id="content" tabIndex={-1}>
          {bad ? <NotFound /> : <Outlet />}
        </main>
      </div>
    </>
  );
}

function ListPage() {
  const { api } = useServices();
  const q = useQuery(queries(api).lessons());
  const hash = useRouterState({ select: (s) => s.location.hash });
  const focusCourse = hash.startsWith('course-') ? hash.slice('course-'.length) : null;
  return (
    <div className="page">
      <h1>Lessons</h1>
      {q.isPending ? (
        <Loading label="Loading the lessons" />
      ) : q.isError ? (
        <StateBlock
          tone="danger"
          icon={<WarningIcon size={20} />}
          title="The lessons could not be loaded."
          actions={
            <Button variant="outline" color="neutral" onClick={() => void q.refetch()}>
              Retry
            </Button>
          }
        >
          <p className="muted">Check the connection, then retry.</p>
        </StateBlock>
      ) : (
        <LessonList lessons={q.data.lessons} locale={locale()} focusCourse={focusCourse} />
      )}
    </div>
  );
}

const rootRoute = createRootRoute({ component: Shell, notFoundComponent: NotFound });

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

const observabilityRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/observability',
  component: function ObservabilityRoute() {
    const { api } = useServices();
    const me = useQuery(queries(api).me());
    // A non-owner gets not-found without any request to /api/obs/* (FR-UI-18).
    if (me.data?.role !== 'owner') return <NotFound />;
    return (
      <Suspense
        fallback={
          <div className="page">
            <Loading label="Loading observability" />
          </div>
        }
      >
        <ObservabilityPage />
      </Suspense>
    );
  },
});

const routeTree = rootRoute.addChildren([indexRoute, lessonRoute, importRoute, signupsRoute, observabilityRoute]);

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
