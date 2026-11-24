// @trace FR-MANAGE-5 INV-MANAGE-4 VEC-MANAGE-1
// WEB-MANAGE "Your sources" on /import: the list, rename, delete of an unused source with typed confirmation, the
// in-use dialog with its cascade, and a reader.yaml source never imported (no actions).
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { load } from '../app/testing/load';
import { fakeServer, type FakeServer } from './testkit';

afterEach(cleanup);

const mount = async (server: FakeServer) => {
  const { AppRoot } = await load(() => import('../app/AppRoot'));
  await act(async () => {
    render(<AppRoot initialPath="/import" fetch={server.fetch} navigateAway={vi.fn()} />);
  });
};
const section = () => screen.findByRole('region', { name: 'Manage your sources' });
const rowOf = async (title: string) => {
  const s = await section();
  return waitFor(() => {
    const li = within(s).getAllByRole('listitem').find((x) => x.querySelector('.source-title')?.textContent === title);
    if (!li) throw new Error(`no ${title}`);
    return li;
  });
};
const choose = async (row: HTMLElement, name: string, label: string) => {
  fireEvent.click(within(row).getByRole('button', { name: `Actions for ${name}` }));
  const menu = await screen.findByRole('menu');
  await act(async () => fireEvent.click(within(menu).getByRole('menuitem', { name: label })));
};

describe('your sources (FR-MANAGE-5)', () => {
  it('lists each source with pages, origin and the courses that use it', async () => {
    await mount(fakeServer());
    const used = await rowOf('Distributed systems notes');
    expect(within(used).getByText(/120 pages/)).toBeTruthy();
    expect(within(used).getByText('Uploaded')).toBeTruthy();
    expect(within(used).getByRole('link', { name: 'Fixture course' }).getAttribute('href')).toBe('/#course-c-fixture');
    const declared = await rowOf('sre-book');
    expect(within(declared).getByText('reader.yaml')).toBeTruthy();
    expect(within(declared).queryByRole('button', { name: /^Actions for/ })).toBeNull();
  });

  it('renames an imported source with its version', async () => {
    const server = fakeServer();
    await mount(server);
    await choose(await rowOf('A short book'), 'A short book', 'Rename');
    const box = (await screen.findByRole('textbox', { name: 'Source title' })) as HTMLInputElement;
    fireEvent.change(box, { target: { value: 'A shorter book' } });
    await act(async () => fireEvent.keyDown(box, { key: 'Enter' }));
    await waitFor(() => expect(server.writes()).toEqual([{ method: 'POST', path: '/api/materials/up-bbbbbbbbbbbb', body: { version: 2, title: 'A shorter book' } }]));
  });

  it('deletes an unused source after typed confirmation', async () => {
    const server = fakeServer();
    await mount(server);
    await choose(await rowOf('A short book'), 'A short book', 'Delete…');
    const dialog = await screen.findByRole('dialog', { name: 'Delete the source “A short book”?' });
    const del = within(dialog).getByRole('button', { name: 'Delete' }) as HTMLButtonElement;
    expect(del.disabled).toBe(true);
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Type A short book to confirm' }), { target: { value: 'A short book' } });
    await act(async () => fireEvent.click(del));
    await waitFor(() => expect(server.writes()).toEqual([{ method: 'DELETE', path: '/api/materials/up-bbbbbbbbbbbb', body: { confirm: 'A short book' } }]));
    expect(await screen.findByText('Source deleted.')).toBeTruthy();
  });

  it('names the courses that use a source and deletes them with it only through the cascade choice', async () => {
    const server = fakeServer();
    await mount(server);
    await choose(await rowOf('Distributed systems notes'), 'Distributed systems notes', 'Delete…');
    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('Fixture course')).toBeTruthy());
    const cascade = within(dialog).getByRole('button', { name: 'Delete the source and its 1 course' }) as HTMLButtonElement;
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeTruthy();
    expect(cascade.disabled).toBe(true);
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Type Distributed systems notes to confirm' }), { target: { value: 'Distributed systems notes' } });
    await act(async () => fireEvent.click(cascade));
    await waitFor(() =>
      expect(server.writes()).toEqual([{ method: 'DELETE', path: '/api/materials/up-aaaaaaaaaaaa', body: { confirm: 'Distributed systems notes', cascade: true } }]),
    );
  });
});
