// The app shell (WEB-UI FR-UI-3, FR-UI-5, FR-UI-7, FR-UI-11): checks the session with /api/me first, then routes; the
// root route renders the header (user menu with the theme choice and Sign out), the course sidebar and the single
// `main`; notices are collected in one region.
import { Skeleton } from '@acme/ui/skeleton';
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { createBrowserHistory, createMemoryHistory, RouterProvider } from '@tanstack/react-router';
import { useCallback, useMemo, useRef, useState } from 'react';
import { createSignupsApi } from '../accounts/api';
import { createPlanApi } from '../import/plan/api';
import { createApi, SIGN_IN_URL } from './api';
import { ownerCompileSlot } from './compile/slot';
import { manageSlot } from '../manage/slot';
import { ServicesContext, useServices, type CompileSlot, type ManageSlot, type Services } from './context';
import { NoticeRegion, type Notice } from './notices';
import { queries } from './queries';
import { createAppRouter, isBadLessonPath } from './routes';
import { ShellContext, type ShellState } from './shell';
import { I18nProvider, useT } from '../ui/i18n';
import en from './messages.en';

export interface AppRootProps {
  /** Start at this path with an in-memory history (tests, stories); the browser history otherwise. */
  initialPath?: string;
  fetch?: typeof fetch;
  /** Full-page navigation away from the SPA (the SRV-AUTH sign-in page). */
  navigateAway?: (url: string) => void;
  /** The compile slot for lesson rows and the lesson page (FR-UI-6); without it, the app's own slot renders the
   * owner's CompileControl (FR-UI-15). */
  compileSlot?: CompileSlot;
  /** WEB-MANAGE's slot (FR-UI-6 paired change); `null` renders no management control. */
  manageSlot?: ManageSlot | null;
}

const defaultNavigate = (url: string) => window.location.assign(url);

function Gate({ queryClient, path, router }: { queryClient: QueryClient; path: string; router: ReturnType<typeof createAppRouter> }) {
  const { api, notify, navigateAway } = useServices();
  const me = useQuery(queries(api).me());
  const t = useT('app', en);
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
            {t('shell.loading')}
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
  if (me.isError || !shell) return <p className="page muted">{t('shell.signing_in')}</p>;
  return (
    <ShellContext.Provider value={shell}>
      <RouterProvider router={router} />
    </ShellContext.Provider>
  );
}

export function AppRoot({ initialPath, fetch: fetchFn, navigateAway = defaultNavigate, compileSlot, manageSlot: manage }: AppRootProps) {
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
      plan: createPlanApi(deps),
      signups: createSignupsApi(deps),
      // The tutor (FR-TUTOR-11), observability (FR-UI-18) and upload (FR-UI-13) clients are built by their lazy chunks
      // from `http`, so their schemas stay out of the initial chunk (NFR-UI-1, V/manage).
      http: deps,
      notify,
      navigateAway,
      // FR-UI-6, FR-UI-15: the owner's CompileControl renders through the slot unless a caller supplies its own.
      compileSlot: compileSlot ?? ownerCompileSlot,
      ...(manage === null ? {} : { manageSlot: manage ?? manageSlot }),
    };
  }, [fetchFn, navigateAway, notify, compileSlot, manage]);
  const Host = services.manageSlot?.Host;
  const queryClient = useMemo(() => new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } }), []);
  const path = initialPath ?? (typeof window !== 'undefined' ? window.location.pathname : '/');
  const router = useMemo(
    () => createAppRouter(initialPath ? createMemoryHistory({ initialEntries: [initialPath] }) : createBrowserHistory()),
    [initialPath],
  );
  return (
    <I18nProvider>
      <ServicesContext.Provider value={services}>
        <QueryClientProvider client={queryClient}>
          <Gate queryClient={queryClient} path={path} router={router} />
          <NoticeRegion notices={notices} onDismiss={(id) => setNotices((ns) => ns.filter((n) => n.id !== id))} />
          {Host ? <Host /> : null}
        </QueryClientProvider>
      </ServicesContext.Provider>
    </I18nProvider>
  );
}
