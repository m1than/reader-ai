// @trace FR-UI-11
// [browser] Run on request: `cd web && npm run build && npm run e2e -- ui/header`. The header's main nav at narrow
// and wide widths for the owner of the test server (four entries; one sign-up pending). Live fix 2026-11-24: at 390 px
// the four entries overflowed the header by 119 px on every page; below 40rem the nav now lives in the Courses drawer.
import { expect, test, type Page } from '@playwright/test';

const overflow = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
const ENTRIES = ['Lessons', 'Import', 'Sign-ups, 1 waiting', 'Observability'];

test.beforeEach(async ({ request }) => {
  expect((await request.post('/__test__/reset')).status()).toBe(204);
});

test('at 390 px the nav is in the Courses drawer and no page scrolls sideways', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  for (const path of ['/', '/lessons/w01', '/no-such-page', '/import']) {
    await page.goto(path);
    await expect(page.locator('#content h1').first()).toBeVisible();
    await expect(page.locator('header.app-header nav.main-nav')).toBeHidden();
    expect(await overflow(page), `overflow on ${path}`).toBeLessThanOrEqual(0);
  }
  await page.goto('/');
  await page.getByRole('button', { name: 'Courses' }).click();
  const nav = page.getByRole('navigation', { name: 'Main' });
  await expect(nav).toHaveCount(1);
  await expect(nav).toHaveClass(/drawer-nav/);
  for (const name of ENTRIES) await expect(nav.getByRole('link', { name, exact: true })).toBeVisible();
  await nav.getByRole('link', { name: 'Import', exact: true }).click();
  await expect(page).toHaveURL(/\/import$/);
  await expect(page.locator('nav.drawer-nav')).toHaveCount(0);
});

for (const width of [1024, 1280])
  test(`at ${width} px the header holds the nav`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/');
    const nav = page.getByRole('navigation', { name: 'Main' });
    await expect(nav).toHaveCount(1);
    await expect(nav).toHaveClass(/main-nav/);
    for (const name of ENTRIES) await expect(nav.getByRole('link', { name, exact: true })).toBeVisible();
    expect(await overflow(page)).toBeLessThanOrEqual(0);
    if (width === 1024) {
      // The drawer (below 75rem) does not repeat the nav from 40rem up.
      await page.getByRole('button', { name: 'Courses' }).click();
      await expect(page.locator('#course-drawer')).toBeVisible();
      await expect(page.locator('nav.drawer-nav')).toBeHidden();
    }
  });
