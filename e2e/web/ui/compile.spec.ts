// @trace FR-UI-15
// [browser] Run on request: `cd web && npm run build && npm run e2e -- ui/compile`. The test server
// (web/test-server) stubs a compile of w02 shaped like a real one: no run until the POST; then `running` at
// fact_check with the draft's revision already set (the list links w02 with the draft chip) for two reads; then
// `converged`, when w02 leaves GET /api/compiles. `POST /__test__/reset` restores the start (`?w02=stopped`: a
// stage_failed run with a revision and `error: provider_missing`). The state is global, so the Playwright config runs
// one worker. No real compile runs and nothing is spent. Checks: Compile, the confirm (estimate and cap), a link with
// the chip and "Compiling: checking facts", then the chip gone; the stopped state; axe with no serious or critical
// issue at 1280, 1024 and 390 px; no horizontal scroll; no CSP violation. Screenshots go to $COMPILE_SHOTS when set.
import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

const WIDTHS = [1280, 1024, 390] as const;
const SHOTS = process.env.COMPILE_SHOTS;
const TITLE = 'Fixture lesson W02: not compiled yet';
const CHIP = 'draft, compile not finished';

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
  test(`compile from the list at ${width} px: confirm, a draft still compiling, then finished`, async ({ page, request }) => {
    test.setTimeout(60_000);
    expect((await request.post('/__test__/reset')).status()).toBe(204);
    await collectViolations(page);
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/');
    const row = page.locator('li', { hasText: TITLE });
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
    // the draft's revision exists from the first running read: a link, but with the chip and the run's state
    await expect(row.getByRole('link', { name: TITLE })).toHaveAttribute('href', '/lessons/w02');
    await expect(row.getByText(CHIP)).toBeVisible();
    await expect(row.getByText('Compiling: checking facts')).toBeVisible();
    expect(await serious(page)).toEqual([]);
    expect(await overflow(page)).toBeLessThanOrEqual(0);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/compile-running-${width}.png`, fullPage: true });

    // two polls at 5 s, then converged: the run leaves the open runs and the chip goes
    await expect(row.getByText(CHIP)).toBeHidden({ timeout: 20_000 });
    await expect(row.getByRole('link', { name: TITLE })).toBeVisible();
    expect(await serious(page)).toEqual([]);

    const violations = await page.evaluate(() => (window as unknown as { __csp: string[] }).__csp);
    expect(violations).toEqual([]);
  });
}

test('a stopped compile of a drafted lesson: the link, the chip, its reason and Retry', async ({ page, request }) => {
  expect((await request.post('/__test__/reset?w02=stopped')).status()).toBe(204);
  for (const width of WIDTHS) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/');
    const row = page.locator('li', { hasText: TITLE });
    await expect(row.getByRole('link', { name: TITLE })).toBeVisible();
    await expect(row.getByText(CHIP)).toBeVisible();
    await expect(row.getByText(/^Compile stopped at checking facts\. No AI provider is connected/)).toBeVisible();
    await expect(row.getByRole('button', { name: 'Retry' })).toBeVisible();
    expect(await serious(page)).toEqual([]);
    expect(await overflow(page)).toBeLessThanOrEqual(0);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/compile-stopped-${width}.png`, fullPage: true });
  }
});
