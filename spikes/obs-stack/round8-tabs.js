// Spike round 8 (never shipped): a compile Run after 5 of its 120 linked traces were deleted, the `bench`
// experiment's Datasets and Evaluation runs, the agent version, and a trace with a HUMAN label and its override,
// under the candidate CSP. Writes /out/r8/<tab>.png and /out/r8.json.
const fs = require('fs');
const { chromium } = require('/nm/playwright-core');

const BASE = 'http://localhost:8446';
const ids = JSON.parse(fs.readFileSync('/out/round8-ids.json', 'utf8'));
const B = ids.bench;
const TABS = [
  ['run-overview', `/#/experiments/0/runs/${ids.run}`],
  ['run-traces-after-delete', `/#/experiments/0/runs/${ids.run}/traces`],
  ['bench-datasets', `/#/experiments/${B}/datasets`],
  ['bench-dataset-detail', `/#/experiments/${B}/datasets/${ids.dataset}`],
  ['bench-evaluation-runs', `/#/experiments/${B}/evaluation-runs`],
  ['bench-run', `/#/experiments/${B}/runs/${ids.bench_run}`],
  ['agent-versions', '/#/experiments/0/models'],
  ['trace-labels', `/#/experiments/0/traces?selectedEvaluationId=tr-${ids.trace}`],
];

(async () => {
  fs.mkdirSync('/out/r8', { recursive: true });
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
      await page.screenshot({ path: `/out/r8/${tab}.png` });
      const text = await page.evaluate(() => document.body.innerText);
      out.tabs[tab] = { url: page.url().replace(BASE, ''), head: text.replace(/\s+/g, ' ').slice(0, 900) };
    } catch (e) {
      out.tabs[tab] = { error: String(e).slice(0, 200) };
    }
  }
  out.csp = csp;
  fs.writeFileSync('/out/r8.json', JSON.stringify(out, null, 1));
  await browser.close();
})();
