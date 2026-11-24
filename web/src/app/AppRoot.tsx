// The app shell (WEB-UI FR-UI-3, FR-UI-5, FR-UI-7, FR-UI-11): checks the session with /api/me first, then routes; the
// root route renders the header (user menu with the theme choice and Sign out), the course sidebar and the single
// `main`; notices are collected in one region.
import { Skeleton } from '@acme/ui/skeleton';
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { createBrowserHistory, createMemoryHistory, RouterProvider } from '@tanstack/react-router';
import { useCallback, useMemo, useRef, useState } from 'react';
import { createSignupsApi } from '../accounts/api';
import { createUploadApi } from '../import/api';
import { createPlanApi } from '../import/plan/api';
import { createTutorApi } from '../tutor/api';
import { createApi, SIGN_IN_URL } from './api';
import { ownerCompileSlot } from './compile/slot';
import { createObsApi } from './observability/api';
import { ServicesContext, useServices, type CompileSlot, type Services } from './context';
import { NoticeRegion, type Notice } from './notices';
import { queries } from './queries';
import { createAppRouter, isBadLessonPath } from './routes';
import { ShellContext, type ShellState } from './shell';

export interface AppRootProps {
  /** Start at this path with an in-memory history (tests, stories); the browser history otherwise. */
  initialPath?: string;
  fetch?: typeof fetch;
  /** Full-page navigation away from the SPA (the SRV-AUTH sign-in page). */
  navigateAway?: (url: string) => void;
  /** The compile slot for lesson rows and the lesson page (FR-UI-6); without it, the app's own slot renders the
   * owner's CompileControl (FR-UI-15). */
  compileSlot?: CompileSlot;
}

const defaultNavigate = (url: string) => window.location.assign(url);

function Gate({ queryClient, path, router }: { queryClient: QueryClient; path: string; router: ReturnType<typeof createAppRouter> }) {
  const { api, notify, navigateAway } = useServices();
  const me = useQuery(queries(api).me());
  const shell = useMemo<ShellState | null>(() => {
    if (!me.data) return null;
    return {
      displayName: me.data.display_name,
      owner: me.data.role === 'owner',
      badFirstPath: isBadLessonPath(path),
      onSignOut: () => {
        void (async () => {
          try {
            await api.signOut();
          } catch {
            notify('sign_out_failed');
            return;
          }
          queryClient.clear();
          navigateAway(SIGN_IN_URL);
        })();
      },
    };
  }, [me.data, path, api, notify, navigateAway, queryClient]);
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
  if (me.isError || !shell) return <p className="page muted">Signing in…</p>;
  return (
    <ShellContext.Provider value={shell}>
      <RouterProvider router={router} />
    </ShellContext.Provider>
  );
}

export function AppRoot({ initialPath, fetch: fetchFn, navigateAway = defaultNavigate, compileSlot }: AppRootProps) {
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
      signups: createSignupsApi(deps),
      obs: createObsApi(deps), // FR-UI-18
      http: deps,
      upload: createUploadApi(deps), // FR-UI-13
      notify,
      navigateAway,
      // FR-UI-6, FR-UI-15: the owner's CompileControl renders through the slot unless a caller supplies its own.
      compileSlot: compileSlot ?? ownerCompileSlot,
    };
  }, [fetchFn, navigateAway, notify, compileSlot]);
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
