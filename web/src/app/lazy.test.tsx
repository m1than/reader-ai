// @trace FR-UI-11
// A lazy chunk that fails to load keeps the plain trigger on screen (code review of the design pass): the user menu's
// and the course drawer's chunks are mocked to throw on import.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { load } from './testing/load';

vi.mock('./UserMenu', () => {
  throw new Error('chunk failed');
});
vi.mock('./CourseDrawer', () => {
  throw new Error('chunk failed');
});

afterEach(cleanup);

const me = { id: '0b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b10', handle: 'ivan', role: 'owner', display_name: 'Ivan', consent: { evaluation: true, tuning: false } };
const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

describe('lazy chunks that fail (FR-UI-11)', () => {
  it('keeps the account trigger and the Courses button when their chunks fail', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const { AppRoot } = await load(() => import('./AppRoot'));
    const fetchFn = vi.fn(async (input: RequestInfo | URL) => (String(input) === '/api/me' ? json(200, me) : json(200, { lessons: [] })));
    await act(async () => {
      render(<AppRoot initialPath="/" fetch={fetchFn as unknown as typeof fetch} navigateAway={vi.fn()} />);
    });
    const trigger = await waitFor(() => screen.getByRole('button', { name: 'Account menu for Ivan' }));
    await act(async () => {
      fireEvent.click(trigger);
    });
    await act(async () => undefined);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Account menu for Ivan' })).toBeTruthy());
    expect(screen.queryByRole('menu')).toBeNull();
    const courses = screen.getByRole('button', { name: 'Courses' });
    await act(async () => {
      fireEvent.click(courses);
    });
    await act(async () => undefined);
    expect(screen.getByRole('button', { name: 'Courses' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Lessons', level: 1 })).toBeTruthy();
  });
});
