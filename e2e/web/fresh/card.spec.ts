// @trace FR-FRESHUI-7
// [browser] Run on request: `cd web && npm run build && npm run e2e -- fresh/`. The test server stubs the freshness
// routes from VEC-FRESHUI-1 (spec/fixtures/WEB-FRESH/bodies.json); nothing is spent.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

// `npm run e2e` runs in web/.
const bodies = JSON.parse(readFileSync(resolve(process.cwd(), '../spec/fixtures/WEB-FRESH/bodies.json'), 'utf8')) as {
  states: Record<string, { material_id: string }>;
};

const WIDTHS = [1280, 1024, 390];
const STATES = ['empty', 'estimate', 'running', 'current', 'outdated', 'failed'];

const open = async (page: Page, state: string) => {
  await page.goto(`/materials/${bodies.states[state]!.material_id}/freshness`);
  const card = page.locator('.fresh');
  await expect(card).toBeVisible();
  if (state === 'estimate') {
    await card.getByRole('button', { name: 'Run check' }).click();
    await expect(card.getByText('About $0.27–$0.39, at most $1.00')).toBeVisible();
  }
  return card;
};

for (const width of WIDTHS) {
  for (const state of STATES) {
    test(`${state} at ${width} px: no serious or critical axe issue, headings in order`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await open(page, state);
      const results = await new AxeBuilder({ page }).include('main').analyze();
      const bad = results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');
      expect(bad.map((v) => v.id)).toEqual([]);
      const levels = await page.locator('main').locator('h1, h2, h3, h4').evaluateAll((hs) => hs.map((h) => Number(h.tagName.slice(1))));
      expect(levels[0]).toBe(1);
      for (let i = 1; i < levels.length; i += 1) expect(levels[i]! - levels[i - 1]!).toBeLessThanOrEqual(1);
    });
  }
}

test('a 2,048-character URL wraps at 390 px without horizontal overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  const id = bodies.states.outdated!.material_id;
  await page.route(`**/api/materials/${id}/freshness`, async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    const long = `https://courses.example/${'a'.repeat(2048 - 'https://courses.example/'.length)}`;
    body.report.recommendations[3].url = long;
    body.report.recommendations[4].url = `javascript:${'b'.repeat(2037)}`;
    await route.fulfill({ json: body });
  });
  await open(page, 'outdated');
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});
