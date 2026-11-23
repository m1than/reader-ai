// @trace FR-NOTEBOOK-9
// [browser] Run on request: `cd web && npm run build && npm run e2e -- notebook/`. The test server serves the W01
// fixture under the production CSP; the kernel WebSocket is routed by Playwright, so no kernel runs.
// Design pass (2026-11-23): the lab's CodeMirror 6 editor mounts in a shadow root without any CSP violation, is styled
// (spike codemirror-bundle: unstyled in the document under this CSP), runs the edited code on Shift+Enter, and lets
// Escape then Tab leave the editor.
import { expect, test } from '@playwright/test';

test('the lab editor mounts styled under the CSP, runs edits on Shift+Enter and releases focus', async ({ page }) => {
  await page.addInitScript(() => {
    (window as unknown as { __csp: string[] }).__csp = [];
    document.addEventListener('securitypolicyviolation', (e) => {
      (window as unknown as { __csp: string[] }).__csp.push(`${e.violatedDirective} ${e.blockedURI}`);
    });
  });
  const sent: string[] = [];
  await page.routeWebSocket(/\/api\/kernel\/sessions\/[^/]+\/channels$/, (ws) => {
    ws.onMessage((m) => {
      sent.push(String(m));
    });
  });
  await page.goto('/lessons/w01');
  const lab = page.locator('[data-cell-id="w01-lab"]');
  await expect(lab.getByText('Python · Lab 1')).toBeVisible();
  // The editor lives in an open shadow root; Playwright's CSS locators pierce it.
  const content = lab.locator('.cm-content');
  await expect(content).toBeVisible({ timeout: 15_000 });
  await expect(content).toHaveAttribute('aria-label', 'Lab 1 code');
  const gutters = await lab.locator('.cm-gutters').evaluate((el) => getComputedStyle(el).display);
  expect(gutters).toBe('flex');
  await content.click();
  await page.keyboard.press('ControlOrMeta+End');
  await page.keyboard.type('\nprint("edited")');
  await page.keyboard.press('Shift+Enter');
  await expect.poll(() => sent.length, { timeout: 10_000 }).toBeGreaterThan(0);
  const execute = JSON.parse(sent[0]!) as { content: { code: string } };
  expect(execute.content.code).toContain('print("edited")');
  await content.click();
  await page.keyboard.press('Escape');
  await page.keyboard.press('Tab');
  const inEditor = await page.evaluate(() => {
    const host = document.activeElement;
    return Boolean(host?.shadowRoot?.activeElement?.closest('.cm-editor'));
  });
  expect(inEditor).toBe(false);
  const violations = await page.evaluate(() => (window as unknown as { __csp: string[] }).__csp);
  expect(violations).toEqual([]);
});
