// @trace FR-MANAGE-1 FR-MANAGE-2 INV-MANAGE-3 VEC-MANAGE-1
// WEB-MANAGE primitives: ActionsMenu, RenameField, ConfirmDialog with typed confirmation, UndoToast, the shared
// response mapping, and the text-only rule. Synthetic VEC-MANAGE-1 bodies only.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { createRef, type ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { webRoot } from '../app/testing/fixtures';
import { load } from '../app/testing/load';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

type Item = { label: string; onSelect: () => void; danger?: boolean; ownerOnly?: boolean; hidden?: boolean };
interface Primitives {
  ActionsMenu: (p: { label: string; items: Item[]; owner?: boolean }) => ReactNode;
  RenameField: (p: { value: string; label: string; onSave: (v: string) => Promise<void>; onCancel: () => void; hint?: string; max?: number; returnFocus?: { current: HTMLElement | null } }) => ReactNode;
  ConfirmDialog: (p: { open: boolean; title: string; children?: ReactNode; confirmLabel: string; typed?: string; onConfirm: (typed: string | undefined) => Promise<void>; onClose: () => void; alert?: string | null }) => ReactNode;
  UndoToast: (p: { text: string; restoredText: string; onUndo: () => Promise<void>; onClose: () => void; ms?: number }) => ReactNode;
}
interface Model {
  cleanTitle: (v: string, max?: number) => { ok: true; value: string } | { ok: false };
  confirmTarget: (title: string) => string;
  matchesConfirm: (typed: string, title: string) => boolean;
  problem: (code: string, object: string) => { text: string; signIn?: string; refetch?: boolean };
}
const PRIM = './primitives';
const MODEL = './model';
const prim = () => load<Partial<Primitives>>(() => import(/* @vite-ignore */ PRIM) as Promise<Primitives>);
const model = () => load<Partial<Model>>(() => import(/* @vite-ignore */ MODEL) as Promise<Model>);

describe('titles and typed confirmation (FR-MANAGE-1, FR-MANAGE-2)', () => {
  it('trims and NFC-normalises a title of 1..200 characters without control characters', async () => {
    const { cleanTitle } = await model();
    expect(cleanTitle).toBeTypeOf('function');
    expect(cleanTitle?.('  Курс  ')).toEqual({ ok: true, value: 'Курс' });
    expect(cleanTitle?.('й')).toEqual({ ok: true, value: 'й' });
    expect(cleanTitle?.('   ')).toEqual({ ok: false });
    expect(cleanTitle?.('x'.repeat(201))).toEqual({ ok: false });
    expect(cleanTitle?.('x'.repeat(200))).toEqual({ ok: true, value: 'x'.repeat(200) });
    expect(cleanTitle?.('a\u0007b')).toEqual({ ok: false });
    expect(cleanTitle?.('x'.repeat(65), 64)).toEqual({ ok: false });
  });

  it('asks for the first 40 code points of the title, or all of it, compared after trim and NFC', async () => {
    const { confirmTarget, matchesConfirm } = await model();
    expect(confirmTarget?.('Fixture course')).toBe('Fixture course');
    const long = 'A'.repeat(30) + 'Б'.repeat(30);
    expect(confirmTarget?.(long)).toBe('A'.repeat(30) + 'Б'.repeat(10));
    expect(matchesConfirm?.('A'.repeat(30) + 'Б'.repeat(10), long)).toBe(true);
    expect(matchesConfirm?.(long, long)).toBe(false);
    expect(matchesConfirm?.('Fixture course ', 'Fixture course')).toBe(true);
    expect(matchesConfirm?.('fixture course', 'Fixture course')).toBe(false);
    expect(matchesConfirm?.('Краткий курс'.normalize('NFD'), 'Краткий курс')).toBe(true);
  });

  it('cuts the NFC title to 40 code points before trimming (the server rule, FR-MANAGE-2 vector)', async () => {
    const { confirmTarget, matchesConfirm } = await model();
    const title = 'a'.repeat(39) + ' ' + 'tail words';
    expect(confirmTarget?.(title)).toBe('a'.repeat(39));
    expect(matchesConfirm?.('a'.repeat(39), title)).toBe(true);
    expect(matchesConfirm?.('a'.repeat(39) + ' t', title)).toBe(false);
    // Leading white space in a stored title counts towards the 40 code points, then goes.
    expect(confirmTarget?.('  ' + 'b'.repeat(50))).toBe('b'.repeat(38));
  });

  it('matches nothing when the target is empty, the empty input included (FR-MANAGE-2)', async () => {
    const { confirmTarget, matchesConfirm } = await model();
    for (const title of ['', '   ', ' '.repeat(40) + 'after forty']) {
      expect(confirmTarget?.(title)).toBe('');
      for (const typed of ['', '  ', 'after forty']) expect(matchesConfirm?.(typed, title)).toBe(false);
    }
  });
});

describe('response mapping (FR-MANAGE-1)', () => {
  it('maps each kind of answer to fixed copy, never server text', async () => {
    const { problem } = await model();
    expect(problem?.('reauth_required', 'course')).toEqual({ text: 'Sign in again to do this (required every 10 minutes for account actions).', signIn: '/api/auth/signin?next=/settings' });
    expect(problem?.('not_found', 'course')).toEqual({ text: 'This course no longer exists.', refetch: true });
    expect(problem?.('network', 'lesson').text).toBe('Could not reach reader-ai. Try again.');
    expect(problem?.('http_500', 'lesson').text).toBe('Could not reach reader-ai. Try again.');
    expect(problem?.('stale', 'course').text).toBe('Changed elsewhere. Reload to see the new title.');
    expect(problem?.('title_taken', 'course').text).toBe('Another course already has this name.');
    expect(problem?.('compile_running', 'course').text).toBe('A compile of this course is running. Cancel it first.');
  });
});

describe('ActionsMenu (FR-MANAGE-1)', () => {
  it('names its trigger after the object and renders items in order, the danger item last after a separator', async () => {
    const { ActionsMenu } = await prim();
    expect(ActionsMenu).toBeTypeOf('function');
    if (!ActionsMenu) return;
    const select = vi.fn();
    render(
      <ActionsMenu
        label="Fixture course"
        owner={false}
        items={[
          { label: 'Delete…', onSelect: select, danger: true },
          { label: 'Rename', onSelect: select, ownerOnly: true },
          { label: 'Mark as done', onSelect: select },
          { label: 'Hidden', onSelect: select, hidden: true },
          { label: 'Clear tutor history…', onSelect: select },
        ]}
      />,
    );
    const trigger = screen.getByRole('button', { name: 'Actions for Fixture course' });
    fireEvent.click(trigger);
    const menu = await screen.findByRole('menu');
    const items = within(menu).getAllByRole('menuitem');
    expect(items.map((i) => i.textContent)).toEqual(['Mark as done', 'Clear tutor history…', 'Delete…']);
    expect(menu.querySelector('[role="separator"]')).toBeTruthy();
    fireEvent.click(items[0]!);
    expect(select).toHaveBeenCalledTimes(1);
  });
});

describe('RenameField (FR-MANAGE-1)', () => {
  const setup = async (onSave: (v: string) => Promise<void>) => {
    const { RenameField } = await prim();
    expect(RenameField).toBeTypeOf('function');
    const onCancel = vi.fn();
    const back = createRef<HTMLButtonElement>();
    render(
      <>
        <button ref={back} type="button">
          trigger
        </button>
        {RenameField ? <RenameField value="Fixture course" label="Course title" onSave={onSave} onCancel={onCancel} returnFocus={back} /> : null}
      </>,
    );
    return { box: screen.getByRole('textbox', { name: 'Course title' }) as HTMLInputElement, onCancel, back };
  };

  it('saves the trimmed NFC value on Enter, and sends nothing for an unchanged or invalid value', async () => {
    const onSave = vi.fn(async () => undefined);
    const { box } = await setup(onSave);
    expect(box.value).toBe('Fixture course');
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(onSave).not.toHaveBeenCalled();
    fireEvent.change(box, { target: { value: '   ' } });
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(await screen.findByText('Use 1 to 200 characters.')).toBeTruthy();
    expect(onSave).not.toHaveBeenCalled();
    fireEvent.change(box, { target: { value: '  Новый курс  ' } });
    await act(async () => {
      fireEvent.keyDown(box, { key: 'Enter' });
    });
    expect(onSave).toHaveBeenCalledWith('Новый курс');
  });

  it('cancels on Escape and returns focus to the menu trigger', async () => {
    const onSave = vi.fn(async () => undefined);
    const { box, onCancel, back } = await setup(onSave);
    fireEvent.change(box, { target: { value: 'Other' } });
    fireEvent.keyDown(box, { key: 'Escape' });
    expect(onCancel).toHaveBeenCalled();
    expect(onSave).not.toHaveBeenCalled();
    await waitFor(() => expect(document.activeElement).toBe(back.current));
  });

  it('is read-only while saving and shows the stale refusal with Reload', async () => {
    const { ApiError } = await import('../app/api');
    let reject: (e: unknown) => void = () => undefined;
    const onSave = vi.fn(() => new Promise<void>((_, r) => (reject = r)));
    const { box } = await setup(onSave);
    fireEvent.change(box, { target: { value: 'Renamed course' } });
    fireEvent.keyDown(box, { key: 'Enter' });
    await waitFor(() => expect(box.readOnly).toBe(true));
    await act(async () => reject(new ApiError(409, 'stale')));
    expect(await screen.findByText('Changed elsewhere. Reload to see the new title.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Reload' })).toBeTruthy();
  });
});

describe('ConfirmDialog (FR-MANAGE-2)', () => {
  it('focuses Cancel, keeps the danger button disabled until the typed text matches, and sends that text', async () => {
    const { ConfirmDialog } = await prim();
    expect(ConfirmDialog).toBeTypeOf('function');
    if (!ConfirmDialog) return;
    const onConfirm = vi.fn(async () => undefined);
    const onClose = vi.fn();
    render(
      <ConfirmDialog open title="Delete the course “Fixture course”?" confirmLabel="Delete" typed="Fixture course" onConfirm={onConfirm} onClose={onClose}>
        <p>3 lessons are hidden.</p>
      </ConfirmDialog>,
    );
    const dialog = await screen.findByRole('dialog', { name: 'Delete the course “Fixture course”?' });
    await waitFor(() => expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: 'Cancel' })));
    const input = within(dialog).getByRole('textbox', { name: 'Type Fixture course to confirm' });
    const del = within(dialog).getByRole('button', { name: 'Delete' }) as HTMLButtonElement;
    expect(del.disabled).toBe(true);
    fireEvent.change(input, { target: { value: 'fixture course' } });
    expect(del.disabled).toBe(true);
    fireEvent.change(input, { target: { value: 'Fixture course ' } });
    expect(del.disabled).toBe(false);
    expect(onConfirm).not.toHaveBeenCalled();
    await act(async () => fireEvent.click(del));
    expect(onConfirm).toHaveBeenCalledWith('Fixture course');
  });

  it('enables on the vector title’s 39-letter target and sends the normalised input', async () => {
    const { ConfirmDialog } = await prim();
    if (!ConfirmDialog) return expect(ConfirmDialog).toBeTypeOf('function');
    const onConfirm = vi.fn(async () => undefined);
    const title = 'a'.repeat(39) + ' ' + 'tail words';
    render(<ConfirmDialog open title="Delete the course?" confirmLabel="Delete" typed={title} onConfirm={onConfirm} onClose={vi.fn()} />);
    const dialog = await screen.findByRole('dialog');
    const input = within(dialog).getByRole('textbox', { name: `Type ${'a'.repeat(39)} to confirm` });
    const del = within(dialog).getByRole('button', { name: 'Delete' }) as HTMLButtonElement;
    fireEvent.change(input, { target: { value: 'a'.repeat(39) + ' t' } });
    expect(del.disabled).toBe(true);
    fireEvent.change(input, { target: { value: '  ' + 'a'.repeat(39) + '  ' } });
    expect(del.disabled).toBe(false);
    await act(async () => fireEvent.click(del));
    expect(onConfirm).toHaveBeenCalledWith('a'.repeat(39));
  });

  it('keeps the button disabled for an empty target, whatever is typed', async () => {
    const { ConfirmDialog } = await prim();
    if (!ConfirmDialog) return expect(ConfirmDialog).toBeTypeOf('function');
    const onConfirm = vi.fn(async () => undefined);
    render(<ConfirmDialog open title="Delete the course?" confirmLabel="Delete" typed={'   '} onConfirm={onConfirm} onClose={vi.fn()} />);
    const dialog = await screen.findByRole('dialog');
    const del = within(dialog).getByRole('button', { name: 'Delete' }) as HTMLButtonElement;
    expect(del.disabled).toBe(true);
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: ' ' } });
    expect(del.disabled).toBe(true);
    await act(async () => fireEvent.click(del));
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('closes on Escape and on Cancel with no request', async () => {
    const { ConfirmDialog } = await prim();
    if (!ConfirmDialog) return expect(ConfirmDialog).toBeTypeOf('function');
    const onConfirm = vi.fn(async () => undefined);
    const onClose = vi.fn();
    render(<ConfirmDialog open title="Delete the lesson “W01”?" confirmLabel="Delete" onConfirm={onConfirm} onClose={onClose} />);
    const dialog = await screen.findByRole('dialog');
    fireEvent.keyDown(dialog, { key: 'Escape' });
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(onConfirm).not.toHaveBeenCalled();
  });
});

describe('UndoToast (FR-MANAGE-1)', () => {
  it('offers Undo for 10 s, pauses while hovered, and announces the restore', async () => {
    const { UndoToast } = await prim();
    expect(UndoToast).toBeTypeOf('function');
    if (!UndoToast) return;
    vi.useFakeTimers();
    const onClose = vi.fn();
    const onUndo = vi.fn(async () => undefined);
    const { container } = render(<UndoToast text="Course deleted." restoredText="Course restored." onUndo={onUndo} onClose={onClose} />);
    expect(screen.getByText('Course deleted.')).toBeTruthy();
    const toast = container.querySelector('.undo-toast') as HTMLElement;
    expect(toast).toBeTruthy();
    fireEvent.mouseEnter(toast);
    act(() => vi.advanceTimersByTime(15_000));
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.mouseLeave(toast);
    act(() => vi.advanceTimersByTime(9_000));
    expect(onClose).not.toHaveBeenCalled();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Undo' }));
    });
    expect(onUndo).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('status').textContent).toContain('Course restored.');
    vi.useRealTimers();
    render(<UndoToast text="Lesson deleted." restoredText="Lesson restored." onUndo={onUndo} onClose={onClose} ms={50} />);
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });
});

describe('text only (INV-MANAGE-3)', () => {
  it('finds no dangerouslySetInnerHTML or innerHTML under web/src/manage', () => {
    const dir = join(webRoot, 'src/manage');
    const files = readdirSync(dir).filter((f) => /\.(ts|tsx)$/.test(f) && !/\.test\./.test(f));
    expect(files.length).toBeGreaterThan(3);
    for (const f of files) expect(readFileSync(join(dir, f), 'utf8'), f).not.toMatch(/dangerouslySetInnerHTML|\.innerHTML\b/);
  });
});
