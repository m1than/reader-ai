// The app shell (WEB-UI FR-UI-3, FR-UI-5, FR-UI-7): checks the session with /api/me first, then routes; the header
// shows the signed-in name and Sign out; notices are collected in one region.
import { Button } from '@acme/ui/button';
import { Skeleton } from '@acme/ui/skeleton';
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { createBrowserHistory, createMemoryHistory, RouterProvider } from '@tanstack/react-router';
import { useCallback, useMemo, useRef, useState } from 'react';
import { createUploadApi } from '../import/api';
import { createPlanApi } from '../import/plan/api';
import { createTutorApi } from '../tutor/api';
import { createApi, SIGN_IN_URL } from './api';
import { ServicesContext, useServices, type Services } from './context';
import { NoticeRegion, type Notice } from './notices';
import { queries } from './queries';
import { createAppRouter, isBadLessonPath } from './routes';

export interface AppRootProps {
  /** Start at this path with an in-memory history (tests, stories); the browser history otherwise. */
  initialPath?: string;
  fetch?: typeof fetch;
  /** Full-page navigation away from the SPA (the SRV-AUTH sign-in page). */
  navigateAway?: (url: string) => void;
}

const defaultNavigate = (url: string) => window.location.assign(url);

function Header({ displayName, owner, queryClient }: { displayName: string; owner: boolean; queryClient: QueryClient }) {
  const { api, notify, navigateAway } = useServices();
  const signOut = async () => {
    try {
      await api.signOut();
    } catch {
      notify('sign_out_failed');
      return;
    }
    queryClient.clear();
    navigateAway(SIGN_IN_URL);
  };
  return (
    <header className="app-header">
      <a className="brand" href="/">
        reader
      </a>
      {/* FR-UI-12: the Import link, for the owner only. */}
      {owner ? <a href="/import">Import</a> : null}
      <span className="who">{displayName}</span>
      <Button variant="ghost" color="neutral" size="sm" onClick={() => void signOut()}>
        Sign out
      </Button>
    </header>
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

function Gate({ queryClient, path, router }: { queryClient: QueryClient; path: string; router: ReturnType<typeof createAppRouter> }) {
  const { api } = useServices();
  const me = useQuery(queries(api).me());
  if (me.isPending)
    return (
      <>
        <div className="app-header" aria-hidden="true" />
        {/* FR-UI-11: a status with hidden text (no aria-busy: it would silence the status) and the library skeleton. */}
        <div className="page">
          <p role="status" className="visually-hidden">
            Loading
          </p>
          <div aria-hidden="true">
            <Skeleton.Root>
              <Skeleton.Item variant="text" width="40%" height="2.25rem" />
              <Skeleton.Item variant="text" width="90%" />
              <Skeleton.Item variant="text" width="75%" />
            </Skeleton.Root>
          </div>
        </div>
      </>
    );
  if (me.isError) return <p className="page muted">Signing in…</p>;
  return (
    <>
      <SkipLink />
      <Header displayName={me.data.display_name} owner={me.data.role === 'owner'} queryClient={queryClient} />
      <main id="content" tabIndex={-1}>
        {isBadLessonPath(path) ? (
          <div className="page">
            <h1>Not found</h1>
            <p>
              <a href="/">Back to lessons</a>
            </p>
          </div>
        ) : (
          <RouterProvider router={router} />
        )}
      </main>
    </>
  );
}

export function AppRoot({ initialPath, fetch: fetchFn, navigateAway = defaultNavigate }: AppRootProps) {
  const [notices, setNotices] = useState<Notice[]>([]);
  const seq = useRef(0);
  const notify = useCallback((code: string) => {
    seq.current += 1;
    const id = seq.current;
    setNotices((ns) => [...ns, { id, code }]);
  }, []);
  const services = useMemo<Services>(() => {
    const deps = {
      fetch: fetchFn ?? ((...a: Parameters<typeof fetch>) => globalThis.fetch(...a)),
      onUnauthorized: () => navigateAway(SIGN_IN_URL),
    };
    return {
      api: createApi(deps),
      tutor: createTutorApi(deps),
      plan: createPlanApi(deps),
      upload: createUploadApi(deps),
      notify,
      navigateAway,
    };
  }, [fetchFn, navigateAway, notify]);
  const queryClient = useMemo(() => new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } }), []);
  const path = initialPath ?? (typeof window !== 'undefined' ? window.location.pathname : '/');
  const router = useMemo(
    () => createAppRouter(initialPath ? createMemoryHistory({ initialEntries: [initialPath] }) : createBrowserHistory()),
    [initialPath],
  );
  return (
    <ServicesContext.Provider value={services}>
      <QueryClientProvider client={queryClient}>
        <Gate queryClient={queryClient} path={path} router={router} />
        <NoticeRegion notices={notices} onDismiss={(id) => setNotices((ns) => ns.filter((n) => n.id !== id))} />
      </QueryClientProvider>
    </ServicesContext.Provider>
  );
}
