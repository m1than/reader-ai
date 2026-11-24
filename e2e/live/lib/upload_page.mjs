// Live browser upload on /import (run by upload_live.py; spends nothing). Reads READER_URL, READER_CA_SPKI,
// READER_SESSION (this run's owner session, from upload_live.py's memory) and UPLOAD_PDF (VEC-INGEST-2's synthetic
// PDF). Uploads it through the drop zone's file input, follows the step list to Ready, and runs axe at 1280, 1024 and
// 390 px with the CSP listener on. Prints `PASS name` / `FAIL name` lines only.
import { createRequire } from 'node:module';

const require = createRequire(`${process.env.NODE_PATH}/`);
const { chromium } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

const url = process.env.READER_URL;
const line = (ok, name, detail = '') => console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${ok || !detail ? '' : `\t${detail}`}`);
const serious = async (page) =>
  (await new AxeBuilder({ page }).analyze()).violations.filter((v) => v.impact === 'serious' || v.impact === 'critical').map((v) => v.id);

const browser = await chromium.launch({ args: [`--ignore-certificate-errors-spki-list=${process.env.READER_CA_SPKI}`] });
try {
  const context = await browser.newContext();
  await context.addCookies([
    { name: '__Host-reader_session', value: process.env.READER_SESSION, url, secure: true, httpOnly: true, sameSite: 'Lax' },
  ]);
  const page = await context.newPage();
  await page.addInitScript(() => {
    window.__csp = [];
    document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(e.violatedDirective));
  });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto(`${url}/import`);
  await page.getByRole('heading', { name: 'Import a source', level: 1 }).waitFor({ timeout: 20_000 });
  const zone = page.getByRole('region', { name: 'Upload a PDF' });
  await zone.locator('input[type="file"]').setInputFiles(process.env.UPLOAD_PDF);
  await zone.getByRole('combobox').selectOption('other');
  await page.getByRole('button', { name: 'Upload', exact: true }).click();
  const steps = page.getByRole('list', { name: 'Steps' });
  let reached = false;
  try {
    await steps.locator('li[aria-current="step"]', { hasText: 'Ready' }).waitFor({ timeout: 15 * 60_000 });
    reached = true;
  } catch {
    reached = false;
  }
  line(reached, 'upload: the browser follows the upload to Ready');
  for (const width of [1280, 1024, 390]) {
    await page.setViewportSize({ width, height: 900 });
    const found = await serious(page);
    line(found.length === 0, `upload: axe finds no serious issue on /import at ${width} px`, found.join(','));
    const scroll = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    line(scroll <= 0, `upload: no horizontal scroll at ${width} px`, String(scroll));
  }
  const violations = await page.evaluate(() => window.__csp);
  line(violations.length === 0, 'upload: no CSP violation', violations.join(','));
} finally {
  await browser.close();
}
