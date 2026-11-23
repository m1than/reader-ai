// @trace INV-UI-4 INV-UI-3
// [browser] Run on request: `cd web && npm run build && npm run build-storybook && npx playwright test`.
// The test server (web/test-server/server.mjs) serves web/dist with the CSP read from deploy/Caddyfile and stubs the
// API with the WEB-UI and WEB-NOTEBOOK fixtures.
import { expect, test, type Page } from '@playwright/test';

const collectViolations = async (page: Page) => {
  await page.addInitScript(() => {
    (window as unknown as { __csp: string[] }).__csp = [];
    document.addEventListener('securitypolicyviolation', (e) => {
      (window as unknown as { __csp: string[] }).__csp.push(`${e.violatedDirective} ${e.blockedURI}`);
    });
  });
};

test('no CSP violation on the lesson list and the W01 lesson (INV-UI-4)', async ({ page }) => {
  await collectViolations(page);
  const consoleErrors: string[] = [];
  page.on('console', (m) => {
    if (m.type() === 'error') consoleErrors.push(m.text());
  });
  await page.goto('/');
  // Design pass: the course title also appears in the sidebar and the Continue block; wait for its list group.
  await expect(page.locator('#content #course-c-fixture')).toBeVisible();
  await page.goto('/lessons/w01');
  await expect(page.getByRole('heading', { name: 'Digests' })).toBeVisible();
  await expect(page.locator('.katex').first()).toBeVisible();
  await expect(page.locator('pre.shiki').first()).toBeVisible();
  const violations = await page.evaluate(() => (window as unknown as { __csp: string[] }).__csp);
  expect(violations).toEqual([]);
  expect(consoleErrors.filter((e) => /Content Security Policy/i.test(e))).toEqual([]);
});

test('stories have no gradients, glass or glow in light and dark (INV-UI-3)', async ({ page, request }) => {
  const index = (await (await request.get('/storybook/index.json')).json()) as { entries: Record<string, { type: string; id: string }> };
  const stories = Object.values(index.entries).filter((e) => e.type === 'story');
  expect(stories.length).toBeGreaterThan(0);
  for (const story of stories) {
    await page.goto(`/storybook/iframe.html?id=${story.id}&viewMode=story`);
    await page.waitForSelector('#storybook-root > *', { timeout: 10_000 });
    const bad = await page.evaluate(() => {
      const out: string[] = [];
      for (const el of Array.from(document.querySelectorAll('#storybook-root *'))) {
        const cs = getComputedStyle(el);
        if (cs.backgroundImage !== 'none' && !/^url\("?blob:/.test(cs.backgroundImage)) out.push(`background-image ${cs.backgroundImage}`);
        if ((cs as unknown as { backdropFilter?: string }).backdropFilter && cs.backdropFilter !== 'none') out.push(`backdrop-filter ${cs.backdropFilter}`);
        for (const layer of cs.boxShadow === 'none' ? [] : cs.boxShadow.split(/,(?![^(]*\))/)) {
          const lengths = layer.replace(/rgba?\([^)]*\)/g, '').match(/-?\d+(\.\d+)?px/g) ?? [];
          const blur = Number.parseFloat(lengths[2] ?? '0');
          if (blur > 12) out.push(`box-shadow blur ${blur}px`);
        }
      }
      return out;
    });
    expect(bad, story.id).toEqual([]);
  }
});
