// @trace INV-IMPORT-3 FR-IMPORT-9 FR-UI-13
// [browser] Run on request: `cd web && npm run build && npm run e2e -- import/upload`. The test server (web/test-server)
// stubs the upload routes from spec/fixtures/WEB-IMPORT/responses.json: an upload walks queued → converting →
// importing → indexing → ready, one step per poll. Checks the upload sections under the production CSP, axe with no
// serious or critical issue at 1280, 1024 and 390 px in each state, no horizontal scroll, keyboard reach of the one
// "Choose a PDF" button, and the first-run redirect with an empty /api/materials.
import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

const WIDTHS = [1280, 1024, 390] as const;
const SHOTS = process.env.UPLOAD_SHOTS;
const PDF = Buffer.from('%PDF-1.7\n' + 'x'.repeat(2000) + '\n%%EOF\n');

const watchCsp = async (page: Page) => {
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
const csp = (page: Page) => page.evaluate(() => (window as unknown as { __csp: string[] }).__csp);

for (const width of WIDTHS) {
  test(`upload at ${width} px walks every state under the CSP`, async ({ page }) => {
    await watchCsp(page);
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/import');
    await expect(page.getByRole('heading', { name: 'Import a source', level: 1 })).toBeVisible();
    const zone = page.getByRole('region', { name: 'Upload a PDF' });
    await expect(zone.getByRole('button', { name: 'Choose a PDF' })).toBeVisible();
    expect(await serious(page)).toEqual([]);
    expect(await noScroll(page)).toBeLessThanOrEqual(0);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/upload-empty-${width}.png`, fullPage: true });

    await zone.locator('input[type="file"]').setInputFiles({ name: 'Synthetic chapter 1.pdf', mimeType: 'application/pdf', buffer: PDF });
    await page.getByRole('button', { name: 'Upload' }).click();
    const steps = page.getByRole('list', { name: 'Steps' });
    for (const step of ['Uploaded', 'Converting on your Mac', 'Importing', 'Indexing for search', 'Ready']) {
      await expect(steps.locator('li[aria-current="step"]')).toHaveText(step, { timeout: 15_000 });
      expect(await serious(page)).toEqual([]);
      expect(await noScroll(page)).toBeLessThanOrEqual(0);
      if (SHOTS) await page.screenshot({ path: `${SHOTS}/upload-${step.split(' ')[0]!.toLowerCase()}-${width}.png`, fullPage: true });
    }
    expect(await csp(page)).toEqual([]);
  });
}

test('the one Choose a PDF button is reachable by keyboard', async ({ page }) => {
  await page.goto('/import');
  await expect(page.getByRole('heading', { name: 'Import a source', level: 1 })).toBeVisible();
  let reached = false;
  for (let i = 0; i < 30 && !reached; i++) {
    await page.keyboard.press('Tab');
    reached = await page.evaluate(() => document.activeElement?.textContent?.trim() === 'Choose a PDF');
  }
  expect(reached).toBe(true);
});

test('first run: an owner with no lessons and no materials lands on /import', async ({ page, context }) => {
  await context.addCookies([{ name: 'stub-first-run', value: '1', domain: '127.0.0.1', path: '/' }]);
  await watchCsp(page);
  await page.goto('/');
  await expect(page).toHaveURL(/\/import$/);
  await expect(page.getByRole('heading', { name: 'Import a source', level: 1 })).toBeVisible();
  expect(await csp(page)).toEqual([]);
});
