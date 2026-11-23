// @trace FR-UI-12 FR-IMPORT-5 FR-IMPORT-8
// [browser] Run on request: `cd web && npm run build && npm run e2e -- import/`. The test server (web/test-server)
// stubs /api/materials and the plan routes with a synthetic 30-page source; no book text. Checks the /import page
// under the production CSP, axe with no serious or critical issue at 1280, 1024 and 390 px, no horizontal scroll,
// and writes screenshots to $PLAN_SHOTS when set.
import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

const WIDTHS = [1280, 1024, 390] as const;
const SHOTS = process.env.PLAN_SHOTS;

const collectViolations = async (page: Page) => {
  await page.addInitScript(() => {
    (window as unknown as { __csp: string[] }).__csp = [];
    document.addEventListener('securitypolicyviolation', (e) => {
      (window as unknown as { __csp: string[] }).__csp.push(`${e.violatedDirective} ${e.blockedURI}`);
    });
  });
};

const serious = async (page: Page) =>
  (await new AxeBuilder({ page }).analyze()).violations
    .filter((v) => v.impact === 'serious' || v.impact === 'critical')
    .map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`);

const noScroll = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

for (const width of WIDTHS) {
  test(`/import at ${width} px: sources, form, estimate, does-not-fit and confirm`, async ({ page }) => {
    await collectViolations(page);
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/');
    await page.getByRole('link', { name: 'Import' }).click();
    await expect(page.getByRole('heading', { name: 'Import a source', level: 1 })).toBeVisible();
    await expect(page.getByText('synthetic-plan')).toBeVisible();
    expect(await serious(page)).toEqual([]);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/import-sources-${width}.png`, fullPage: true });

    await page.getByRole('button', { name: 'Plan study time' }).click();
    await page.getByLabel('Total weeks').fill('4');
    // Design pass (FR-IMPORT-5): the headline is the status line; the count is the "Lessons" figure.
    await expect(page.locator('.plan-estimate [role="status"]')).toHaveText(/^Estimate updated: 28 lessons, one a day/);
    await expect(page.locator('.plan-figures dd').first()).toHaveText('28');
    await page.locator('summary', { hasText: 'Lessons' }).click();
    expect(await serious(page)).toEqual([]);
    expect(await noScroll(page)).toBeLessThanOrEqual(0);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/import-estimate-${width}.png`, fullPage: true });

    await page.getByLabel('Total weeks').fill('1');
    await page.getByLabel('Minutes per day').fill('10');
    await expect(page.getByRole('button', { name: /^Use \d+ min a day$/ })).toBeVisible();
    expect(await serious(page)).toEqual([]);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/import-does-not-fit-${width}.png`, fullPage: true });

    await page.getByRole('button', { name: 'Use 2 weeks' }).click();
    await page.getByLabel('Minutes per day').fill('60');
    await expect(page.getByRole('button', { name: 'Create the course' })).toBeEnabled();
    await page.getByRole('checkbox').uncheck();
    await page.getByRole('button', { name: 'Create the course' }).click();
    await expect(page.getByText('Course created: 1 lesson.')).toBeVisible();
    expect(await serious(page)).toEqual([]);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/import-confirmed-${width}.png`, fullPage: true });

    const violations = await page.evaluate(() => (window as unknown as { __csp: string[] }).__csp);
    expect(violations).toEqual([]);
  });
}
