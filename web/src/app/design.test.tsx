// @trace FR-UI-1 FR-UI-6 FR-UI-7 FR-UI-11 FR-UI-16 FR-UI-17
// Design pass (2026-11-23, V/design-pass): library entries, the lesson list's Continue block and course groups, the
// compile slot, notices, the header with NAV_ITEMS and the user menu, state blocks, the theme choice and the course
// sidebar. Synthetic lessons only.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { load } from './testing/load';

afterEach(cleanup);

// Modules this pass adds (Red): imported by path so that the suite collects before they exist.
const dyn = <T,>(path: string) => load<Partial<T>>(() => import(/* @vite-ignore */ path) as Promise<T>);
type Theme = 'system' | 'light' | 'dark';
interface ThemeModule {
  THEME_KEY: string;
  readTheme: (storage: { getItem: (k: string) => string | null }) => Theme;
  applyTheme: (choice: Theme, root: HTMLElement) => void;
}
interface NavModule {
  NAV_ITEMS: { label: string; to: string; owner_only: boolean }[];
}
interface NoticesModule {
  NOTICE_CATALOGUE: Record<string, string>;
}

const owner = { id: '0b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b10', handle: 'ivan', role: 'owner', display_name: 'Ivan', consent: { evaluation: true, tuning: false } };
const learner = { ...owner, id: '1b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b11', role: 'learner', display_name: 'Lena' };
const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const SHA = (n: number) => String(n % 10).repeat(64);

interface L {
  lesson_id: string;
  course_id: string;
  course_title: string;
  ordinal: number;
  title: string;
  updated_at: string;
  latest_sha256: string | null;
}
const course = (id: string, title: string, n: number, published: number[], day = 10): L[] =>
  Array.from({ length: n }, (_, i) => ({
    lesson_id: `${id}-${i + 1}`,
    course_id: id,
    course_title: title,
    ordinal: i + 1,
    title: `${title} lesson ${i + 1}`,
    updated_at: `2026-11-${String(day + (published.includes(i + 1) ? i % 9 : 0)).padStart(2, '0')}T09:00:00Z`,
    latest_sha256: published.includes(i + 1) ? SHA(i) : null,
  }));
// A course of 30 lessons with 2 published (ordinals 1 and 17) and a course of 3 lessons with ordinal 2 published.
const big = () => course('big', 'Big course', 30, [1, 17]);
const small = () => course('small', 'Small course', 3, [2], 1);
const lessons = () => [...big(), ...small()];

const mount = async (path: string, opts: { me?: unknown; list?: L[]; compileAction?: (l: L) => ReactNode; lesson?: unknown } = {}) => {
  const { AppRoot } = await load(() => import('./AppRoot'));
  expect(AppRoot).toBeTypeOf('function');
  const fetchFn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === '/api/me') return json(200, opts.me ?? owner);
    if (url === '/api/lessons') return json(200, { lessons: opts.list ?? lessons() });
    if (url === '/api/auth/signout') return new Response(null, { status: 204 });
    if (url === '/api/materials') return json(200, { materials: [] });
    return json(404, { error: 'not_found' });
  });
  const navigateAway = vi.fn();
  const props = { initialPath: path, fetch: fetchFn as unknown as typeof fetch, navigateAway, compileAction: opts.compileAction };
  await act(async () => {
    render(<AppRoot {...(props as Parameters<typeof AppRoot>[0])} />);
  });
  // The shell's main appears once /api/me has answered.
  await waitFor(() => expect(document.getElementById('content')).toBeTruthy());
  return { fetchFn, navigateAway };
};
const main = () => document.getElementById('content') as HTMLElement;
const group = (courseId: string) => main().querySelector(`#course-${courseId}`) as HTMLElement;

describe('library entries (FR-UI-1, design pass)', () => {
  it('allows exactly the spec list, in order, then styles.css', async () => {
    const { allowedEntries } = await load(() => import('./sourceScan'));
    expect(allowedEntries).toEqual([
      'button', 'toast', 'progress', 'skeleton', 'tooltip', 'dialog', 'chip', 'divider', 'link', 'typography', 'stack',
      'container', 'app-bar', 'list-view', 'alert', 'breadcrumbs', 'menu', 'drawer', 'icon', 'styles.css',
    ]);
  });
});

describe('lesson list (FR-UI-6, design pass)', () => {
  it('shows a Continue block of published lessons, newest first, at most 5', async () => {
    const many = [...course('a', 'Course A', 6, [1, 2, 3, 4], 1), ...course('b', 'Course B', 4, [1, 2, 3], 5)];
    await mount('/', { list: many });
    const heading = await within(main()).findByRole('heading', { name: 'Continue', level: 2 });
    const section = heading.closest('section') as HTMLElement;
    const links = within(section).getAllByRole('link');
    expect(links).toHaveLength(5);
    const byDate = many
      .filter((l) => l.latest_sha256)
      .sort((x, y) => y.updated_at.localeCompare(x.updated_at))
      .slice(0, 5)
      .map((l) => `/lessons/${l.lesson_id}`);
    expect(links.map((a) => a.getAttribute('href'))).toEqual(byDate);
    expect(within(section).getAllByText(/^Course (A|B)$/).length).toBeGreaterThan(0);
  });

  it('has no Continue block without a published lesson', async () => {
    await mount('/', { list: course('n', 'None course', 3, []) });
    await within(main()).findByText(/0 of 3 published/);
    expect(within(main()).queryByRole('heading', { name: 'Continue' })).toBeNull();
  });

  it('summarises each course: published of all and the next unpublished lesson', async () => {
    await mount('/');
    await waitFor(() => expect(group('big')).toBeTruthy());
    expect(group('big').textContent).toContain('2 of 30 published · next: Big course lesson 2');
    expect(group('small').textContent).toContain('1 of 3 published · next: Small course lesson 1');
    const all = course('done', 'Done course', 2, [1, 2]);
    cleanup();
    await mount('/', { list: all });
    await waitFor(() => expect(group('done')).toBeTruthy());
    expect(group('done').textContent).toContain('2 of 2 published');
    expect(group('done').textContent).not.toContain('next:');
  });

  it('collapses a group of more than 12 lessons to its published rows; 12 or fewer start expanded', async () => {
    await mount('/');
    await waitFor(() => expect(group('big')).toBeTruthy());
    const bigToggle = within(group('big')).getByRole('button', { name: /Big course/ });
    expect(bigToggle.getAttribute('aria-expanded')).toBe('false');
    expect(within(group('big')).getAllByRole('listitem')).toHaveLength(2);
    for (const a of within(group('big')).getAllByRole('link')) expect(a.getAttribute('href')).toMatch(/^\/lessons\/big-(1|17)$/);
    expect(within(group('small')).getByRole('button', { name: /Small course/ }).getAttribute('aria-expanded')).toBe('true');
    expect(within(group('small')).getAllByRole('listitem')).toHaveLength(3);
  });

  it('expands with "Show all N lessons" and with the heading button, and collapses back', async () => {
    await mount('/');
    await waitFor(() => expect(group('big')).toBeTruthy());
    fireEvent.click(within(group('big')).getByRole('button', { name: 'Show all 30 lessons' }));
    expect(within(group('big')).getAllByRole('listitem')).toHaveLength(30);
    expect(within(group('big')).getByRole('button', { name: /Big course/ }).getAttribute('aria-expanded')).toBe('true');
    fireEvent.click(within(group('big')).getByRole('button', { name: 'Show published only' }));
    expect(within(group('big')).getAllByRole('listitem')).toHaveLength(2);
    fireEvent.click(within(group('big')).getByRole('button', { name: /Big course/ }));
    expect(within(group('big')).getAllByRole('listitem')).toHaveLength(30);
  });

  it('points the disclosure at its list', async () => {
    await mount('/');
    await waitFor(() => expect(group('big')).toBeTruthy());
    const toggle = within(group('big')).getByRole('button', { name: /Big course/ });
    const list = document.getElementById(toggle.getAttribute('aria-controls') ?? '');
    expect(list && group('big').contains(list)).toBe(true);
  });

  it('expands the group named by /#course-<id>', async () => {
    await mount('/#course-big');
    await waitFor(() => expect(group('big')).toBeTruthy());
    expect(within(group('big')).getAllByRole('listitem')).toHaveLength(30);
  });

  it('renders the compile slot on every unpublished row, and nothing without it', async () => {
    const slot = vi.fn((l: L) => <button type="button">{`Compile ${l.lesson_id}`}</button>);
    await mount('/', { list: small(), compileAction: slot });
    await waitFor(() => expect(group('small')).toBeTruthy());
    expect(within(group('small')).getByRole('button', { name: 'Compile small-1' })).toBeTruthy();
    expect(within(group('small')).getByRole('button', { name: 'Compile small-3' })).toBeTruthy();
    expect(within(group('small')).queryByRole('button', { name: 'Compile small-2' })).toBeNull();
    cleanup();
    await mount('/', { list: small() });
    await waitFor(() => expect(group('small')).toBeTruthy());
    expect(within(group('small')).queryByRole('button', { name: /^Compile/ })).toBeNull();
  });

  it('shows the status as a chip on unpublished rows', async () => {
    await mount('/', { list: small() });
    await waitFor(() => expect(group('small')).toBeTruthy());
    const chip = within(group('small')).getAllByText('not published yet')[0]!;
    expect(chip.closest('.acme-chip, [data-chip], .chip')).toBeTruthy();
  });
});

describe('notices (FR-UI-7, design pass)', () => {
  it('every catalogue message is in sentence case and ends with a full stop', async () => {
    const { NOTICE_CATALOGUE } = await dyn<NoticesModule>('./notices');
    expect(NOTICE_CATALOGUE).toBeTypeOf('object');
    const entries = Object.entries(NOTICE_CATALOGUE ?? {});
    expect(entries.length).toBeGreaterThan(10);
    for (const [code, text] of entries) expect(text, code).toMatch(/^[A-Z].*\.$/s);
  });

  it('shows an icon before each notice and "+n more" when more than 3', async () => {
    const { NoticeRegion } = await load(() => import('./notices'));
    expect(NoticeRegion).toBeTypeOf('function');
    const notices = [1, 2, 3, 4, 5].map((id) => ({ id, code: 'busy' }));
    render(<NoticeRegion notices={notices} onDismiss={() => undefined} />);
    expect(screen.getByText('+2 more')).toBeTruthy();
    const items = screen.getAllByRole('status');
    expect(items).toHaveLength(3);
    for (const item of items) expect(item.querySelector('svg[aria-hidden="true"]')).toBeTruthy();
  });
});

describe('header and state blocks (FR-UI-11, design pass)', () => {
  it('orders brand, main nav and user menu; the brand reads reader-ai', async () => {
    await mount('/');
    const header = await waitFor(() => document.querySelector('header.app-header') as HTMLElement);
    const brand = within(header).getByRole('link', { name: 'reader-ai' });
    expect(brand.getAttribute('href')).toBe('/');
    const nav = within(header).getByRole('navigation', { name: 'Main' });
    const user = within(header).getByRole('button', { name: /Ivan/ });
    expect(brand.compareDocumentPosition(nav) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(nav.compareDocumentPosition(user) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('drives the main nav from NAV_ITEMS, owner-only items hidden from a learner', async () => {
    const mod = await dyn<NavModule>('./nav');
    expect(mod.NAV_ITEMS).toEqual([
      { label: 'Lessons', to: '/', owner_only: false },
      { label: 'Import', to: '/import', owner_only: true },
    ]);
    if (!mod.NAV_ITEMS) return;
    mod.NAV_ITEMS.push({ label: 'Test entry', to: '/test-entry', owner_only: false });
    try {
      await mount('/');
      const nav = await waitFor(() => screen.getByRole('navigation', { name: 'Main' }));
      expect(within(nav).getAllByRole('link').map((a) => a.textContent)).toEqual(['Lessons', 'Import', 'Test entry']);
      cleanup();
      await mount('/', { me: learner });
      const nav2 = await waitFor(() => screen.getByRole('navigation', { name: 'Main' }));
      expect(within(nav2).getAllByRole('link').map((a) => a.textContent)).toEqual(['Lessons', 'Test entry']);
    } finally {
      mod.NAV_ITEMS.pop();
    }
  });

  it('marks the current nav item: Lessons on / and on a lesson, Import on /import', async () => {
    const current = () => within(screen.getByRole('navigation', { name: 'Main' })).getAllByRole('link').filter((a) => a.getAttribute('aria-current') === 'page').map((a) => a.textContent);
    await mount('/');
    await waitFor(() => expect(current()).toEqual(['Lessons']));
    cleanup();
    await mount('/lessons/big-1');
    await waitFor(() => expect(current()).toEqual(['Lessons']));
    cleanup();
    await mount('/import');
    await waitFor(() => expect(current()).toEqual(['Import']));
  });

  it('puts the theme choice and Sign out in the user menu; Sign out signs out', async () => {
    const { navigateAway, fetchFn } = await mount('/');
    const trigger = await waitFor(() => screen.getByRole('button', { name: /Ivan/ }));
    await act(async () => {
      fireEvent.click(trigger);
    });
    const menu = await screen.findByRole('menu');
    expect(within(menu).getAllByRole('menuitemradio').map((r) => r.textContent?.trim())).toEqual(['System', 'Light', 'Dark']);
    await act(async () => {
      fireEvent.click(within(menu).getByRole('menuitem', { name: 'Sign out' }));
    });
    await waitFor(() => expect(navigateAway).toHaveBeenCalledWith('/api/auth/signin'));
    expect(fetchFn.mock.calls.map((c) => String(c[0]))).toContain('/api/auth/signout');
  });

  it('renders not-found through the state block', async () => {
    await mount('/no-such-page');
    const h1 = await within(main()).findByRole('heading', { level: 1, name: 'Not found' });
    expect(h1.closest('.state-block')).toBeTruthy();
    expect(within(h1.closest('.state-block') as HTMLElement).getByRole('link', { name: /lessons/i })).toBeTruthy();
  });

  it('renders the list error through the state block with Retry', async () => {
    const { AppRoot } = await load(() => import('./AppRoot'));
    const fetchFn = vi.fn(async (input: RequestInfo | URL) => (String(input) === '/api/me' ? json(200, owner) : json(500, { error: 'x' })));
    await act(async () => {
      render(<AppRoot initialPath="/" fetch={fetchFn as unknown as typeof fetch} navigateAway={vi.fn()} />);
    });
    await waitFor(() => expect(main()).toBeTruthy());
    const retry = await within(main()).findByRole('button', { name: /retry/i });
    expect(retry.closest('.state-block')).toBeTruthy();
  });
});

describe('theme (FR-UI-16)', () => {
  const store = () => {
    const m = new Map<string, string>();
    return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), removeItem: (k: string) => void m.delete(k), m };
  };
  beforeEach(() => document.documentElement.removeAttribute('data-theme'));

  it('reads the stored choice; anything else, or a throwing storage, is System', async () => {
    const { readTheme, THEME_KEY } = await dyn<ThemeModule>('../ui/theme');
    expect(THEME_KEY).toBe('reader-ai.theme');
    if (!readTheme) return;
    const s = store();
    expect(readTheme(s)).toBe('system');
    for (const v of ['system', 'light', 'dark'] as const) {
      s.setItem('reader-ai.theme', v);
      expect(readTheme(s)).toBe(v);
    }
    s.setItem('reader-ai.theme', 'sepia');
    expect(readTheme(s)).toBe('system');
    const broken = { getItem: () => { throw new Error('blocked'); } };
    expect(readTheme(broken)).toBe('system');
  });

  it('applies System by removing data-theme, Light and Dark by setting it', async () => {
    const { applyTheme } = await dyn<ThemeModule>('../ui/theme');
    expect(applyTheme).toBeTypeOf('function');
    if (!applyTheme) return;
    const root = document.documentElement;
    applyTheme('dark', root);
    expect(root.dataset.theme).toBe('dark');
    applyTheme('light', root);
    expect(root.dataset.theme).toBe('light');
    applyTheme('system', root);
    expect(root.hasAttribute('data-theme')).toBe(false);
  });

  it('the menu choice sets the attribute and stores it; the checked radio follows', async () => {
    localStorage.removeItem('reader-ai.theme');
    await mount('/');
    const trigger = await waitFor(() => screen.getByRole('button', { name: /Ivan/ }));
    await act(async () => {
      fireEvent.click(trigger);
    });
    const menu = await screen.findByRole('menu');
    expect(within(menu).getByRole('menuitemradio', { name: 'System' }).getAttribute('aria-checked')).toBe('true');
    await act(async () => {
      fireEvent.click(within(menu).getByRole('menuitemradio', { name: 'Dark' }));
    });
    expect(document.documentElement.dataset.theme).toBe('dark');
    expect(localStorage.getItem('reader-ai.theme')).toBe('dark');
    localStorage.removeItem('reader-ai.theme');
  });

  it('main.tsx applies the stored theme before rendering', async () => {
    const { readText } = await import('./testing/fixtures');
    const src = readText('web/src/main.tsx');
    const apply = src.search(/initTheme\(|applyTheme\(/);
    const renderAt = src.search(/createRoot\(/);
    expect(apply).toBeGreaterThan(-1);
    expect(apply).toBeLessThan(renderAt);
  });
});

describe('course sidebar (FR-UI-17)', () => {
  const sidebar = () => screen.getAllByRole('navigation', { name: 'Courses' })[0]!;

  it('lists every course with its count, current lesson marked and its group open', async () => {
    await mount('/lessons/big-17');
    await waitFor(() => expect(within(sidebar()).getByRole('button', { name: /Big course/ })).toBeTruthy());
    const bigBtn = within(sidebar()).getByRole('button', { name: /Big course/ });
    expect(bigBtn.textContent).toMatch(/2\/30/);
    expect(bigBtn.textContent).toMatch(/published/);
    expect(bigBtn.getAttribute('aria-expanded')).toBe('true');
    expect(within(sidebar()).getByRole('button', { name: /Small course/ }).getAttribute('aria-expanded')).toBe('false');
    const current = within(sidebar()).getByRole('link', { name: /Big course lesson 17/ });
    expect(current.getAttribute('aria-current')).toBe('page');
    expect(current.getAttribute('href')).toBe('/lessons/big-17');
  });

  it('an open group of more than 12 shows published rows and the current one, then all', async () => {
    await mount('/lessons/big-1');
    const bigList = await waitFor(() => {
      const btn = within(sidebar()).getByRole('button', { name: /Big course/ });
      return document.getElementById(btn.getAttribute('aria-controls') ?? '') as HTMLElement;
    });
    expect(within(bigList).getAllByRole('listitem')).toHaveLength(2);
    fireEvent.click(within(sidebar()).getByRole('button', { name: 'Show all 30' }));
    expect(within(bigList).getAllByRole('listitem')).toHaveLength(30);
    fireEvent.click(within(sidebar()).getByRole('button', { name: 'Show fewer' }));
    expect(within(bigList).getAllByRole('listitem')).toHaveLength(2);
  });

  it('closed groups show no rows; opening one shows all of a small course with the compile slot', async () => {
    await mount('/lessons/big-1', { compileAction: (l) => <button type="button">{`Compile ${l.lesson_id}`}</button> });
    const btn = await waitFor(() => within(sidebar()).getByRole('button', { name: /Small course/ }));
    const list = document.getElementById(btn.getAttribute('aria-controls') ?? '');
    expect(list === null || within(list).queryAllByRole('listitem').length === 0 || list.hidden).toBe(true);
    fireEvent.click(btn);
    const open = document.getElementById(btn.getAttribute('aria-controls') ?? '') as HTMLElement;
    expect(within(open).getAllByRole('listitem')).toHaveLength(3);
    expect(within(open).getByRole('button', { name: 'Compile small-1' })).toBeTruthy();
    expect(within(open).getByRole('link', { name: /Small course lesson 2/ }).getAttribute('href')).toBe('/lessons/small-2');
  });

  it('the header Courses button opens the sidebar in a drawer, which closes on navigation', async () => {
    await mount('/');
    const open = await waitFor(() => screen.getByRole('button', { name: 'Courses' }));
    expect(open.getAttribute('aria-expanded')).toBe('false');
    await act(async () => {
      fireEvent.click(open);
    });
    expect(open.getAttribute('aria-expanded')).toBe('true');
    const dialog = await screen.findByRole('dialog');
    const navInDrawer = within(dialog).getByRole('navigation', { name: 'Courses' });
    const btn = within(navInDrawer).getByRole('button', { name: /Small course/ });
    fireEvent.click(btn);
    await act(async () => {
      fireEvent.click(within(navInDrawer).getByRole('link', { name: /Small course lesson 2/ }));
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('makes no request beyond the shared lessons query', async () => {
    const { fetchFn } = await mount('/');
    await waitFor(() => expect(screen.getAllByRole('navigation', { name: 'Courses' }).length).toBeGreaterThan(0));
    const lessonsCalls = fetchFn.mock.calls.filter((c) => String(c[0]) === '/api/lessons');
    expect(lessonsCalls).toHaveLength(1);
  });
});
