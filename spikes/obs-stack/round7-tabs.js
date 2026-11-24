// Spike round 7 (never shipped): screenshots of a compile Run kept in its own experiment `runs` whose linked traces
// live in experiment 0, under the candidate CSP. Writes /out/r7/<tab>.png and /out/r7.json.
const fs = require('fs');
const { chromium } = require('/nm/playwright-core');

const BASE = 'http://localhost:8446';
const ids = JSON.parse(fs.readFileSync('/out/round7-ids.json', 'utf8'));
const E = ids.runs_experiment;
const TABS = [
  ['run-overview', `/#/experiments/${E}/runs/${ids.run}`],
  ['run-traces', `/#/experiments/${E}/runs/${ids.run}/traces`],
  ['run-metrics', `/#/experiments/${E}/runs/${ids.run}/model-metrics`],
  ['runs-exp-runs', `/#/experiments/${E}/runs`],
  ['runs-exp-evaluation-runs', `/#/experiments/${E}/evaluation-runs`],
  ['runs-exp-traces', `/#/experiments/${E}/traces`],
  ['exp0-evaluation-runs', '/#/experiments/0/evaluation-runs'],
  ['exp0-traces', '/#/experiments/0/traces'],
  ['trace-detail', `/#/experiments/0/traces?selectedEvaluationId=tr-${ids.stage_traces.revise}`],
];

(async () => {
  fs.mkdirSync('/out/r7', { recursive: true });
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1920, height: 1080 },
    extraHTTPHeaders: { Cookie: '__Host-reader_session=owner' } });
  const page = await ctx.newPage();
  const csp = [];
  page.on('console', (m) => { if (/Content Security Policy|Refused to/i.test(m.text())) csp.push(m.text().slice(0, 200)); });
  const out = { tabs: {} };
  for (const [tab, path] of TABS) {
    try {
      await page.goto(BASE + path, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await page.waitForTimeout(8000);
      for (const name of ['Got it', 'Close guidance']) {
        const b = page.getByRole('button', { name, exact: true });
        if (await b.count()) await b.first().click().catch(() => {});
      }
      await page.waitForTimeout(800);
      await page.screenshot({ path: `/out/r7/${tab}.png` });
      const text = await page.evaluate(() => document.body.innerText);
      out.tabs[tab] = { url: page.url().replace(BASE, ''), head: text.replace(/\s+/g, ' ').slice(0, 900) };
    } catch (e) {
      out.tabs[tab] = { error: String(e).slice(0, 200) };
    }
  }
  out.csp = csp;
  fs.writeFileSync('/out/r7.json', JSON.stringify(out, null, 1));
  await browser.close();
})();
