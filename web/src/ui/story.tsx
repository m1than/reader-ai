// Storybook helpers (WEB-UI FR-UI-8): Calm in light or dark, a memory router, and app services over a stub fetch.
// Used only by stories.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createMemoryHistory, createRootRoute, createRouter, RouterProvider } from '@tanstack/react-router';
import type { Decorator } from '@storybook/react-vite';
import { createApi } from '../app/api';
import { ServicesContext } from '../app/context';
import type { KernelController, KernelState } from '../notebook/kernel';

export const themed =
  (mode: 'light' | 'dark'): Decorator =>
  (Story) => {
    if (typeof document !== 'undefined') document.documentElement.dataset.theme = mode;
    return (
      <div data-theme={mode} style={{ padding: 16, background: 'var(--acme-canvas)', color: 'var(--acme-text)' }}>
        <Story />
      </div>
    );
  };

export const withRouter: Decorator = (Story) => {
  const router = createRouter({
    routeTree: createRootRoute({ component: () => <Story /> }),
    history: createMemoryHistory({ initialEntries: ['/'] }),
  });
  return <RouterProvider router={router} />;
};

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

/** A stub fetch answering from a path → body table; anything else is 404. */
export const stubFetch =
  (routes: Record<string, unknown>): typeof fetch =>
  async (input) => {
    const url = String(input);
    return Object.hasOwn(routes, url) ? json(routes[url]) : json({ error: 'not_found' }, 404);
  };

export const withServices =
  (routes: Record<string, unknown>): Decorator =>
  (Story) => {
    const api = createApi({ fetch: stubFetch(routes), onUnauthorized: () => undefined });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return (
      <ServicesContext.Provider value={{ api, notify: () => undefined, navigateAway: () => undefined }}>
        <QueryClientProvider client={client}>
          <Story />
        </QueryClientProvider>
      </ServicesContext.Provider>
    );
  };

/** A kernel controller that holds a fixed state and does nothing. */
export const staticController = (cells: KernelState['cells'] = {}): KernelController => {
  const state: KernelState = { cells, slot: null, session: null, connection: 'none', resetting: false, resetFailed: false };
  return {
    run: () => undefined,
    runAll: () => undefined,
    interrupt: () => undefined,
    reset: () => undefined,
    retry: () => undefined,
    dispose: () => undefined,
    getState: () => state,
    subscribe: () => () => undefined,
  };
};

export const sampleMe = { id: '0b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b10', handle: 'ivan', role: 'owner', display_name: 'Ivan', consent: { evaluation: true, tuning: false } };
export const sampleSha = '1'.repeat(64);
export const sampleLessons = {
  lessons: [
    { lesson_id: 'w01', course_id: 'c1', course_title: 'Sample course', ordinal: 1, title: 'Hashing', updated_at: '2026-11-21T09:00:00Z', latest_sha256: sampleSha },
    { lesson_id: 'w02', course_id: 'c1', course_title: 'Sample course', ordinal: 2, title: 'Signatures', updated_at: '2026-11-21T09:00:00Z', latest_sha256: null },
  ],
};
export const sampleNotebook = {
  nbformat: 4,
  nbformat_minor: 5,
  metadata: { reader: { title: 'Hashing' } },
  cells: [
    {
      cell_type: 'markdown', id: 'e', source: '## Digests\n\nA collision is expected after about $2^{n/2}$ tries.',
      metadata: { reader: { kind: 'explain', claims: [{ text: 't', citations: [{ span: `${'a'.repeat(64)}:12`, page: 12, material_title: 'Sample material' }] }] } },
    },
    { cell_type: 'code', id: 'l', source: 'print(1)', execution_count: 1, outputs: [{ output_type: 'stream', name: 'stdout', text: '1\n' }], metadata: { reader: { kind: 'lab' } } },
    { cell_type: 'markdown', id: 'o', source: 'Open: bits or bytes?', metadata: { reader: { kind: 'open', status: 'open', finding: { gate: 'G2', reason: 'not confirmed by fact-check' } } } },
  ],
};
export const sampleLesson = { ...sampleLessons.lessons[0], revisions: [{ sha256: sampleSha, created_at: '2026-11-21T09:00:00Z' }] };
