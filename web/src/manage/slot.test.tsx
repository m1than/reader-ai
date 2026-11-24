// @trace FR-UI-31 FR-MANAGE-3 FR-MANAGE-4 FR-MANAGE-10
// WEB-UI FR-UI-31 for WEB-MANAGE's slot (web/src/manage/slot.tsx): each lazy part loads the `manage` Russian
// catalogue together with its chunk, so in a Russian interface the course and lesson menus appear only once that
// catalogue is there, already in Russian, never in English first.
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { load } from '../app/testing/load';
import { registerRuLoaders, type Catalogue } from '../ui/i18n';
import { RU_LOADERS } from '../ui/ru-loaders';
import { fakeServer } from './testkit';

afterEach(() => {
  cleanup();
  registerRuLoaders(RU_LOADERS);
  localStorage.removeItem('reader-ai.locale');
});

const settle = () => act(async () => new Promise((r) => setTimeout(r, 100)));

describe('the manage slot’s lazy chunks in Russian (FR-UI-31)', () => {
  it('renders the course and lesson menus only with the manage catalogue, in Russian', async () => {
    let open: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      open = resolve;
    });
    const manageLoader = vi.fn(() => gate.then(() => import('./messages.ru') as Promise<{ default: Catalogue }>));
    registerRuLoaders({ ...RU_LOADERS, manage: manageLoader });
    localStorage.setItem('reader-ai.locale', 'ru');
    const { AppRoot } = await load(() => import('../app/AppRoot'));
    await act(async () => {
      render(<AppRoot initialPath="/" fetch={fakeServer().fetch} navigateAway={vi.fn()} />);
    });
    await screen.findByRole('heading', { level: 2, name: 'Fixture course' });
    await settle();
    // The chunks have loaded, the catalogue has not: no menu trigger in either language yet.
    expect(manageLoader).toHaveBeenCalled();
    expect(screen.queryAllByRole('button', { name: /^(Actions for|Действия:) / })).toEqual([]);
    open();
    expect(await screen.findByRole('button', { name: 'Действия: Fixture course' })).toBeTruthy();
    expect(await screen.findByRole('button', { name: 'Действия: Fixture lesson W01: one cell of every kind' })).toBeTruthy();
    expect(screen.queryAllByRole('button', { name: /^Actions for / })).toEqual([]);
  });
});
