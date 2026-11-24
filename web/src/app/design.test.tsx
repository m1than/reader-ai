// @trace FR-UI-1 FR-UI-6 FR-UI-7 FR-UI-11 FR-UI-16 FR-UI-17
// Design pass (2026-11-23, V/design-pass): library entries, the lesson list's Continue block and course groups, the
// compile slot, notices, the header with NAV_ITEMS and the user menu, state blocks, the theme choice and the course
// sidebar. Synthetic lessons only.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useState, type ReactNode } from 'react';
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
  NAV_ITEMS: { label: string; to: string; owner_only: boolean; useBadge?: () => { shown: string; after: string } | null }[];
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

type Slot = { Status?: (p: { lesson: L }) => ReactNode; Action?: (p: { lesson: L }) => ReactNode };
const mount = async (path: string, opts: { me?: unknown; list?: L[]; compileSlot?: Slot; lesson?: unknown } = {}) => {
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
  const props = { initialPath: path, fetch: fetchFn as unknown as typeof fetch, navigateAway, compileSlot: opts.compileSlot };
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
      'container', 'app-bar', 'list-view', 'alert', 'breadcrumbs', 'menu', 'drawer', 'icon',
      // WEB-MANAGE paired change (2026-11-24).
      'text-field', 'select', 'switch', 'tabs', 'radio-group', 'styles.css',
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

  // One disclosure control per group (review): the button under the list; the heading is plain text.
  const toggleOf = (id: string) => within(group(id)).getByRole('button', { name: /^Show (all \d+ lessons|published only)$/ });

  it('collapses a group of more than 12 lessons to its published rows; 12 or fewer start expanded', async () => {
    await mount('/');
    await waitFor(() => expect(group('big')).toBeTruthy());
    expect(within(group('big')).getByRole('heading', { level: 2 }).querySelector('button')).toBeNull();
    expect(toggleOf('big').getAttribute('aria-expanded')).toBe('false');
    expect(toggleOf('big').textContent).toBe('Show all 30 lessons');
    expect(within(group('big')).getAllByRole('listitem')).toHaveLength(2);
    for (const a of within(group('big')).getAllByRole('link')) expect(a.getAttribute('href')).toMatch(/^\/lessons\/big-(1|17)$/);
    expect(toggleOf('small').getAttribute('aria-expanded')).toBe('true');
    expect(within(group('small')).getAllByRole('listitem')).toHaveLength(3);
  });

  it('draws the 12-row line exactly: 12 lessons start expanded, 13 collapsed', async () => {
    await mount('/', { list: [...course('twelve', 'Twelve course', 12, [3]), ...course('thirteen', 'Thirteen course', 13, [3])] });
    await waitFor(() => expect(group('twelve')).toBeTruthy());
    expect(toggleOf('twelve').getAttribute('aria-expanded')).toBe('true');
    expect(within(group('twelve')).getAllByRole('listitem')).toHaveLength(12);
    expect(toggleOf('thirteen').getAttribute('aria-expanded')).toBe('false');
    expect(within(group('thirteen')).getAllByRole('listitem')).toHaveLength(1);
  });

  it('has no toggle in a group whose lessons are all published', async () => {
    await mount('/', { list: course('done', 'Done course', 14, Array.from({ length: 14 }, (_, i) => i + 1)) });
    await waitFor(() => expect(group('done')).toBeTruthy());
    expect(within(group('done')).queryByRole('button', { name: /^Show / })).toBeNull();
    expect(within(group('done')).getAllByRole('listitem')).toHaveLength(14);
  });

  it('toggles with the one button under the list, which names the list it controls', async () => {
    await mount('/');
    await waitFor(() => expect(group('big')).toBeTruthy());
    const list = document.getElementById(toggleOf('big').getAttribute('aria-controls') ?? '');
    expect(list && group('big').contains(list)).toBe(true);
    fireEvent.click(toggleOf('big'));
    expect(within(group('big')).getAllByRole('listitem')).toHaveLength(30);
    expect(toggleOf('big').getAttribute('aria-expanded')).toBe('true');
    expect(toggleOf('big').textContent).toBe('Show published only');
    fireEvent.click(toggleOf('big'));
    expect(within(group('big')).getAllByRole('listitem')).toHaveLength(2);
  });

  it('expands the group named by /#course-<id> and scrolls it into view', async () => {
    const scroll = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scroll;
    try {
      await mount('/#course-big');
      await waitFor(() => expect(group('big')).toBeTruthy());
      expect(within(group('big')).getAllByRole('listitem')).toHaveLength(30);
      await waitFor(() => expect(scroll.mock.contexts).toContain(group('big')));
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });

  it('renders the slot Status and Action components for every row, published ones included', async () => {
    const seen: string[] = [];
    const slot: Slot = {
      Status: ({ lesson }) => {
        seen.push(lesson.lesson_id);
        return lesson.lesson_id === 'small-3' ? null : <span>{`status ${lesson.lesson_id}`}</span>;
      },
      Action: ({ lesson }) => <button type="button">{`Compile ${lesson.lesson_id}`}</button>,
    };
    await mount('/', { list: small(), compileSlot: slot });
    await waitFor(() => expect(group('small')).toBeTruthy());
    const g = group('small');
    expect(seen).toEqual(expect.arrayContaining(['small-1', 'small-2', 'small-3']));
    const row = (n: number) => within(g).getByText(`Small course lesson ${n}`).closest('li') as HTMLElement;
    // small-1: unpublished; the slot's status fills .slot-status, so app.css hides the default chip.
    expect(within(row(1)).getByText('status small-1').closest('.slot-status')).toBeTruthy();
    expect((row(1).querySelector('.slot-status') as HTMLElement).matches(':empty')).toBe(false);
    expect(within(row(1)).getByRole('button', { name: 'Compile small-1' }).closest('.row-action')).toBeTruthy();
    // small-2: published; status and an action on a published row too, no default chip at all.
    expect(within(row(2)).getByText('status small-2')).toBeTruthy();
    expect(within(row(2)).getByRole('button', { name: 'Compile small-2' })).toBeTruthy();
    expect(row(2).querySelector('.status-default')).toBeNull();
    // small-3: Status rendered null, so .slot-status is empty beside the default chip (shown by app.css).
    expect((row(3).querySelector('.slot-status') as HTMLElement).matches(':empty')).toBe(true);
    expect(within(row(3).querySelector('.status-default') as HTMLElement).getByText('not published yet')).toBeTruthy();
  });

  it('keeps a stateful slot component\'s state across re-renders', async () => {
    const Action = ({ lesson }: { lesson: L }) => {
      const [n, setN] = useState(0);
      return <button type="button" onClick={() => setN((x) => x + 1)}>{`Compile ${lesson.lesson_id} ${n}`}</button>;
    };
    await mount('/', { list: course('big', 'Big course', 30, [1, 17]), compileSlot: { Action } });
    await waitFor(() => expect(group('big')).toBeTruthy());
    fireEvent.click(within(group('big')).getByRole('button', { name: 'Compile big-1 0' }));
    expect(within(group('big')).getByRole('button', { name: 'Compile big-1 1' })).toBeTruthy();
    fireEvent.click(within(group('big')).getByRole('button', { name: 'Show all 30 lessons' }));
    expect(within(group('big')).getByRole('button', { name: 'Compile big-1 1' })).toBeTruthy();
  });

  // Since the merge with V/compile-ui the app always has a slot (FR-UI-15's control for the owner); for a learner it
  // renders nothing, so the rows show only the default status.
  it('shows only the default status when the slot renders nothing (a learner)', async () => {
    await mount('/', { list: small(), me: learner });
    await waitFor(() => expect(group('small')).toBeTruthy());
    expect(within(group('small')).queryByRole('button', { name: /^Compile/ })).toBeNull();
    expect(within(group('small')).getAllByText('not published yet')).toHaveLength(2);
  });

  it('shows the status as a chip on unpublished rows', async () => {
    await mount('/', { list: small(), me: learner });
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
      // FR-UI-14 (merge with V/signup): the Sign-ups entry with its count as the badge.
      { label: 'Sign-ups', to: '/signups', owner_only: true, useBadge: expect.any(Function) },
      // FR-UI-18 (paired SRV-OBS change).
      { label: 'Observability', to: '/observability', owner_only: true },
    ]);
    if (!mod.NAV_ITEMS) return;
    // An appended entry with a badge appears without changes to the header: "<label> <shown> <after>" with only
    // `after` visually hidden (code review 2026-11-24; no punctuation in hidden text).
    const useBadge = () => ({ shown: '7', after: 'waiting' });
    mod.NAV_ITEMS.push({ label: 'Test entry', to: '/test-entry', owner_only: false, useBadge });
    try {
      await mount('/');
      const nav = await waitFor(() => screen.getByRole('navigation', { name: 'Main' }));
      const links = within(nav).getAllByRole('link');
      // The owner's Sign-ups count call fails here (404), so its badge renders nothing.
      expect(links.map((a) => a.textContent)).toEqual(['Lessons', 'Import', 'Sign-ups', 'Observability', 'Test entry 7 waiting']);
      expect(within(nav).getByRole('link', { name: 'Sign-ups' })).toBe(links[2]);
      expect(links[4]!.getAttribute('aria-label')).toBeNull();
      expect(links[4]!.querySelector('.nav-badge')?.textContent).toBe('7');
      expect(links[4]!.querySelector('.visually-hidden')?.textContent).toBe(' waiting');
      // Below 75rem the same nav sits in the Courses drawer (live fix 2026-11-24): same entries, same names.
      fireEvent.click(screen.getByRole('button', { name: 'Courses' }));
      const drawerNav = await waitFor(() => {
        const n = document.querySelector('nav.drawer-nav');
        if (!n) throw new Error('no drawer nav');
        return n as HTMLElement;
      });
      expect(drawerNav.getAttribute('aria-label')).toBe('Main');
      expect(within(drawerNav).getAllByRole('link').map((a) => a.textContent)).toEqual([
        'Lessons',
        'Import',
        'Sign-ups',
        'Observability',
        'Test entry 7 waiting',
      ]);
      fireEvent.click(within(drawerNav).getByRole('link', { name: 'Import' }));
      await waitFor(() => expect(document.querySelector('nav.drawer-nav')).toBeNull());
      mod.NAV_ITEMS.pop();
      mod.NAV_ITEMS.push({ label: 'Test entry', to: '/test-entry', owner_only: false });
      cleanup();
      await mount('/', { me: learner });
      const nav2 = await waitFor(() => screen.getByRole('navigation', { name: 'Main' }));
      expect(within(nav2).getAllByRole('link').map((a) => a.textContent)).toEqual(['Lessons', 'Test entry']);
      // The drawer's copy follows the same owner-only rule for a learner (code review: kills an `owner={true}`).
      fireEvent.click(screen.getByRole('button', { name: 'Courses' }));
      const learnerDrawer = await waitFor(() => {
        const n = document.querySelector('nav.drawer-nav');
        if (!n) throw new Error('no drawer nav');
        return n as HTMLElement;
      });
      expect(within(learnerDrawer).getAllByRole('link').map((a) => a.textContent)).toEqual(['Lessons', 'Test entry']);
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

  it('index.html loads /theme.js from <head> before the module script, with no inline script', async () => {
    const { readText } = await import('./testing/fixtures');
    const html = readText('web/index.html');
    const head = html.slice(0, html.indexOf('</head>'));
    expect(head).toMatch(/<script src="\/theme\.js"><\/script>/);
    const themeAt = html.indexOf('src="/theme.js"');
    const moduleAt = html.indexOf('type="module"');
    expect(themeAt).toBeGreaterThan(-1);
    expect(themeAt).toBeLessThan(moduleAt);
    for (const m of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)) {
      expect(m[1], 'every script has a src').toMatch(/\bsrc=/);
      expect(m[2]!.trim(), 'no inline script body').toBe('');
    }
  });

  it('theme.js sets the same attribute as applyTheme for every stored value and a throwing storage', async () => {
    const { readText } = await import('./testing/fixtures');
    const { runInNewContext } = await import('node:vm');
    const { applyTheme } = await dyn<ThemeModule>('../ui/theme');
    expect(applyTheme).toBeTypeOf('function');
    if (!applyTheme) return;
    const src = readText('web/src/public/theme.js');
    const run = (getItem: () => string | null) => {
      const attrs = new Map<string, string>();
      const documentElement = {
        setAttribute: (k: string, v: string) => void attrs.set(k, v),
        removeAttribute: (k: string) => void attrs.delete(k),
        dataset: new Proxy({} as Record<string, string>, {
          set: (_t, k, v) => (attrs.set(`data-${String(k)}`, String(v)), true),
          deleteProperty: (_t, k) => (attrs.delete(`data-${String(k)}`), true),
          get: (_t, k) => attrs.get(`data-${String(k)}`),
        }),
      };
      runInNewContext(src, { document: { documentElement }, localStorage: { getItem } });
      return attrs.get('data-theme') ?? null;
    };
    for (const v of ['system', 'light', 'dark', 'sepia', null]) {
      const el = document.createElement('div');
      applyTheme((v === 'light' || v === 'dark' ? v : 'system') as Theme, el);
      expect(run(() => v), String(v)).toBe(el.getAttribute('data-theme'));
    }
    expect(
      run(() => {
        throw new Error('blocked');
      }),
    ).toBeNull();
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

  it('closed groups show no rows; opening one shows all of a small course, without the compile slot', async () => {
    const slot: Slot = {
      Status: ({ lesson }) => <span>{`status ${lesson.lesson_id}`}</span>,
      Action: ({ lesson }) => <button type="button">{`Compile ${lesson.lesson_id}`}</button>,
    };
    await mount('/lessons/big-1', { compileSlot: slot });
    const btn = await waitFor(() => within(sidebar()).getByRole('button', { name: /Small course/ }));
    const list = document.getElementById(btn.getAttribute('aria-controls') ?? '');
    expect(list === null || within(list).queryAllByRole('listitem').length === 0 || list.hidden).toBe(true);
    fireEvent.click(btn);
    const open = document.getElementById(btn.getAttribute('aria-controls') ?? '') as HTMLElement;
    expect(within(open).getAllByRole('listitem')).toHaveLength(3);
    expect(within(sidebar()).queryByRole('button', { name: /^Compile/ })).toBeNull();
    expect(within(sidebar()).queryByText(/^status /)).toBeNull();
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
    // aria-controls names the drawer's popup (code review).
    expect(open.getAttribute('aria-controls')).toBe('course-drawer');
    expect(document.getElementById('course-drawer')).toBeTruthy();
    const navInDrawer = within(dialog).getByRole('navigation', { name: 'Courses' });
    // The static column and the drawer's copy share no id.
    const ids = [...document.querySelectorAll('[id]')].map((e) => e.id);
    expect(ids.filter((id, i) => ids.indexOf(id) !== i)).toEqual([]);
    const btn = within(navInDrawer).getByRole('button', { name: /Small course/ });
    fireEvent.click(btn);
    await act(async () => {
      fireEvent.click(within(navInDrawer).getByRole('link', { name: /Small course lesson 2/ }));
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Courses' })));
  });

  it('closes the drawer with Escape and returns focus to the Courses button', async () => {
    await mount('/');
    const open = await waitFor(() => screen.getByRole('button', { name: 'Courses' }));
    await act(async () => {
      fireEvent.click(open);
    });
    const dialog = await screen.findByRole('dialog');
    await act(async () => {
      fireEvent.keyDown(dialog, { key: 'Escape' });
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(open));
  });

  it('makes no request beyond the shared lessons query', async () => {
    const { fetchFn } = await mount('/');
    await waitFor(() => expect(screen.getAllByRole('navigation', { name: 'Courses' }).length).toBeGreaterThan(0));
    const lessonsCalls = fetchFn.mock.calls.filter((c) => String(c[0]) === '/api/lessons');
    expect(lessonsCalls).toHaveLength(1);
  });
});
