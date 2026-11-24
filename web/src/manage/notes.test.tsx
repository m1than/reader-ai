// @trace FR-MANAGE-9 INV-MANAGE-3 INV-MANAGE-4 VEC-MANAGE-1
// WEB-MANAGE notes and bookmarks on the lesson page: the bookmark toggle per cell, the Bookmarks entry, the Notes tab
// beside the tutor with add, edit, delete and Undo, both caps, and notes rendered as text only.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { load } from '../app/testing/load';
import { fakeServer, refusal, type FakeServer } from './testkit';

afterEach(cleanup);

const mount = async (server: FakeServer) => {
  const { AppRoot } = await load(() => import('../app/AppRoot'));
  await act(async () => {
    render(<AppRoot initialPath="/lessons/w01" fetch={server.fetch} navigateAway={vi.fn()} />);
  });
  await screen.findByText('Digests', { selector: 'h2' });
};
const notesPanel = async () => {
  await act(async () => fireEvent.click(await screen.findByRole('button', { name: 'Notes' })));
  return screen.findByRole('region', { name: 'Notes' });
};

describe('bookmarks (FR-MANAGE-9)', () => {
  it('toggles a bookmark per cell with aria-pressed', async () => {
    const server = fakeServer();
    await mount(server);
    const cell = document.querySelector('[data-cell-id="w01-explain"]') as HTMLElement;
    const add = await within(cell).findByRole('button', { name: 'Bookmark this cell' });
    expect(add.getAttribute('aria-pressed')).toBe('false');
    await act(async () => fireEvent.click(add));
    const remove = await within(cell).findByRole('button', { name: 'Remove bookmark' });
    expect(remove.getAttribute('aria-pressed')).toBe('true');
    await act(async () => fireEvent.click(remove));
    await waitFor(() =>
      expect(server.writes()).toEqual([
        { method: 'POST', path: '/api/lessons/w01/bookmarks/w01-explain', body: {} },
        { method: 'DELETE', path: '/api/lessons/w01/bookmarks/w01-explain', body: {} },
      ]),
    );
  });

  it('lists the lesson\'s bookmarks in the contents, a stale one with Remove', async () => {
    await mount(fakeServer());
    const contents = await screen.findByRole('navigation', { name: 'Lesson contents' });
    const bookmarks = await within(contents).findByRole('list', { name: 'Bookmarks' });
    expect(within(bookmarks).getByText('No longer in this lesson')).toBeTruthy();
    expect(within(bookmarks).getByRole('button', { name: 'Remove' })).toBeTruthy();
  });

  it('says when the bookmark cap is reached', async () => {
    await mount(fakeServer({ overrides: { 'POST /api/lessons/w01/bookmarks/w01-explain': refusal('limit') } }));
    const cell = document.querySelector('[data-cell-id="w01-explain"]') as HTMLElement;
    await act(async () => fireEvent.click(await within(cell).findByRole('button', { name: 'Bookmark this cell' })));
    expect(await screen.findByText('You have reached the limit of 500 notes (200 bookmarks) for this lesson.')).toBeTruthy();
  });
});

describe('notes (FR-MANAGE-9, INV-MANAGE-3)', () => {
  it('lists notes newest first as plain text, with the cell they are on', async () => {
    await mount(fakeServer());
    const panel = await notesPanel();
    const items = await waitFor(() => {
      const r = within(panel).getAllByRole('listitem');
      if (r.length < 2) throw new Error('not yet');
      return r;
    });
    expect(items[0]!.textContent).toContain('Проверить, как это работает в Raft.');
    expect(within(items[0]!).getByText(/^On: /)).toBeTruthy();
    expect(within(items[1]!).getByText('<b>not bold</b> plain text')).toBeTruthy();
    expect(items[1]!.querySelector('b')).toBeNull();
  });

  it('adds, edits and deletes a note, and Undo posts the same body again', async () => {
    const server = fakeServer();
    await mount(server);
    const panel = await notesPanel();
    const box = within(panel).getByRole('textbox', { name: 'New note' }) as HTMLTextAreaElement;
    expect(box.maxLength).toBe(10000);
    fireEvent.change(box, { target: { value: 'A new note' } });
    await act(async () => fireEvent.click(within(panel).getByRole('button', { name: 'Add note' })));
    const items = await waitFor(() => within(panel).getAllByRole('listitem'));
    const first = items.find((li) => li.textContent?.includes('Проверить'))!;
    fireEvent.click(within(first).getByRole('button', { name: 'Edit' }));
    const edit = within(first).getByRole('textbox', { name: 'Edit note' });
    fireEvent.change(edit, { target: { value: 'Edited.' } });
    await act(async () => fireEvent.click(within(first).getByRole('button', { name: 'Save' })));
    const second = within(panel).getAllByRole('listitem').find((li) => li.textContent?.includes('not bold'))!;
    await act(async () => fireEvent.click(within(second).getByRole('button', { name: 'Delete' })));
    await act(async () => fireEvent.click(await screen.findByRole('button', { name: 'Undo' })));
    await waitFor(() =>
      expect(server.writes()).toEqual([
        { method: 'POST', path: '/api/lessons/w01/notes', body: { body: 'A new note' } },
        { method: 'POST', path: '/api/lessons/w01/notes/n-2', body: { body: 'Edited.' } },
        { method: 'DELETE', path: '/api/lessons/w01/notes/n-1', body: {} },
        { method: 'POST', path: '/api/lessons/w01/notes', body: { body: '<b>not bold</b> plain text' } },
      ]),
    );
  });

  it('says when the note cap is reached', async () => {
    await mount(fakeServer({ overrides: { 'POST /api/lessons/w01/notes': refusal('limit') } }));
    const panel = await notesPanel();
    fireEvent.change(within(panel).getByRole('textbox', { name: 'New note' }), { target: { value: 'One more' } });
    await act(async () => fireEvent.click(within(panel).getByRole('button', { name: 'Add note' })));
    expect(await within(panel).findByText('You have reached the limit of 500 notes (200 bookmarks) for this lesson.')).toBeTruthy();
  });
});
