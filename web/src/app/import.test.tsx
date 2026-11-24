// @trace FR-UI-12
// The /import route and the header's Import link: owner only, the page in a lazy chunk inside main#content.
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { webRoot } from './testing/fixtures';
import { load } from './testing/load';

afterEach(cleanup);

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const user = (role: string) => ({ id: '0b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b10', handle: 'ivan', role, display_name: 'Ivan', consent: { evaluation: true, tuning: false } });

const mount = async (path: string, role: string) => {
  const { AppRoot } = await load(() => import('./AppRoot'));
  expect(AppRoot).toBeTypeOf('function');
  const fetchFn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === '/api/me') return json(200, user(role));
    if (url === '/api/lessons') return json(200, { lessons: [] });
    if (url === '/api/materials') return json(200, { materials: [{ id: 'synthetic-plan', status: 'imported', page_count: 30 }] });
    return json(404, { error: 'not_found' });
  });
  await act(async () => {
    render(<AppRoot initialPath={path} fetch={fetchFn as unknown as typeof fetch} navigateAway={vi.fn()} />);
  });
};

describe('import route and link (FR-UI-12)', () => {
  it('shows the Import link to the owner and the page inside the one main', async () => {
    await mount('/import', 'owner');
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Import a source', level: 1 })).toBeTruthy());
    expect(screen.getByRole('link', { name: 'Import' }).getAttribute('href')).toBe('/import');
    expect(document.querySelectorAll('main')).toHaveLength(1);
    expect(document.querySelector('main#content h1')?.textContent).toBe('Import a source');
    await waitFor(() => expect(screen.getByText('synthetic-plan')).toBeTruthy());
  });

  it('hides the link from a learner and shows not-found at /import', async () => {
    await mount('/import', 'learner');
    await waitFor(() => expect(screen.getByText(/not found/i)).toBeTruthy());
    expect(screen.queryByRole('link', { name: 'Import' })).toBeNull();
    expect(screen.queryByText('Import a source')).toBeNull();
  });

  it('loads the page as a lazy chunk', () => {
    const routes = readFileSync(`${webRoot}src/app/routes.tsx`, 'utf8');
    // FR-UI-13 mount note: the chunk's entry is WEB-IMPORT's page, which renders WEB-PLAN's page first
    // FR-UI-31: the lazy route loads its Russian catalogues with its chunk (withAreas).
    expect(routes).toMatch(/lazy\(\s*withAreas\(\(\) => import\('\.\.\/import\/ImportPage'\)/);
    expect(routes).not.toMatch(/^import .*ImportPage/m);
    const entry = readFileSync(`${webRoot}src/import/ImportPage.tsx`, 'utf8');
    expect(entry).toMatch(/import \{ ImportPage as PlanImportPage \} from '\.\/plan\/ImportPage'/);
  });
});
