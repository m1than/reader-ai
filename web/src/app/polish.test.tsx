// @trace FR-UI-6 FR-UI-11 VEC-UI-2
// The shell's presentation, amended from the live screenshots (2026-11-23, V/WEB-polish).
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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

const group = () => document.querySelector('#content #course-c-fixture') as HTMLElement;

describe('lesson rows (FR-UI-6, amended)', () => {
  it('shows ordinal, title, status and date as separate elements of one row', async () => {
    await mount('/', (url) => (url === '/api/me' ? json(200, me) : json(200, list())));
    // Design pass: titles also appear in the sidebar and the Continue block; look inside the course group.
    await waitFor(() => expect(group()).toBeTruthy());
    const row = within(group()).getByText('Fixture lesson W02: not compiled yet').closest('li') as HTMLElement;
    expect(within(row).getByText('2', { exact: true })).toBeTruthy();
    expect(within(row).getByText('not published yet', { exact: true })).toBeTruthy();
    expect(row.querySelector('time')).toBeTruthy();
    const published = within(group()).getByText('Fixture lesson W01: one cell of every kind').closest('a') as HTMLElement;
    expect(published.textContent).toBe('Fixture lesson W01: one cell of every kind');
  });
});

const fixture = () => readJson<{ lesson: { lesson: { latest_sha256: string } } }>('spec/fixtures/WEB-UI/lessons.json');
const nb = () => readJson<unknown>('spec/fixtures/WEB-NOTEBOOK/lesson-w01.ipynb');

const shellTarget = () => {
  const first = document.querySelector('a') as HTMLAnchorElement;
  expect(first.textContent).toBe('Skip to content');
  expect(first.getAttribute('href')).toBe('#content');
  const mains = document.querySelectorAll('main');
  expect(mains).toHaveLength(1);
  expect(mains[0]!.id).toBe('content');
  expect(mains[0]!.getAttribute('tabindex')).toBe('-1');
};

describe('skip link, one main and shell loading (FR-UI-11)', () => {
  it('on the lesson list', async () => {
    await mount('/', (url) => (url === '/api/me' ? json(200, me) : json(200, list())));
    await waitFor(() => expect(group()).toBeTruthy());
    shellTarget();
  });

  it('on a loaded lesson', async () => {
    const sha = fixture().lesson.lesson.latest_sha256;
    await mount('/lessons/w01', (url) =>
      url === '/api/me' ? json(200, me) : url === '/api/lessons/w01' ? json(200, fixture().lesson) : url.endsWith(sha) ? json(200, nb()) : json(404, {}),
    );
    await waitFor(() => expect(screen.getByText('Digests', { selector: 'h2' })).toBeTruthy());
    shellTarget();
  });

  it('on a lesson that failed to load', async () => {
    await mount('/lessons/w01', (url) => (url === '/api/me' ? json(200, me) : json(500, { error: 'internal' })));
    await waitFor(() => expect(screen.getByText('The lesson could not be loaded.')).toBeTruthy());
    shellTarget();
  });

  it('on the not-found view', async () => {
    await mount('/no-such-page', (url) => (url === '/api/me' ? json(200, me) : json(404, {})));
    await waitFor(() => expect(screen.getByText(/not found/i)).toBeTruthy());
    shellTarget();
  });

  it('shows a status and a skeleton, not a bare line and no aria-busy, while /api/me loads', async () => {
    await mount('/', () => new Promise<Response>(() => undefined));
    expect(screen.getByRole('status').textContent).toBe('Loading');
    expect(document.querySelector('[aria-busy]')).toBeNull();
    expect(screen.queryByText('Loading…')).toBeNull();
  });

  it('the lesson list loads with the same status pattern', async () => {
    await mount('/', (url) => (url === '/api/me' ? json(200, me) : new Promise<Response>(() => undefined)));
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe('Loading the lessons'));
    expect(document.querySelector('[aria-busy]')).toBeNull();
  });

  it('following the skip link focuses main#content', async () => {
    await mount('/', (url) => (url === '/api/me' ? json(200, me) : json(200, list())));
    await waitFor(() => expect(group()).toBeTruthy());
    fireEvent.click(screen.getByRole('link', { name: 'Skip to content' }));
    await waitFor(() => expect(document.activeElement).toBe(document.getElementById('content')));
  });

  it('marks the unbulleted lesson list as a list', async () => {
    await mount('/', (url) => (url === '/api/me' ? json(200, me) : json(200, list())));
    await waitFor(() => expect(group()).toBeTruthy());
    for (const ul of document.querySelectorAll('.lesson-list ul')) expect(ul.getAttribute('role')).toBe('list');
  });
});
