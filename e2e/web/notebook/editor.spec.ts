// @trace FR-NOTEBOOK-9 FR-NOTEBOOK-11
// [browser] Run on request: `cd web && npm run build && npm run e2e -- notebook/`. The test server serves the W01
// fixture under the production CSP; the kernel WebSocket is routed by Playwright, so no kernel runs.
// Design pass (2026-11-23): the lab's CodeMirror 6 editor mounts in a shadow root without any CSP violation, is styled
// (spike codemirror-bundle: unstyled in the document under this CSP), is a described textbox with a focus outline and
// no serious axe issue, takes `j k / ?` as text (the lesson shortcuts do not fire inside it, review B4), runs the
// edited code on Shift+Enter, and lets Escape then Tab leave the editor.
import AxeBuilder from '@axe-core/playwright';
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
  await expect(content).toHaveAttribute('role', 'textbox');
  const gutters = await lab.locator('.cm-gutters').evaluate((el) => getComputedStyle(el).display);
  expect(gutters).toBe('flex');
  // The description resolves inside the same shadow root (aria-describedby does not cross it).
  const description = await content.evaluate((el) => {
    const id = el.getAttribute('aria-describedby') ?? '';
    const root = el.getRootNode() as ShadowRoot;
    return id ? root.getElementById(id)?.textContent ?? null : null;
  });
  expect(description).toBe('Press Escape, then Tab, to leave the editor.');
  const axe = await new AxeBuilder({ page }).include('[data-cell-id="w01-lab"]').analyze();
  expect(axe.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical').map((v) => v.id)).toEqual([]);

  await content.click();
  const outline = await lab.locator('.cm-editor').evaluate((el) => {
    const cs = getComputedStyle(el);
    return `${cs.outlineWidth} ${cs.outlineStyle}`;
  });
  expect(outline).toBe('2px solid');
  // Shortcut keys typed in the editor are text: no cell moves, no tutor, no dialog.
  await page.keyboard.press('ControlOrMeta+End');
  await page.keyboard.type('\n# j k / ?');
  await expect(content).toContainText('# j k / ?');
  expect(await page.evaluate(() => Boolean(document.activeElement?.shadowRoot?.activeElement?.closest('.cm-editor')))).toBe(true);
  await expect(page.locator('#tutor')).toBeHidden();
  await expect(page.getByRole('dialog')).toHaveCount(0);

  await page.keyboard.type('\nprint("edited")');
  await page.keyboard.press('Shift+Enter');
  await expect.poll(() => sent.length, { timeout: 10_000 }).toBeGreaterThan(0);
  const execute = JSON.parse(sent[0]!) as { content: { code: string } };
  expect(execute.content.code).toContain('print("edited")');
  expect(execute.content.code).toContain('# j k / ?');
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

// FR-NOTEBOOK-9, inserted text (live fix 2026-11-24, narrowed in code review): only an `insertText` outside composition
// over a non-empty selection is taken over (Chromium's own edit of that case wrote a CSP-blocked style attribute); it
// lands on the event's target ranges when it has any, else on the selection. Caret-only typing, composition and
// `insertReplacementText` stay with CodeMirror and the browser (macOS double-space period, text replacement).
// Synthetic events are dispatched in the page; one that is not taken over changes nothing (it is untrusted), so
// `defaultPrevented` and the document tell the cases apart.
test('the lab editor takes over only insertText over a selection, on its target ranges', async ({ page }) => {
  await page.addInitScript(() => {
    (window as unknown as { __csp: string[] }).__csp = [];
    document.addEventListener('securitypolicyviolation', (e) => {
      (window as unknown as { __csp: string[] }).__csp.push(`${e.violatedDirective} ${e.blockedURI}`);
    });
  });
  await page.routeWebSocket(/\/api\/kernel\/sessions\/[^/]+\/channels$/, () => undefined);
  await page.goto('/lessons/w01');
  const content = page.locator('[data-cell-id="w01-lab"] .cm-content');
  await expect(content).toBeVisible({ timeout: 15_000 });
  await content.click();
  await page.keyboard.press('ControlOrMeta+a');
  await page.keyboard.type('abcdef');
  await expect(content).toHaveText('abcdef');

  /** Dispatches a beforeinput on the content; `range` is a [start, end] offset into the first line's text. */
  const fire = (o: { inputType: string; data: string | null; isComposing?: boolean; range?: [number, number] }) =>
    content.evaluate((el, a) => {
      const line = el.querySelector('.cm-line')!;
      const text = document.createTreeWalker(line, NodeFilter.SHOW_TEXT).nextNode()!;
      const targetRanges = a.range
        ? [new StaticRange({ startContainer: text, startOffset: a.range[0], endContainer: text, endOffset: a.range[1] })]
        : [];
      const ev = new InputEvent('beforeinput', {
        inputType: a.inputType,
        data: a.data,
        isComposing: a.isComposing ?? false,
        targetRanges,
        bubbles: true,
        cancelable: true,
        composed: true,
      });
      el.dispatchEvent(ev);
      return ev.defaultPrevented;
    }, o);

  // A caret only (every selection range empty): left to CodeMirror.
  await page.keyboard.press('End');
  expect(await fire({ inputType: 'insertText', data: 'X' })).toBe(false);
  await expect(content).toHaveText('abcdef');
  await page.keyboard.insertText('g');
  await expect(content).toHaveText('abcdefg');

  // Over a selection, composition and insertReplacementText are not taken over.
  await page.keyboard.press('ControlOrMeta+a');
  expect(await fire({ inputType: 'insertText', data: 'X', isComposing: true })).toBe(false);
  expect(await fire({ inputType: 'insertReplacementText', data: 'X' })).toBe(false);
  await expect(content).toHaveText('abcdefg');

  // Over a selection with a target range that is not the selection (the macOS double-space shape): the target wins.
  expect(await fire({ inputType: 'insertText', data: '. ', range: [3, 4] })).toBe(true);
  await expect(content).toHaveText('abc. efg');
  // Over a selection without target ranges: the selection is replaced.
  await page.keyboard.press('ControlOrMeta+a');
  expect(await fire({ inputType: 'insertText', data: 'x = 1' })).toBe(true);
  await expect(content).toHaveText('x = 1');
  // A real insertion over a selection across highlighted spans: no CSP report.
  await page.keyboard.press('ControlOrMeta+a');
  await page.keyboard.insertText('print("hi")');
  await expect(content).toHaveText('print("hi")');
  expect(await page.evaluate(() => (window as unknown as { __csp: string[] }).__csp)).toEqual([]);
});
