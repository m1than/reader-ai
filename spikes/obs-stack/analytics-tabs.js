// Spike round 4, every tab (never shipped; the owner's "use all tabs"). Visits each tab of MLflow 3.18.0's GenAI
// experiment UI under SRV-OBS's CSP and saves one screenshot per tab as /out/tabs/<VARIANT>-<tab>.png (VARIANT is
// `before`: today's spans; or `after`: the proposed conventions, assessments and prompts), with the tab's visible
// text head and CSP violations in /out/tabs-<VARIANT>.json.
const fs = require('fs');
const { chromium } = require('/nm/playwright-core');

const VARIANT = process.env.VARIANT || 'after';
const ids = JSON.parse(fs.readFileSync('/out/browser-ids.json', 'utf8'));
const BASE = 'http://localhost:8446';
const pick = (...names) => names.map((n) => ids[n]).find(Boolean);
const TABS = [
  ['overview', '/#/experiments/0/overview'],
  ['traces', '/#/experiments/0/traces'],
  ['trace-detail', `/#/experiments/0/traces?selectedEvaluationId=tr-${pick('tutor_slow', 'tutor_baseline')}`],
  ['trace-detail-compile', `/#/experiments/0/traces?selectedEvaluationId=tr-${pick('compile_like', 'stage_fact_check', 'tutor_baseline')}`],
  ['sessions', '/#/experiments/0/traces?groupBy=session'],
  // Per-lesson saved view: the full-text `q` over the lesson id (FR-OBS-17).
  ['traces-lesson', '/#/experiments/0/traces?q=20000000-0000-4000-8000-0000000000bb'],
  ['issues', '/#/experiments/0/issues'],
  ['dashboard-usage', '/#/experiments/0/dashboard/usage'],
  ['dashboard-quality', '/#/experiments/0/dashboard/quality'],
  ['dashboard-tool-calls', '/#/experiments/0/dashboard/tool-calls'],
  ['judges', null],
  ['review', null],
  ['datasets', null],
  ['evaluation-runs', null],
  ['prompts', null],
  ['agent-versions', null],
];
const NAV = { judges: 'Judges', review: 'Review', datasets: 'Datasets', 'evaluation-runs': 'Evaluation runs',
  prompts: 'Prompts', 'agent-versions': 'Agent versions' };

(async () => {
  fs.mkdirSync('/out/tabs', { recursive: true });
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
      await page.screenshot({ path: `/out/tabs/${VARIANT}-${tab}.png` });
      const text = await page.evaluate(() => document.body.innerText);
      out.tabs[tab] = { url: page.url().replace(BASE, ''), head: text.replace(/\s+/g, ' ').slice(0, 600) };
    } catch (e) {
      out.tabs[tab] = { error: String(e).slice(0, 200) };
    }
  }
  // The traces table's column chooser: is there a cost column, and does it sort?
  try {
    await page.goto(`${BASE}/#/experiments/0/traces`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(7000);
    await dismiss();
    out.headers = await page.locator('[role="columnheader"], th').allInnerTexts();
    const chooser = page.getByRole('button', { name: /columns|display|view options/i }).first();
    out.chooser_found = await chooser.count();
    if (out.chooser_found) {
      await chooser.click();
      await page.waitForTimeout(1500);
      const menu = await page.locator('[role="menu"], [role="dialog"], [role="listbox"]').allInnerTexts();
      out.chooser_menu = menu.join(' | ').replace(/\s+/g, ' ').slice(0, 2000);
      const sub = async (label) => {
        await page.getByRole('menuitem', { name: new RegExp(`^${label}`) }).first().hover();
        await page.waitForTimeout(1200);
        const all = await page.locator('[role="menu"]').allInnerTexts();
        await page.screenshot({ path: `/out/tabs/${VARIANT}-traces-${label.toLowerCase()}-menu.png` });
        return all.map((t) => t.replace(/\s+/g, ' ')).join(' | ').slice(0, 2000);
      };
      out.sort_menu = await sub('Sort');
      out.columns_menu = await sub('Columns');
      const cost = page.getByRole('menuitemcheckbox', { name: /cost/i }).or(page.getByRole('checkbox', { name: /cost/i }));
      out.cost_option = await cost.count();
      if (out.cost_option) {
        await cost.first().click().catch(() => {});
        await page.keyboard.press('Escape');
        await page.waitForTimeout(1500);
        out.headers_with_cost = await page.locator('[role="columnheader"], th').allInnerTexts();
        const head = page.locator('[role="columnheader"], th').filter({ hasText: /cost/i }).first();
        if (await head.count()) {
          await head.click().catch(() => {});
          await page.waitForTimeout(2500);
          out.cost_sort_url = page.url().replace(BASE, '');
          out.cost_sorted_rows = (await page.locator('[role="row"]').allInnerTexts()).slice(0, 8)
            .map((r) => r.replace(/\s+/g, ' ').slice(0, 160));
        }
        await page.screenshot({ path: `/out/tabs/${VARIANT}-traces-cost-column.png` });
      }
    }
  } catch (e) {
    out.chooser_error = String(e).slice(0, 200);
  }
  out.csp = csp;
  fs.writeFileSync(`/out/tabs-${VARIANT}.json`, JSON.stringify(out, null, 1));
  await browser.close();
})();
