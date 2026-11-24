// Spike round 6, phase 2 (never shipped). Screenshots MLflow 3.18.0's experiment tabs under SRV-OBS's CSP:
// VARIANT=before is the implemented V.obs-analytics data (analytics_tabs.py), VARIANT=after adds phase 2
// (phase2.py: Runs, datasets, evaluation runs, agent versions, review queue). Writes /out/p2/<VARIANT>-<tab>.png and
// /out/p2-<VARIANT>.json (URL, visible text head, CSP violations).
const fs = require('fs');
const { chromium } = require('/nm/playwright-core');

const VARIANT = process.env.VARIANT || 'after';
const BASE = 'http://localhost:8446';
let ids = {};
try { ids = JSON.parse(fs.readFileSync('/out/phase2-ids.json', 'utf8')); } catch (e) { /* before: none */ }
const NAV = {};
const TABS = [
  ['overview', '/#/experiments/0/overview'],
  ['evaluation-runs', '/#/experiments/0/evaluation-runs'],
  ['runs', '/#/experiments/0/runs'],
  ['datasets', '/#/experiments/0/datasets'],
  ['agent-versions', '/#/experiments/0/models'],
  ['review', '/#/experiments/0/review-queue'],
  ['prompts', '/#/experiments/0/prompts'],
  ['traces', '/#/experiments/0/traces'],
  ['experiments', '/#/experiments'],
];
if (ids.compile_run) {
  TABS.push(['run-compile', `/#/experiments/0/runs/${ids.compile_run}`]);
  TABS.push(['run-compile-traces', `/#/experiments/0/runs/${ids.compile_run}/traces`]);
  TABS.push(['run-compile-metrics', `/#/experiments/0/runs/${ids.compile_run}/model-metrics`]);
  TABS.push(['run-ingest', `/#/experiments/0/runs/${ids.ingest_run}`]);
  TABS.push(['run-cross-experiment', `/#/experiments/${ids.compile_experiment}/runs/${ids.compile_run_cross}`]);
}
if (ids.models) {
  const m = Object.values(ids.models);
  TABS.push(['agent-version-detail', `/#/experiments/0/models/${m[0]}`]);
  TABS.push(['agent-version-traces', `/#/experiments/0/models/${m[0]}/traces`]);
}
if (ids.dataset) TABS.push(['dataset-detail', `/#/experiments/0/datasets?selectedDatasetId=${ids.dataset}`]);

(async () => {
  fs.mkdirSync('/out/p2', { recursive: true });
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1920, height: 1080 },
    extraHTTPHeaders: { Cookie: '__Host-reader_session=owner' } });
  const page = await ctx.newPage();
  const csp = [];
  page.on('console', (m) => { if (/Content Security Policy|Refused to/i.test(m.text())) csp.push(m.text().slice(0, 200)); });
  const out = { variant: VARIANT, tabs: {} };
  const dismiss = async () => {
    for (const name of ['Got it', 'Close guidance']) {
      const b = page.getByRole('button', { name, exact: true });
      if (await b.count()) await b.first().click().catch(() => {});
    }
  };
  for (const [tab, path] of TABS) {
    try {
      if (path) {
        await page.goto(BASE + path, { waitUntil: 'domcontentloaded', timeout: 30000 });
      } else {
        await page.goto(`${BASE}/#/experiments/0/traces`, { waitUntil: 'domcontentloaded', timeout: 30000 });
        await page.waitForTimeout(4000);
        await page.getByRole('link', { name: NAV[tab], exact: true }).first().click();
      }
      await page.waitForTimeout(8000);
      await dismiss();
      await page.waitForTimeout(800);
      await page.screenshot({ path: `/out/p2/${VARIANT}-${tab}.png` });
      const text = await page.evaluate(() => document.body.innerText);
      out.tabs[tab] = { url: page.url().replace(BASE, ''), head: text.replace(/\s+/g, ' ').slice(0, 800) };
    } catch (e) {
      out.tabs[tab] = { error: String(e).slice(0, 200) };
    }
  }
  out.csp = csp;
  fs.writeFileSync(`/out/p2-${VARIANT}.json`, JSON.stringify(out, null, 1));
  await browser.close();
})();
