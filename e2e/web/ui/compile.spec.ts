// @trace FR-UI-15
// [browser] Run on request: `cd web && npm run build && npm run e2e -- ui/compile`. The test server
// (web/test-server) stubs a compile of w02 as a sequence — 404, then `running` at fact_check for two reads, then
// `converged` with a revision, after which the list links w02 — and `POST /__test__/reset` restores the start. No real
// compile runs and nothing is spent. Checks: Compile, the confirm (estimate and cap), "Compiling: checking facts",
// then the link; axe with no serious or critical issue at 1280, 1024 and 390 px on the list with a compiling row and
// with the dialog open; no horizontal scroll; no CSP violation. Screenshots go to $COMPILE_SHOTS when set.
import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

const WIDTHS = [1280, 1024, 390] as const;
const SHOTS = process.env.COMPILE_SHOTS;

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

const overflow = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

for (const width of WIDTHS) {
  test(`compile from the list at ${width} px: confirm, compiling, then a link`, async ({ page, request }) => {
    test.setTimeout(60_000);
    expect((await request.post('/__test__/reset')).status()).toBe(204);
    await collectViolations(page);
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/');
    const row = page.locator('li', { hasText: 'Fixture lesson W02: not compiled yet' });
    await expect(row.getByText('not compiled yet', { exact: true })).toBeVisible();
    expect(await serious(page)).toEqual([]);
    expect(await overflow(page)).toBeLessThanOrEqual(0);

    await row.getByRole('button', { name: 'Compile' }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByRole('heading', { name: 'Compile lesson 2?' })).toBeVisible();
    await expect(dialog.getByText(/^Estimate: \$\d+\.\d{2}–\$\d+\.\d{2}$/)).toBeVisible();
    await expect(dialog.getByText('Stops at $8.00.')).toBeVisible();
    expect(await serious(page)).toEqual([]);
    expect(await overflow(page)).toBeLessThanOrEqual(0);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/compile-confirm-${width}.png`, fullPage: true });

    await dialog.getByRole('button', { name: 'Compile' }).click();
    await expect(dialog).toBeHidden();
    await expect(row.getByText('Compiling: checking facts')).toBeVisible();
    await expect(row.getByText('Compiling: checking facts')).toBeFocused();
    expect(await serious(page)).toEqual([]);
    expect(await overflow(page)).toBeLessThanOrEqual(0);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/compile-running-${width}.png`, fullPage: true });

    // two polls at 5 s, then converged with a revision: the row becomes a link
    await expect(page.getByRole('link', { name: 'Fixture lesson W02: not compiled yet' })).toHaveAttribute('href', '/lessons/w02', { timeout: 20_000 });
    expect(await serious(page)).toEqual([]);

    const violations = await page.evaluate(() => (window as unknown as { __csp: string[] }).__csp);
    expect(violations).toEqual([]);
  });
}

test('a stopped compile shows its reason and Retry', async ({ page, request }) => {
  expect((await request.post('/__test__/reset?w02=stopped')).status()).toBe(204);
  await page.goto('/');
  const row = page.locator('li', { hasText: 'Fixture lesson W02: not compiled yet' });
  await expect(row.getByText(/^Compile stopped at drafting\. No AI provider is connected/)).toBeVisible();
  await expect(row.getByRole('button', { name: 'Retry' })).toBeVisible();
  expect(await serious(page)).toEqual([]);
  expect(await overflow(page)).toBeLessThanOrEqual(0);
});
