// Live screenshots and axe of /import against the running stack (run by plan_live.py; never confirms, spends
// nothing). Reads READER_URL, READER_CA_SPKI, READER_SESSION (this run's owner session, from plan_live.py's memory)
// and PLAN_SHOTS (a directory outside the repository: the screenshots show lesson titles, which are book text).
// Prints `PASS name` / `FAIL name` lines only.
// ESM ignores NODE_PATH: resolve the web app's own Playwright and axe from web/node_modules.
import { createRequire } from 'node:module';

const require = createRequire(`${process.env.NODE_PATH}/`);
const { chromium } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;

const url = process.env.READER_URL;
const dir = process.env.PLAN_SHOTS;
const line = (ok, name) => console.log(`${ok ? 'PASS' : 'FAIL'} ${name}`);

const browser = await chromium.launch({ args: [`--ignore-certificate-errors-spki-list=${process.env.READER_CA_SPKI}`] });
try {
  const context = await browser.newContext();
  await context.addCookies([
    { name: '__Host-reader_session', value: process.env.READER_SESSION, url, secure: true, httpOnly: true, sameSite: 'Lax' },
  ]);
  const page = await context.newPage();
  for (const width of [1280, 1024, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`${url}/import`);
    await page.getByRole('heading', { name: 'Import a source', level: 1 }).waitFor({ timeout: 20_000 });
    await page.getByRole('button', { name: /^(Plan study time|Re-plan)$/ }).first().click();
    const weeks = page.getByLabel('Total weeks');
    await weeks.fill(process.env.PLAN_WEEKS ?? '12');
    await page.getByLabel('Minutes per day').fill('60');
    await page.getByText(/lessons, one a day/).waitFor({ timeout: 20_000 });
    await page.getByText('Updating the estimate').waitFor({ state: 'detached', timeout: 20_000 }).catch(() => undefined);
    await page.waitForTimeout(800);
    const axe = (await new AxeBuilder({ page }).analyze()).violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');
    line(axe.length === 0, `plan: /import estimate has no serious or critical axe issue at ${width} px`);
    const scroll = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    line(scroll <= 0, `plan: /import does not scroll horizontally at ${width} px`);
    await page.screenshot({ path: `${dir}/live-import-estimate-${width}.png`, fullPage: true });
    if (width === 1280) {
      // The plan's "Lessons" disclosure: since the design pass "Lessons" is also a nav link and a breadcrumb.
      const lessons = page.locator('#content summary', { hasText: 'Lessons' });
      await lessons.click();
      await page.screenshot({ path: `${dir}/live-import-lessons-${width}.png`, fullPage: false });
      await lessons.click();
    }
    // the realistic does-not-fit state: the whole pilot at 12 weeks × 60 min
    await weeks.fill('12');
    await page.getByRole('button', { name: /^Use \d+ (min a day|weeks)$/ }).first().waitFor({ timeout: 20_000 });
    await page.waitForTimeout(800);
    const axe2 = (await new AxeBuilder({ page }).analyze()).violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');
    line(axe2.length === 0, `plan: /import does-not-fit has no serious or critical axe issue at ${width} px`);
    await page.screenshot({ path: `${dir}/live-import-does-not-fit-${width}.png`, fullPage: true });
  }
  await context.close();
} catch (e) {
  line(false, `plan: live /import walk (${String(e).split('\n')[0].slice(0, 120)})`);
  await browser.contexts()[0]?.pages()[0]?.screenshot({ path: `${dir}/live-import-failure.png` }).catch(() => undefined);
} finally {
  await browser.close();
}
