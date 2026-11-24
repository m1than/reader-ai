// @trace FR-UI-11
// [browser] Run on request: `cd web && npm run build && npm run e2e -- ui/header`. The header's main nav for the owner
// of the test server (four entries; one sign-up pending). Live fix 2026-11-24: at 390 px the four entries overflowed
// the header by 119 px on every page; code review: a 40rem breakpoint still overflowed up to about 847 px with a long
// display name. Below 75rem (the Courses breakpoint: a CSS width under 1200 px) the nav lives in the Courses drawer.
import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

/** Below the Courses breakpoint: `max-width: 74.98rem` holds exactly for widths under 1200 px. */
const BELOW = (width: number) => width < 1200;
const NAMES = [
  'Ivan',
  'Abcdefghij Klmnopqrst Uvwxyzabcd Efghijklmn Opqrstuvwx Yzabcdefg', // 64 code points (WEB-ACCOUNTS long_name)
  'Александра Константиновна Ивановская', // 36 characters
];
const WIDTHS = [390, 641, 768, 1024, 1199, 1200, 1280];
const PAGES = ['/', '/lessons/w01', '/no-such-page', '/import'];
const ENTRIES = ['Lessons', 'Import', 'Sign-ups 1 waiting', 'Observability'];

const overflow = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
const asOwnerNamed = (page: Page, name: string) =>
  page.route('**/api/me', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ id: '0b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b10', handle: 'ivan', role: 'owner', display_name: name, consent: { evaluation: true, tuning: false } }),
    }),
  );

test.beforeEach(async ({ request }) => {
  expect((await request.post('/__test__/reset')).status()).toBe(204);
});

test('the display names are the lengths the spec names', () => {
  expect([...NAMES[1]!].length).toBe(64);
  expect([...NAMES[2]!].length).toBe(36);
});

for (const width of WIDTHS)
  test(`at ${width} px no page scrolls sideways and the nav sits ${BELOW(width) ? 'in the drawer' : 'in the header'}`, async ({ page }) => {
    test.setTimeout(90_000);
    await page.setViewportSize({ width, height: 900 });
    for (const name of NAMES) {
      await page.unrouteAll();
      await asOwnerNamed(page, name);
      for (const path of PAGES) {
        await page.goto(path);
        await expect(page.locator('#content h1').first()).toBeVisible();
        expect(await overflow(page), `overflow on ${path} with "${name}"`).toBeLessThanOrEqual(0);
        if (BELOW(width)) await expect(page.locator('header.app-header nav.main-nav')).toBeHidden();
        else await expect(page.locator('header.app-header nav.main-nav')).toBeVisible();
      }
    }
    await page.goto('/');
    let nav = page.getByRole('navigation', { name: 'Main' });
    if (BELOW(width)) {
      await page.getByRole('button', { name: 'Courses' }).click();
      nav = page.locator('nav.drawer-nav');
      await expect(nav).toBeVisible();
      const axe = await new AxeBuilder({ page }).analyze();
      expect(axe.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical').map((v) => v.id)).toEqual([]);
    } else {
      await expect(nav).toHaveClass(/main-nav/);
    }
    // The names Chromium computes (the badge's "waiting" visually hidden).
    for (const entry of ENTRIES) await expect(nav.getByRole('link', { name: entry, exact: true })).toBeVisible();
    if (BELOW(width)) {
      await nav.getByRole('link', { name: 'Import', exact: true }).click();
      await expect(page).toHaveURL(/\/import$/);
      await expect(page.locator('nav.drawer-nav')).toHaveCount(0);
    }
  });
