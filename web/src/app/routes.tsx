// Route table (WEB-UI FR-UI-3, FR-UI-12): `/` the lesson list, `/lessons/$lessonId` the lesson view (lazy),
// `/import` the import page for the owner (lazy), `/materials/$materialId/freshness` the freshness card (lazy;
// WEB-FRESH), anything else not-found. A lesson id outside the pattern renders not-found without a request. The root
// route renders the shell (FR-UI-11): skip link, header, course sidebar and the single `main#content`, so header and
// sidebar links are router links.
import { Button } from '@acme/ui/button';
import { Skeleton } from '@acme/ui/skeleton';
import { useQuery } from '@tanstack/react-query';
import { createRootRoute, createRoute, createRouter, Link, Outlet, useNavigate, useRouterState, type RouterHistory } from '@tanstack/react-router';
import { lazy, Suspense, useContext, useEffect, useState } from 'react';
import { useLocale, useT, withAreas } from '../ui/i18n';
import { SearchOffIcon, WarningIcon } from '../ui/icons';
import en from './messages.en';
import { StateBlock } from '../ui/StateBlock';
import type { LessonSummary } from './api';
import { useServices } from './context';
import { CourseSidebar } from './CourseSidebar';
import { Header } from './Header';
import { LessonList } from './LessonList';
import { queries } from './queries';
import { ShellContext } from './shell';

const LESSON_ID = /^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$/;

export const isLessonId = (id: string): boolean => LESSON_ID.test(id) && id !== '.' && id !== '..';

// FR-UI-31: each lazy route loads its areas' Russian catalogues together with its chunk (withAreas).
const LessonPage = lazy(
  withAreas(() => import('../notebook/LessonPage').then((m) => ({ default: m.LessonPage })), ['notebook', 'render', 'tutor', 'manage']),
);
// FR-UI-13 mount note: WEB-IMPORT's page composes WEB-PLAN's page with the upload sections.
const ImportPage = lazy(withAreas(() => import('../import/ImportPage').then((m) => ({ default: m.ImportPage })), ['import', 'import/plan', 'manage']));
// FR-UI-14: WEB-ACCOUNTS's Sign-ups screen, a lazy chunk, for the owner only.
const SignupsPage = lazy(withAreas(() => import('../accounts/SignupsPage').then((m) => ({ default: m.SignupsPage })), ['accounts']));
// FR-UI-18: the observability page, a lazy chunk, for the owner only.
const ObservabilityPage = lazy(
  withAreas(() => import('./observability/ObservabilityPage').then((m) => ({ default: m.ObservabilityPage })), ['app/observability']),
);

// WEB-FRESH (paired change): the freshness card's own lazy route until it mounts in the sources list.
const MATERIAL_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const FreshnessPage = lazy(withAreas(() => import('../fresh/FreshnessPage').then((m) => ({ default: m.FreshnessPage })), ['fresh']));

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

const useAppT = () => useT('app', en);

// Pages render inside the shell's single `main#content` (FR-UI-11).
function NotFound() {
  const t = useAppT();
  return (
    <div className="page">
      <StateBlock
        icon={<SearchOffIcon size={20} />}
        title={t('shell.not_found.title')}
        actions={
          <Link to="/" className="state-link">
            {t('shell.not_found.back')}
          </Link>
        }
      >
        <p className="muted">{t('shell.not_found.body')}</p>
      </StateBlock>
    </div>
  );
}

type LoadingKey = 'loading.lessons' | 'loading.lesson' | 'loading.import' | 'loading.signups' | 'loading.freshness' | 'loading.observability' | 'loading.settings';

function Loading({ label }: { label: LoadingKey }) {
  const t = useAppT();
  return (
    <div>
      {/* FR-UI-11: no aria-busy, which would silence the status. */}
      <p role="status" className="visually-hidden">
        {t(label)}
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
  const t = useAppT();
  return (
    <a
      className="skip-link"
      href="#content"
      onClick={() => {
        const main = document.getElementById('content');
        if (main) window.setTimeout(() => main.focus({ preventScroll: true }), 0);
      }}
    >
      {t('shell.skip')}
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
  useFirstRun(q.data?.lessons);
  const t = useAppT();
  const { formatLocale } = useLocale();
  return (
    <div className="page">
      <h1>{t('list.title')}</h1>
      {q.isPending ? (
        <Loading label="loading.lessons" />
      ) : q.isError ? (
        <StateBlock
          tone="danger"
          icon={<WarningIcon size={20} />}
          title={t('list.error.title')}
          actions={
            <Button variant="outline" color="neutral" onClick={() => void q.refetch()}>
              {t('list.retry')}
            </Button>
          }
        >
          <p className="muted">{t('list.error.body')}</p>
        </StateBlock>
      ) : (
        <LessonList lessons={q.data.lessons} locale={formatLocale} focusCourse={focusCourse} />
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
            <Loading label="loading.lesson" />
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
            <Loading label="loading.import" />
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
            <Loading label="loading.signups" />
          </div>
        }
      >
        <SignupsPage />
      </Suspense>
    );
  },
});

const freshnessRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/materials/$materialId/freshness',
  component: function FreshnessRoute() {
    const { materialId } = freshnessRoute.useParams();
    if (!MATERIAL_ID.test(materialId)) return <NotFound />;
    return (
      <Suspense
        fallback={
          <div className="page">
            <Loading label="loading.freshness" />
          </div>
        }
      >
        <FreshnessPage materialId={materialId} />
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
            <Loading label="loading.observability" />
          </div>
        }
      >
        <ObservabilityPage />
      </Suspense>
    );
  },
});

const routeTree = rootRoute.addChildren([
  indexRoute,
  lessonRoute,
  importRoute,
  signupsRoute,
  observabilityRoute,
  freshnessRoute,
]);

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
