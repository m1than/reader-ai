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
  await expect(panel).toBeVisible();
  await panel.getByLabel('Ask the tutor').fill('Why do miners vary the nonce?');
  await panel.getByRole('button', { name: 'Ask' }).click();
  await expect(panel.getByText('Mastering Blockchain, p. 61')).toBeVisible({ timeout: 10_000 });
  await expect(panel.getByText('not supported by the source')).toBeVisible();
  await expect(panel.locator('a')).toHaveCount(0); // links render as text (VEC-TUTOR-6)
  const results = await new AxeBuilder({ page }).include('[aria-label="Tutor"]').analyze();
  const bad = results.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical');
  expect(bad.map((v) => v.id)).toEqual([]);
});
