// @trace FR-TUTOR-11
// [browser] Run on request: `cd web && npm run build && npm run e2e -- tutor/`. The test server serves the
// WEB-NOTEBOOK lesson fixture; the tutor routes are stubbed here, so no model is called.
import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';

const QID = '22222222-2222-4222-8222-222222222222';
const answered = {
  state: 'answered',
  grounded: false,
  answer: {
    blocks: [
      {
        text: 'Miners vary the **nonce**; see [the paper](https://bitcoin.org/bitcoin.pdf).',
        supported: true,
        quote: 'Miners vary the nonce until the hash',
        citations: [{ span: `${'c'.repeat(64)}:0`, page: 61, material_title: 'Mastering Blockchain' }],
      },
      { text: 'An invented claim.', supported: false, quote: 'nothing like it', citations: [] },
    ],
    insufficient: false,
    retrieval: 'ok',
    prompt_sha256: 'b'.repeat(64),
    model: 'claude-sonnet-5-5',
  },
};

test('the Ask panel asks, shows a checked answer and has no serious or critical axe issue', async ({ page }) => {
  let polls = 0;
  await page.route('**/api/lessons/*/questions', (route) => route.fulfill({ json: { questions: [] } }));
  await page.route('**/api/lessons/*/ask', (route) => route.fulfill({ status: 202, json: { question_id: QID } }));
  await page.route(`**/api/lessons/*/questions/${QID}`, (route) => {
    polls += 1;
    return route.fulfill({ json: polls < 2 ? { state: 'queued' } : answered });
  });
  await page.goto('/lessons/w01');
  const panel = page.getByRole('region', { name: 'Tutor' });
  // Design pass: the panel is closed by default and opens from the lesson toolbar.
  await expect(panel).toBeHidden();
  await page.locator('.lesson-toolbar').getByRole('button', { name: 'Ask the tutor' }).click();
  await expect(panel).toBeVisible();
  await expect(panel.getByLabel('Ask the tutor')).toBeFocused();
  await panel.getByLabel('Ask the tutor').fill('Why do miners vary the nonce?');
  await panel.getByRole('button', { name: 'Ask' }).click();
  await expect(panel.getByText('Mastering Blockchain, p. 61')).toBeVisible({ timeout: 10_000 });
  await expect(panel.getByText('not supported by the source')).toBeVisible();
  await expect(panel.locator('a')).toHaveCount(0); // links render as text (VEC-TUTOR-6)
  const results = await new AxeBuilder({ page }).include('[aria-label="Tutor"]').analyze();
  const bad = results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');
  expect(bad.map((v) => v.id)).toEqual([]);
});

// Design pass (2026-11-23): open at 1280 px the panel covers no paragraph of the lesson; axe is clean at 1280 and 390.
test('the open panel covers no lesson text at 1280 px and has no serious axe issue at 1280 and 390 px', async ({ page }) => {
  await page.route('**/api/lessons/*/questions', (route) => route.fulfill({ json: { questions: [] } }));
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/lessons/w01');
    await page.locator('.lesson-toolbar').getByRole('button', { name: 'Ask the tutor' }).click();
    const panel = page.getByRole('region', { name: 'Tutor' });
    await expect(panel).toBeVisible();
    if (width === 1280) {
      const overlaps = await page.evaluate(() => {
        const t = (document.getElementById('tutor') as HTMLElement).getBoundingClientRect();
        return [...document.querySelectorAll('[data-markdown] p')]
          .map((p) => p.getBoundingClientRect())
          .filter((r) => r.width > 0 && r.right > t.left && r.left < t.right && r.bottom > t.top && r.top < t.bottom).length;
      });
      expect(overlaps).toBe(0);
    }
    const results = await new AxeBuilder({ page }).include('[aria-label="Tutor"]').analyze();
    const bad = results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');
    expect(bad.map((v) => v.id), `axe at ${width}`).toEqual([]);
  }
});
