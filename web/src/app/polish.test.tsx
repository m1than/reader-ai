// @trace FR-UI-6 FR-UI-11 VEC-UI-2
// The shell's presentation, amended from the live screenshots (2026-11-23, V/WEB-polish).
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { readJson } from './testing/fixtures';
import { load } from './testing/load';

afterEach(cleanup);

const list = () => readJson<{ list: { lessons: unknown[] } }>('spec/fixtures/WEB-UI/lessons.json').list;
const me = { id: '0b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b10', handle: 'ivan', role: 'owner', display_name: 'Ivan', consent: { evaluation: true, tuning: false } };
const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

const mount = async (path: string, route: (url: string) => Response | Promise<Response>) => {
  const { AppRoot } = await load(() => import('./AppRoot'));
  expect(AppRoot).toBeTypeOf('function');
  const fetchFn = vi.fn(async (input: RequestInfo | URL) => route(String(input)));
  await act(async () => {
    render(<AppRoot initialPath={path} fetch={fetchFn as unknown as typeof fetch} navigateAway={vi.fn()} />);
  });
};

describe('lesson rows (FR-UI-6, amended)', () => {
  it('shows ordinal, title, status and date as separate elements of one row', async () => {
    await mount('/', (url) => (url === '/api/me' ? json(200, me) : json(200, list())));
    await waitFor(() => expect(screen.getByText('Fixture course')).toBeTruthy());
    const row = screen.getByText('Fixture lesson W02: not compiled yet').closest('li') as HTMLElement;
    expect(within(row).getByText('2', { exact: true })).toBeTruthy();
    expect(within(row).getByText('not published yet', { exact: true })).toBeTruthy();
    expect(row.querySelector('time')).toBeTruthy();
    const published = screen.getByText('Fixture lesson W01: one cell of every kind').closest('a') as HTMLElement;
    expect(published.textContent).toBe('Fixture lesson W01: one cell of every kind');
  });
});

describe('skip link and shell loading (FR-UI-11)', () => {
  it('makes "Skip to content" the first link, pointing at a focusable main', async () => {
    await mount('/', (url) => (url === '/api/me' ? json(200, me) : json(200, list())));
    await waitFor(() => expect(screen.getByText('Fixture course')).toBeTruthy());
    const first = document.querySelector('a') as HTMLAnchorElement;
    expect(first.textContent).toBe('Skip to content');
    expect(first.getAttribute('href')).toBe('#content');
    const main = document.getElementById('content') as HTMLElement;
    expect(main.tagName).toBe('MAIN');
    expect(main.getAttribute('tabindex')).toBe('-1');
  });

  it('shows a busy skeleton, not a bare line, while /api/me loads', async () => {
    await mount('/', () => new Promise<Response>(() => undefined));
    expect(document.querySelector('[aria-busy="true"]')).toBeTruthy();
    expect(screen.queryByText('Loading…')).toBeNull();
  });
});
