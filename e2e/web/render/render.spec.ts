// @trace INV-RENDER-2 FR-RENDER-3 FR-RENDER-5 VEC-RENDER-1
// [browser] Run on request: `cd web && npx vite build && npm run build-storybook && NODE_PATH=$PWD/node_modules npx playwright test`.
// The stories render VEC-RENDER-1's `svg-script` case, a PNG and an inline `\raisebox`; the test server serves the
// built Storybook under /storybook/.
import { expect, test } from '@playwright/test';

const story = (id: string) => `/storybook/iframe.html?id=${id}&viewMode=story`;

test('svg-script renders as text: no SVG blob, no script runs (INV-RENDER-2)', async ({ page }) => {
  const dialogs: string[] = [];
  page.on('dialog', (d) => {
    dialogs.push(d.message());
    void d.dismiss();
  });
  await page.goto(story('render-output--svg-script'));
  await page.waitForSelector('#storybook-root [data-output="text"]');
  expect(await page.locator('#storybook-root pre').textContent()).toContain('<script>alert(1)</script>');
  expect(await page.locator('img[src^="blob:"]').count()).toBe(0);
  expect(await page.locator('#storybook-root svg').count()).toBe(0);
  await page.waitForTimeout(300);
  expect(dialogs).toEqual([]);
});

test('a PNG blob URL is revoked once the image has loaded (FR-RENDER-5)', async ({ page }) => {
  await page.goto(story('render-output--png'));
  const img = page.locator('#storybook-root img');
  await expect(img).toHaveJSProperty('complete', true);
  const src = await img.getAttribute('src');
  expect(src).toMatch(/^blob:/);
  const stillFetchable = await page.evaluate(async (url) => {
    try {
      await fetch(url!);
      return true;
    } catch {
      return false;
    }
  }, src);
  expect(stillFetchable).toBe(false);
});

test('inline math cannot spill outside its line (FR-RENDER-3)', async ({ page }) => {
  await page.goto(story('render-markdown--raise-box'));
  await page.waitForSelector('#storybook-root .katex');
  const box = await page.evaluate(() => {
    const k = document.querySelector('#storybook-root [data-markdown] .katex') as HTMLElement;
    const cs = getComputedStyle(k);
    return { height: k.getBoundingClientRect().height, contain: cs.contain, overflow: cs.overflow, page: document.documentElement.scrollHeight };
  });
  expect(box.contain).toContain('paint');
  expect(box.overflow).toBe('hidden');
  expect(box.height).toBeLessThan(100);
  expect(box.page).toBeLessThan(2000);
});
