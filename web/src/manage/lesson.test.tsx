// @trace FR-MANAGE-4 INV-MANAGE-2 INV-MANAGE-4 VEC-MANAGE-1
// WEB-MANAGE lesson actions on the list rows and the lesson page's "More lesson actions" menu: the menu for owner
// and learner in each lesson state, each request, the download link and its file name, revisions and revert, the
// visible lesson number, skipped and done rows. Synthetic VEC-MANAGE-1 bodies and fixture W01 only.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { load } from '../app/testing/load';
import { fakeServer, fixture, refusal, type FakeServer } from './testkit';

afterEach(cleanup);

interface Model {
  slugify: (title: string) => string;
  downloadName: (position: number, title: string) => string;
}
const MODEL = './model';
const model = () => load<Partial<Model>>(() => import(/* @vite-ignore */ MODEL) as Promise<Model>);

const mount = async (server: FakeServer, path = '/') => {
  const { AppRoot } = await load(() => import('../app/AppRoot'));
  await act(async () => {
    render(<AppRoot initialPath={path} fetch={server.fetch} navigateAway={vi.fn()} />);
  });
};
const W01 = 'Fixture lesson W01: one cell of every kind';
const W02 = 'Fixture lesson W02: not compiled yet';
const W04 = 'Распределённый консенсус';
const rowOf = async (title: string) =>
  waitFor(() => {
    const groupRow = [...document.querySelectorAll('section.course-group li')].find((li) => li.textContent?.includes(title));
    if (!groupRow) throw new Error(`no row ${title}`);
    return groupRow as HTMLElement;
  });
const openRowMenu = async (title: string) => {
  const row = await rowOf(title);
  fireEvent.click(within(row).getByRole('button', { name: `Actions for ${title}` }));
  return screen.findByRole('menu');
};
const labels = (menu: HTMLElement) => within(menu).getAllByRole('menuitem').map((i) => i.textContent);
const choose = async (menu: HTMLElement, label: string) => {
  await act(async () => {
    fireEvent.click(within(menu).getByRole('menuitem', { name: label }));
  });
};

describe('file name of a download (FR-MANAGE-4)', () => {
  it('slugs a title: NFC, separators and whitespace runs to -, runs collapsed, letters kept, 120 code points, else lesson', async () => {
    const { slugify, downloadName } = await model();
    expect(slugify).toBeTypeOf('function');
    expect(slugify?.('Привет, мир')).toBe('Привет,-мир');
    expect(slugify?.('a/b.c')).toBe('a-b-c');
    expect(slugify?.('a\\b:c\u0007d')).toBe('a-b-c-d');
    expect(slugify?.('  ./:  ')).toBe('lesson');
    expect(slugify?.('...')).toBe('lesson');
    expect([...(slugify?.('я'.repeat(200)) ?? '')]).toHaveLength(120);
    expect(slugify?.('йод')).toBe('йод');
    expect(downloadName?.(1, W01)).toBe('1-Fixture-lesson-W01-one-cell-of-every-kind.ipynb');
  });
});

describe('rows (FR-MANAGE-4)', () => {
  it('numbers the visible lessons 1..n, marks done with hidden text and mutes a skipped row with its chip', async () => {
    await mount(fakeServer());
    const w04 = await rowOf(W04);
    expect(w04.querySelector('.lesson-ordinal')?.textContent).toBe('3');
    expect(w04.classList.contains('skipped')).toBe(true);
    expect(within(w04).getByText('Skipped')).toBeTruthy();
    const w01 = await rowOf(W01);
    expect(w01.querySelector('.lesson-ordinal')?.textContent).toBe('1');
    expect(within(w01).getByText('done').classList.contains('visually-hidden')).toBe(true);
    // A skipped lesson is never the Continue block's target.
    const cont = document.querySelector('section.continue') as HTMLElement;
    expect(within(cont).queryByText(W04)).toBeNull();
  });
});

describe('row menu (FR-MANAGE-4)', () => {
  it('gives the owner every item on a published lesson, with the download link', async () => {
    await mount(fakeServer());
    const menu = await openRowMenu(W01);
    expect(labels(menu)).toEqual(['Mark as not done', 'Clear tutor history…', 'Download .ipynb', 'Rename', 'Move down', 'Skip', 'Revisions…', 'Compile history…', 'Delete…']);
    const link = within(menu).getByRole('menuitem', { name: 'Download .ipynb' });
    expect(link.getAttribute('href')).toBe('/api/lessons/w01/revisions/1111111111111111111111111111111111111111111111111111111111111111');
    expect(link.getAttribute('download')).toBe('1-Fixture-lesson-W01-one-cell-of-every-kind.ipynb');
  });

  it('gives a never-compiled lesson no download and no revisions, and a skipped one Unskip', async () => {
    await mount(fakeServer());
    let menu = await openRowMenu(W02);
    expect(labels(menu)).toEqual(['Mark as done', 'Clear tutor history…', 'Rename', 'Move up', 'Move down', 'Skip', 'Compile history…', 'Delete…']);
    fireEvent.keyDown(menu, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());
    menu = await openRowMenu(W04);
    expect(labels(menu)).toContain('Unskip');
    expect(labels(menu)).not.toContain('Move down');
  });

  it('offers Cancel compile… while a run is open and sends the cancel after its dialog', async () => {
    const fx = fixture();
    const run = { run_id: 'run-w02', lesson_id: 'w02', state: 'running', cap_usd: '8.00', steps: [{ step: 1, stage: 'draft', status: 'running' }], spent_usd: '0.10', refusals: [], latest_sha256: null, error: null };
    const server = fakeServer({ fx, overrides: { 'GET /api/compiles': { status: 200, body: { runs: [run] } } } });
    await mount(server);
    const menu = await openRowMenu(W02);
    await waitFor(() => expect(labels(menu)).toContain('Cancel compile…'));
    await choose(menu, 'Cancel compile…');
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('The step in progress finishes and is paid for; nothing after it runs.')).toBeTruthy();
    await act(async () => fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel compile' })));
    await waitFor(() => expect(server.writes()).toEqual([{ method: 'POST', path: '/api/compiles/run-w02/cancel', body: {} }]));
  });

  it('sends each lesson request', async () => {
    const server = fakeServer();
    await mount(server);
    await choose(await openRowMenu(W02), 'Mark as done');
    await choose(await openRowMenu(W01), 'Skip');
    await choose(await openRowMenu(W02), 'Move up');
    await waitFor(() =>
      expect(server.writes()).toEqual([
        { method: 'POST', path: '/api/lessons/w02/progress', body: { done: true } },
        { method: 'POST', path: '/api/lessons/w01/skip', body: {} },
        { method: 'POST', path: '/api/courses/c-fixture/lessons/order', body: { lesson_ids: ['w02', 'w01', 'w04'] } },
      ]),
    );
  });

  it('renames with the warning about the next compile', async () => {
    const server = fakeServer();
    await mount(server);
    await choose(await openRowMenu(W01), 'Rename');
    const box = (await screen.findByRole('textbox', { name: 'Lesson title' })) as HTMLInputElement;
    expect(screen.getByText("Renaming changes the lesson's text, so the next compile starts from scratch (about a full compile's cost).")).toBeTruthy();
    fireEvent.change(box, { target: { value: 'Digests' } });
    await act(async () => fireEvent.keyDown(box, { key: 'Enter' }));
    await waitFor(() => expect(server.writes()).toEqual([{ method: 'POST', path: '/api/lessons/w01', body: { version: 2, title: 'Digests' } }]));
  });

  it('deletes after a dialog without typed confirmation, with Undo', async () => {
    const server = fakeServer();
    await mount(server);
    await choose(await openRowMenu(W01), 'Delete…');
    const dialog = await screen.findByRole('dialog', { name: `Delete the lesson “${W01}”?` });
    expect(within(dialog).queryByRole('textbox')).toBeNull();
    await act(async () => fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' })));
    await waitFor(() => expect(server.writes()).toEqual([{ method: 'DELETE', path: '/api/lessons/w01', body: {} }]));
    await act(async () => fireEvent.click(await screen.findByRole('button', { name: 'Undo' })));
    await waitFor(() => expect(server.writes().at(-1)).toEqual({ method: 'POST', path: '/api/lessons/w01/restore', body: {} }));
  });

  it('clears the tutor history after a dialog that says it cannot be undone', async () => {
    const server = fakeServer();
    await mount(server);
    await choose(await openRowMenu(W01), 'Clear tutor history…');
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/This cannot be undone\./)).toBeTruthy();
    await act(async () => fireEvent.click(within(dialog).getByRole('button', { name: 'Clear' })));
    await waitFor(() => expect(server.writes()).toEqual([{ method: 'POST', path: '/api/lessons/w01/questions/clear', body: {} }]));
  });

  it('lists the revisions, marks the current one, and reverts after its confirm', async () => {
    const server = fakeServer();
    await mount(server);
    await choose(await openRowMenu(W01), 'Revisions…');
    const dialog = await screen.findByRole('dialog', { name: 'Revisions' });
    const rows = await waitFor(() => {
      const r = within(dialog).getAllByRole('listitem');
      if (r.length < 2) throw new Error('not yet');
      return r;
    });
    expect(within(rows[0]!).getByText('Current')).toBeTruthy();
    expect(within(rows[0]!).queryByRole('button', { name: 'Revert' })).toBeNull();
    fireEvent.click(within(rows[1]!).getByRole('button', { name: 'Revert' }));
    expect(await screen.findByText('Make this revision current? The newer revisions stay and you can switch back.')).toBeTruthy();
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Make current' })));
    await waitFor(() => expect(server.writes()).toEqual([{ method: 'POST', path: '/api/lessons/w01/revert', body: { sha256: '5555555555555555555555555555555555555555555555555555555555555555' } }]));
  });

  it('explains a revert refused for removed notes cells', async () => {
    const server = fakeServer({ overrides: { 'POST /api/lessons/w01/revert': refusal('notes_cell_removed') } });
    await mount(server);
    await choose(await openRowMenu(W01), 'Revisions…');
    const dialog = await screen.findByRole('dialog', { name: 'Revisions' });
    const rows = await waitFor(() => {
      const r = within(dialog).getAllByRole('listitem');
      if (r.length < 2) throw new Error('not yet');
      return r;
    });
    fireEvent.click(within(rows[1]!).getByRole('button', { name: 'Revert' }));
    await act(async () => fireEvent.click(await screen.findByRole('button', { name: 'Make current' })));
    expect(await screen.findByText('This revision lacks notes cells that the current one has, so it cannot be made current.')).toBeTruthy();
  });

  it('shows the compile history with state, spend and finish time', async () => {
    await mount(fakeServer());
    await choose(await openRowMenu(W01), 'Compile history…');
    const dialog = await screen.findByRole('dialog', { name: 'Compile history' });
    await waitFor(() => expect(within(dialog).getAllByRole('listitem')).toHaveLength(2));
    expect(within(dialog).getByText(/\$2\.10/)).toBeTruthy();
    expect(within(dialog).getByText(/cancelled|Cancelled/)).toBeTruthy();
  });
});

describe('the lesson page menu (FR-MANAGE-4)', () => {
  it('keeps Reset kernel… and Keyboard shortcuts first, then the lesson items', async () => {
    await mount(fakeServer(), '/lessons/w01');
    const more = await screen.findByRole('button', { name: 'More lesson actions' });
    fireEvent.click(more);
    const menu = await screen.findByRole('menu');
    await waitFor(() => expect(labels(menu)).toContain('Delete…'));
    const l = labels(menu);
    expect(l.slice(0, 2)).toEqual(['Reset kernel…', 'Keyboard shortcuts']);
    expect(l).toContain('Download .ipynb');
    expect(l).toContain('Revisions…');
  });

  it('gives a learner Mark as done and Clear tutor history… only', async () => {
    const server = fakeServer({ role: 'learner' });
    await mount(server, '/lessons/w01');
    fireEvent.click(await screen.findByRole('button', { name: 'More lesson actions' }));
    const menu = await screen.findByRole('menu');
    await waitFor(() => expect(labels(menu)).toContain('Clear tutor history…'));
    expect(labels(menu)).toEqual(['Reset kernel…', 'Keyboard shortcuts', 'Mark as not done', 'Clear tutor history…']);
    expect(server.to('/api/courses', '/api/compiles')).toEqual([]);
  });

  it('reads "Reverted to an earlier revision" while the current revision is not the run\'s', async () => {
    const fx = fixture();
    const run = { run_id: 'run-2', lesson_id: 'w01', state: 'converged', cap_usd: '8.00', steps: [{ step: 1, stage: 'draft' }], spent_usd: '2.10', refusals: [], latest_sha256: '1111111111111111111111111111111111111111111111111111111111111111', final_sha256: '1111111111111111111111111111111111111111111111111111111111111111', error: null };
    fx.lessons.lessons[0]!.latest_sha256 = '5555555555555555555555555555555555555555555555555555555555555555';
    const server = fakeServer({ fx, overrides: { 'GET /api/lessons/w01/compile': { status: 200, body: run } } });
    await mount(server, '/lessons/w01');
    expect(await screen.findByText('Reverted to an earlier revision')).toBeTruthy();
  });
});

describe('learner rows (INV-MANAGE-2)', () => {
  it('shows a learner only the reader items on a row', async () => {
    const server = fakeServer({ role: 'learner' });
    await mount(server);
    const menu = await openRowMenu(W01);
    expect(labels(menu)).toEqual(['Mark as not done', 'Clear tutor history…']);
    expect(server.writes()).toEqual([]);
  });
});
