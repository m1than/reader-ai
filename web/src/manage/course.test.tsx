// @trace FR-MANAGE-3 INV-MANAGE-1 INV-MANAGE-2 INV-MANAGE-4 VEC-MANAGE-1
// WEB-MANAGE course actions on `/` through the app (AppRoot) against a fake API answering from VEC-MANAGE-1: the menu
// per course state, each request's method, path and body, archive and delete with Undo, the Archived disclosure, the
// learner's view and the GET-only request log of loading and keyboard walks.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { load } from '../app/testing/load';
import { fakeServer, refusal, type FakeServer } from './testkit';

afterEach(cleanup);

const mount = async (server: FakeServer, path = '/') => {
  const { AppRoot } = await load(() => import('../app/AppRoot'));
  await act(async () => {
    render(<AppRoot initialPath={path} fetch={server.fetch} navigateAway={vi.fn()} />);
  });
};
const group = async (title: string) =>
  waitFor(() => {
    const h = screen.getAllByRole('heading', { level: 2 }).find((x) => x.textContent === title);
    if (!h) throw new Error(`no group ${title}`);
    return h.closest('section') as HTMLElement;
  });
const openMenu = async (name: string) => {
  const trigger = await screen.findByRole('button', { name: `Actions for ${name}` });
  fireEvent.click(trigger);
  return screen.findByRole('menu');
};
const items = (menu: HTMLElement) => within(menu).getAllByRole('menuitem').map((i) => i.textContent);
const choose = async (menu: HTMLElement, label: string) => {
  await act(async () => {
    fireEvent.click(within(menu).getByRole('menuitem', { name: label }));
  });
};

describe('course menu (FR-MANAGE-3)', () => {
  it('offers each course its items: no Move up on the first, no Move down on the last, no dates without a plan', async () => {
    const server = fakeServer();
    await mount(server);
    await group('Fixture course');
    let menu = await openMenu('Fixture course');
    expect(items(menu)).toEqual(['Rename', 'Edit description…', 'Change dates and pace…', 'Move down', 'Archive', 'Delete…']);
    fireEvent.keyDown(menu, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());
    menu = await openMenu('Another course');
    expect(items(menu)).toEqual(['Rename', 'Edit description…', 'Move up', 'Archive', 'Delete…']);
  });

  it('renames in place with the version from the last read', async () => {
    const server = fakeServer();
    await mount(server);
    await choose(await openMenu('Fixture course'), 'Rename');
    const box = (await screen.findByRole('textbox', { name: 'Course title' })) as HTMLInputElement;
    expect(box.value).toBe('Fixture course');
    expect(screen.getByText('Lessons compiled before keep the old name in their text until they are compiled again.')).toBeTruthy();
    fireEvent.change(box, { target: { value: 'Renamed course' } });
    await act(async () => {
      fireEvent.keyDown(box, { key: 'Enter' });
    });
    await waitFor(() => expect(server.writes()).toEqual([{ method: 'POST', path: '/api/courses/c-fixture', body: { version: 3, title: 'Renamed course' } }]));
  });

  it('edits the description in a dialog of at most 2,000 characters', async () => {
    const server = fakeServer();
    await mount(server);
    await choose(await openMenu('Fixture course'), 'Edit description…');
    const dialog = await screen.findByRole('dialog');
    const area = within(dialog).getByRole('textbox', { name: 'Description' }) as HTMLTextAreaElement;
    expect(area.value).toBe('A course for the tests.');
    expect(area.maxLength).toBe(2000);
    fireEvent.change(area, { target: { value: 'New words.' } });
    await act(async () => fireEvent.click(within(dialog).getByRole('button', { name: 'Save' })));
    await waitFor(() => expect(server.writes()).toEqual([{ method: 'POST', path: '/api/courses/c-fixture', body: { version: 3, description: 'New words.' } }]));
  });

  it('changes dates and pace through the course\'s study plan', async () => {
    const server = fakeServer();
    await mount(server);
    await choose(await openMenu('Fixture course'), 'Change dates and pace…');
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('This changes weeks and dates. Your lesson order and titles stay; lessons you have not started may get new page ranges.')).toBeTruthy();
    await waitFor(() => expect(server.to('/api/plans?')).toEqual([{ method: 'GET', path: '/api/plans?course_id=c-fixture', body: undefined }]));
    const weeks = (await within(dialog).findByRole('spinbutton', { name: 'Total weeks' })) as HTMLInputElement;
    expect(weeks.value).toBe('4');
    fireEvent.change(weeks, { target: { value: '6' } });
    await waitFor(() => expect(server.to('/api/plans/preview').length).toBeGreaterThan(0));
    const confirm = within(dialog).getByRole('button', { name: 'Apply the new plan' }) as HTMLButtonElement;
    await waitFor(() => expect(confirm.disabled).toBe(false));
    await act(async () => fireEvent.click(confirm));
    await waitFor(() => expect(server.to('/api/plans/confirm')).toHaveLength(1));
    expect(server.to('/api/plans/confirm')[0]!.body).toMatchObject({ material_id: 'up-aaaaaaaaaaaa', total_weeks: 6, minutes_per_day: 30, plan_sha256: '7777777777777777777777777777777777777777777777777777777777777777' });
  });

  it('moves a course down by sending the whole order', async () => {
    const server = fakeServer();
    await mount(server);
    await choose(await openMenu('Fixture course'), 'Move down');
    await waitFor(() => expect(server.writes()).toEqual([{ method: 'POST', path: '/api/courses/order', body: { course_ids: ['c-other', 'c-fixture'] } }]));
  });
});

describe('archive and delete with Undo (FR-MANAGE-3, INV-MANAGE-4)', () => {
  it('archives, then Undo puts the course back where it was', async () => {
    const server = fakeServer();
    await mount(server);
    await choose(await openMenu('Fixture course'), 'Archive');
    await waitFor(() => expect(server.writes()).toEqual([{ method: 'POST', path: '/api/courses/c-fixture/archive', body: {} }]));
    expect(await screen.findByText('Course archived.')).toBeTruthy();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Undo' })));
    await waitFor(() => expect(server.writes().at(-1)).toEqual({ method: 'POST', path: '/api/courses/c-fixture/unarchive', body: { at: 'previous' } }));
    expect(await screen.findByText('Course restored.')).toBeTruthy();
  });

  it('refuses to archive while a compile runs', async () => {
    const server = fakeServer({ overrides: { 'POST /api/courses/c-fixture/archive': refusal('compile_running') } });
    await mount(server);
    await choose(await openMenu('Fixture course'), 'Archive');
    expect(await screen.findByText('A compile of this course is running. Cancel it first.')).toBeTruthy();
  });

  it('deletes after the impact and typed confirmation, then Undo restores at the old position', async () => {
    const server = fakeServer();
    await mount(server);
    await choose(await openMenu('Fixture course'), 'Delete…');
    const dialog = await screen.findByRole('dialog', { name: 'Delete the course “Fixture course”?' });
    await waitFor(() => expect(within(dialog).getByText(/3 lessons, 5 compiled revisions, 7 tutor questions and 2 compile runs are hidden\./)).toBeTruthy());
    expect(within(dialog).getByText(/Spend records stay in the ledger\. You can restore the course from Settings → Data\./)).toBeTruthy();
    expect(server.writes()).toEqual([]);
    const del = within(dialog).getByRole('button', { name: 'Delete' }) as HTMLButtonElement;
    expect(del.disabled).toBe(true);
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Type Fixture course to confirm' }), { target: { value: 'Fixture course' } });
    await act(async () => fireEvent.click(del));
    await waitFor(() => expect(server.writes()).toEqual([{ method: 'DELETE', path: '/api/courses/c-fixture', body: { confirm: 'Fixture course' } }]));
    expect(await screen.findByText('Course deleted.')).toBeTruthy();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Undo' })));
    await waitFor(() => expect(server.writes().at(-1)).toEqual({ method: 'POST', path: '/api/courses/c-fixture/restore', body: { at: 'previous' } }));
  });

  it('keeps the delete dialog open on compile_running with Cancel compile', async () => {
    const server = fakeServer({ overrides: { 'DELETE /api/courses/c-fixture': refusal('compile_running') } });
    await mount(server);
    await choose(await openMenu('Fixture course'), 'Delete…');
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Type Fixture course to confirm' }), { target: { value: 'Fixture course' } });
    await act(async () => fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' })));
    expect(await within(dialog).findByText('A compile of this course is running. Cancel it first.')).toBeTruthy();
    expect(within(dialog).getByRole('button', { name: 'Cancel compile' })).toBeTruthy();
  });

  it('lists archived courses under a closed "Archived (n)" disclosure with Unarchive and Delete…', async () => {
    const server = fakeServer();
    await mount(server);
    const toggle = await screen.findByRole('button', { name: 'Archived (1)' });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText('Archived notes')).toBeNull();
    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    const row = (await screen.findByText('Archived notes')).closest('li') as HTMLElement;
    expect(within(row).getByRole('button', { name: 'Delete…' })).toBeTruthy();
    await act(async () => fireEvent.click(within(row).getByRole('button', { name: 'Unarchive' })));
    await waitFor(() => expect(server.writes()).toEqual([{ method: 'POST', path: '/api/courses/c-archived/unarchive', body: { at: 'first' } }]));
  });
});

describe('no state change without a click (INV-MANAGE-1)', () => {
  it('sends GETs only while loading, opening menus, walking with the keyboard and expanding the archive', async () => {
    const server = fakeServer();
    await mount(server);
    const menu = await openMenu('Fixture course');
    fireEvent.keyDown(menu, { key: 'ArrowDown' });
    fireEvent.keyDown(menu, { key: 'ArrowDown' });
    fireEvent.keyDown(menu, { key: 'Escape' });
    fireEvent.click(await screen.findByRole('button', { name: 'Archived (1)' }));
    await choose(await openMenu('Fixture course'), 'Delete…');
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(server.writes()).toEqual([]);
    expect(server.log.length).toBeGreaterThan(2);
  });
});

describe('learner (INV-MANAGE-2)', () => {
  it('renders no course control and calls no course route', async () => {
    const server = fakeServer({ role: 'learner' });
    await mount(server);
    await group('Fixture course');
    expect(screen.queryByRole('button', { name: /^Actions for (Fixture|Another) course$/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /^Archived/ })).toBeNull();
    expect(server.to('/api/courses', '/api/materials', '/api/auth/users', '/api/settings', '/api/audit')).toEqual([]);
    expect(server.writes()).toEqual([]);
  });
});
