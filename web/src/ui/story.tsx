// Storybook helpers (WEB-UI FR-UI-8): Calm in light or dark, a memory router, and app services over a stub fetch.
// Used only by stories.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createMemoryHistory, createRootRoute, createRouter, RouterProvider } from '@tanstack/react-router';
import type { Decorator } from '@storybook/react-vite';
import { createSignupsApi } from '../accounts/api';
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

/** A fetch that never answers (loading states). */
export const pendingFetch: typeof fetch = () => new Promise<Response>(() => undefined);

/** A fetch that answers every request with a server error (error states with Retry). */
export const failingFetch: typeof fetch = async () => json({ error: 'internal' }, 500);

export const withServices =
  (routes: Record<string, unknown>, fetchFn: typeof fetch = stubFetch(routes)): Decorator =>
  (Story) => {
    const deps = { fetch: fetchFn, onUnauthorized: () => undefined };
    const api = createApi(deps);
    const signups = createSignupsApi(deps);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return (
      <ServicesContext.Provider value={{ api, signups, notify: () => undefined, navigateAway: () => undefined }}>
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
      cell_type: 'markdown', id: 'd', source: 'Before you start, answer these.',
      metadata: { reader: { kind: 'diagnostic', questions: [{ id: 'q1', text: 'What does a digest map its input to?' }, { id: 'q2', text: 'Bits or bytes?' }] } },
    },
    {
      cell_type: 'markdown', id: 'r', source: 'Read the chapter on digests first.',
      metadata: { reader: { kind: 'route', source_refs: [{ span: `${'a'.repeat(64)}:10`, page: 11 }, { span: `${'a'.repeat(64)}:11`, page: 11 }] } },
    },
    {
      cell_type: 'markdown', id: 'e', source: '## Digests\n\nA collision is expected after about $2^{n/2}$ tries.',
      metadata: {
        reader: {
          kind: 'explain',
          claims: [{ text: 't', citations: [12, 12, 13, 14, 16].map((page, i) => ({ span: `${'a'.repeat(64)}:${12 + i}`, page, material_title: 'Sample material' })) }],
        },
      },
    },
    { cell_type: 'markdown', id: 'rc', source: '', metadata: { reader: { kind: 'recall', front: 'Collision bound for an n-bit digest?', back: 'About 2^(n/2) tries.' } } },
    { cell_type: 'markdown', id: 'n', source: '', metadata: { reader: { kind: 'notes' } } },
    { cell_type: 'code', id: 'l', source: 'print(1)', execution_count: 1, outputs: [{ output_type: 'stream', name: 'stdout', text: '1\n' }], metadata: { reader: { kind: 'lab' } } },
    { cell_type: 'markdown', id: 'o', source: 'Open: bits or bytes?', metadata: { reader: { kind: 'open', status: 'open', finding: { gate: 'G2', reason: 'not confirmed by fact-check' } } } },
  ],
};
// SRV-LESSON FR-LESSON-8: the metadata nested under `lesson`.
export const sampleLesson = {
  lesson: sampleLessons.lessons[0],
  revisions: [{ sha256: sampleSha, byte_size: 1, cell_count: 7, created_at: '2026-11-21T09:00:00Z', stages: ['sample'] }],
};
