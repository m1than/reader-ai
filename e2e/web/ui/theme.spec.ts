// @trace FR-UI-16
// [browser] Run on request: `cd web && npm run build && npm run e2e -- ui/theme`. The test server serves web/dist under
// the production CSP with the WEB-UI and WEB-NOTEBOOK fixtures.
// Design pass (2026-11-23): a stored choice wins over the system scheme from the first frame, and a change in the user
// menu recolours the page and the python code without a reload.
import { expect, test } from '@playwright/test';

test.use({ colorScheme: 'dark' });

const LIGHT_CANVAS = 'rgb(246, 244, 239)'; // calm.tokens.json light canvas #f6f4ef
const DARK_CANVAS = 'rgb(27, 26, 24)'; // dark canvas #1b1a18

test('a stored Light wins over a dark system scheme, and Dark from the menu recolours code without a reload', async ({ page }) => {
  await page.addInitScript(() => {
    if (!sessionStorage.getItem('seeded')) {
      localStorage.setItem('reader-ai.theme', 'light');
      sessionStorage.setItem('seeded', '1');
    }
    // Review B5: the attribute must already be set in the first animation frame (before first paint).
    requestAnimationFrame(() => {
      (window as unknown as { __firstFrame: string | null }).__firstFrame = document.documentElement.dataset.theme ?? null;
    });
  });
  await page.goto('/lessons/w01');
  await expect(page.getByRole('heading', { name: 'Digests' })).toBeVisible();
  expect(await page.evaluate(() => (window as unknown as { __firstFrame?: string | null }).__firstFrame)).toBe('light');
  const canvas = () => page.evaluate(() => getComputedStyle(document.documentElement).backgroundColor);
  expect(await canvas()).toBe(LIGHT_CANVAS);
  const keyword = page.locator('pre.shiki span', { hasText: /^import$/ }).first();
  await expect(keyword).toBeVisible();
  const before = await keyword.evaluate((el) => getComputedStyle(el).color);
  const marker = await page.evaluate(() => {
    (window as unknown as { __same: boolean }).__same = true;
    return true;
  });
  expect(marker).toBe(true);
  await page.getByRole('banner').getByRole('button', { name: /Ivan/ }).click();
  await page.getByRole('menuitemradio', { name: 'Dark' }).click();
  await expect.poll(canvas).toBe(DARK_CANVAS);
  const after = await keyword.evaluate((el) => getComputedStyle(el).color);
  expect(after).not.toBe(before);
  expect(await page.evaluate(() => (window as unknown as { __same?: boolean }).__same)).toBe(true);
  expect(await page.evaluate(() => localStorage.getItem('reader-ai.theme'))).toBe('dark');
});
