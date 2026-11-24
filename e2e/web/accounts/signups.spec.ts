// @trace FR-ACCOUNTS-1 FR-ACCOUNTS-2 FR-ACCOUNTS-3 FR-ACCOUNTS-5 FR-UI-14
// [browser] WEB-ACCOUNTS's Sign-ups screen in the built SPA behind WEB-UI's test server (production CSP), in the
// auth-dark and auth-light projects at 1280, 1024 and 390 px: every state with axe, overflow, CSP and focus checks,
// and the request oracle for the actions. `/api/auth/signups` and its POSTs are stubbed with page.route from
// VEC-ACCOUNTS-1. Run on request: `cd web && npm run build && npm run e2e -- accounts`.
import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page, type Route } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const WIDTHS = [1280, 1024, 390] as const;
type Row = { id: string; handle: string; display_name: string; requested_at: string; disabled?: boolean };
type Body = { pending: Row[]; accounts: Row[] };
const FX = JSON.parse(readFileSync(join(__dirname, '../../../spec/fixtures/WEB-ACCOUNTS/signups.json'), 'utf8')) as {
  empty: Body;
  one: Body;
  long_name: string;
  outcomes: Record<string, { status: number; body: unknown }>;
};
const twenty = (): Body => ({
  pending: Array.from({ length: 20 }, (_, i) => ({
    id: `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`,
    handle: `lc-p${String(i).padStart(2, '0')}`,
    display_name: i === 0 ? FX.long_name : `Person ${i}`,
    requested_at: `2026-11-23T0${i % 10}:${String(i).padStart(2, '0')}:00+00:00`,
  })),
  accounts: FX.one.accounts,
});

type Post = { path: string; body: unknown };

const open = async (page: Page, list: Body | 'held' | 'error', actions: Record<string, (route: Route) => Promise<void>> = {}) => {
  const posts: Post[] = [];
  page.on('request', (r) => {
    if (r.method() !== 'GET') posts.push({ path: new URL(r.url()).pathname, body: r.postDataJSON() });
  });
  await page.addInitScript(() => {
    (window as unknown as { __csp: string[] }).__csp = [];
    document.addEventListener('securitypolicyviolation', (e) => (window as unknown as { __csp: string[] }).__csp.push(e.violatedDirective));
  });
  await page.route('**/api/auth/signups', async (route) => {
    if (list === 'held') return; // never answered: the loading state
    if (list === 'error') return route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"unavailable"}' });
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(list) });
  });
  for (const [path, handler] of Object.entries(actions)) await page.route(`**${path}`, handler);
  await page.goto('/signups');
  return { posts };
};

const checks = async (page: Page) => {
  const axe = await new AxeBuilder({ page }).analyze();
  expect(axe.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical').map((v) => v.id)).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
  expect(await page.evaluate(() => (window as unknown as { __csp: string[] }).__csp)).toEqual([]);
  expect(await page.locator('main#content').count()).toBe(1);
};

const fulfill = (o: { status: number; body: unknown }) => (route: Route) =>
  route.fulfill({ status: o.status, contentType: 'application/json', body: o.body === null ? '' : JSON.stringify(o.body) });

for (const width of WIDTHS) {
  test.describe(`${width} px`, () => {
    test.beforeEach(async ({ page }) => page.setViewportSize({ width, height: 900 }));

    test('loading (FR-ACCOUNTS-1, FR-ACCOUNTS-5)', async ({ page }) => {
      await open(page, 'held');
      await expect(page.getByRole('status').filter({ hasText: 'Loading sign-ups' })).toHaveCount(1);
      await checks(page);
    });

    test('error with Retry (FR-ACCOUNTS-1)', async ({ page }) => {
      await open(page, 'error');
      await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible();
      await checks(page);
    });

    test('empty (FR-ACCOUNTS-1, FR-ACCOUNTS-3)', async ({ page }) => {
      await open(page, FX.empty);
      await expect(page.getByText('No sign-ups are waiting.')).toBeVisible();
      await expect(page.getByText('No approved sign-up accounts.')).toBeVisible();
      await checks(page);
    });

    test('one pending and accounts with a disabled row; header link (FR-UI-14)', async ({ page }) => {
      await open(page, FX.one);
      await expect(page.getByRole('button', { name: 'Approve @lc-1a2b' })).toBeVisible();
      await expect(page.getByText('Disabled')).toBeVisible();
      await checks(page);
      // Below 75rem (a width under 1200 px) the main nav lives in the Courses drawer (WEB-UI FR-UI-11).
      if ((page.viewportSize()?.width ?? 1280) < 1200) await page.getByRole('button', { name: 'Courses' }).click();
      const link = page.getByRole('link', { name: 'Sign-ups 1 waiting', exact: true });
      await expect(link).toHaveAttribute('href', '/signups');
    });

    test('twenty pending with a 64-code-point name (FR-ACCOUNTS-1)', async ({ page }) => {
      await open(page, twenty());
      await expect(page.getByRole('button', { name: /^Approve @/ })).toHaveCount(20);
      await expect(page.getByText(FX.long_name)).toBeVisible();
      await checks(page);
    });

    test('reject confirmation and keyboard focus outline (FR-ACCOUNTS-2, FR-ACCOUNTS-5)', async ({ page }) => {
      const { posts } = await open(page, FX.one);
      await page.getByRole('button', { name: 'Reject @lc-1a2b' }).click();
      await expect(page.getByText('Reject @lc-1a2b? The request is deleted and the handle becomes free.')).toBeVisible();
      await expect(page.getByRole('button', { name: 'Cancel' })).toBeFocused();
      await page.keyboard.press('Tab');
      const outline = await page.evaluate(() => {
        const s = getComputedStyle(document.activeElement as Element);
        return `${s.outlineWidth} ${s.outlineStyle}`;
      });
      expect(outline).toBe('2px solid');
      await checks(page);
      expect(posts).toEqual([]);
    });

    test('a busy row, then approve announced (FR-ACCOUNTS-2)', async ({ page }) => {
      let release: () => void = () => undefined;
      const held = new Promise<void>((r) => (release = r));
      const id = FX.one.pending[0]!.id;
      const { posts } = await open(page, FX.one, {
        [`/api/auth/signups/${id}/approve`]: async (route) => {
          await held;
          await fulfill(FX.outcomes.approve!)(route);
        },
      });
      await page.getByRole('button', { name: 'Approve @lc-1a2b' }).click();
      await expect(page.getByRole('button', { name: 'Approve @lc-1a2b' })).toBeDisabled();
      await expect(page.getByRole('button', { name: 'Reject @lc-1a2b' })).toBeDisabled();
      await checks(page);
      release();
      await expect(page.getByText('@lc-1a2b approved.')).toBeVisible();
      expect(posts).toEqual([{ path: `/api/auth/signups/${id}/approve`, body: {} }]);
    });

    for (const [outcome, message] of [
      ['reauth_required', 'Sign in again to approve or reject (required every 10 minutes for these actions).'],
      ['not_found', 'This request is no longer waiting.'],
      ['server_error', 'Could not reach reader-ai. Try again.'],
    ] as const) {
      test(`approve answered ${outcome} (FR-ACCOUNTS-2)`, async ({ page }) => {
        const id = FX.one.pending[0]!.id;
        const { posts } = await open(page, FX.one, { [`/api/auth/signups/${id}/approve`]: fulfill(FX.outcomes[outcome]!) });
        await page.getByRole('button', { name: 'Approve @lc-1a2b' }).click();
        await expect(page.getByText(message)).toBeVisible();
        if (outcome === 'reauth_required')
          await expect(page.getByRole('link', { name: /sign in again/i })).toHaveAttribute('href', '/api/auth/signin?next=/signups');
        await checks(page);
        expect(posts).toEqual([{ path: `/api/auth/signups/${id}/approve`, body: {} }]);
      });
    }

    test('disable flow (FR-ACCOUNTS-3)', async ({ page }) => {
      const maria = FX.one.accounts[0]!;
      const { posts } = await open(page, FX.one, { [`/api/auth/signups/${maria.id}/disable`]: fulfill(FX.outcomes.disable!) });
      await page.getByRole('button', { name: 'Disable @maria' }).click();
      // ADR-0037 (WEB-MANAGE paired change): Enable undoes a disable.
      await expect(page.getByText('Disable @maria? Their sessions end and they can no longer sign in. Enable undoes it.')).toBeVisible();
      await checks(page);
      await page.getByRole('button', { name: 'Disable', exact: true }).click();
      await expect.poll(() => posts).toEqual([{ path: `/api/auth/signups/${maria.id}/disable`, body: {} }]);
    });
  });
}
