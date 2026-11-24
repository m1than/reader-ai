// Spike round 4, filter-URL probe (never shipped): how the MLflow 3.18.0 UI itself encodes a traces filter in the
// URL. Collects every link carrying `filter=` on the Dashboard tabs, then opens the traces tab's Filters popover,
// adds `State = ERROR` through the UI, and records the URL and the rows it renders. Writes /out/filters.json.
const fs = require('fs');
const { chromium } = require('/nm/playwright-core');

const ids = JSON.parse(fs.readFileSync('/out/browser-ids.json', 'utf8'));
const byId = Object.fromEntries(Object.entries(ids).map(([k, v]) => [`tr-${v}`, k]));
const BASE = 'http://localhost:8446';

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 },
    extraHTTPHeaders: { Cookie: '__Host-reader_session=owner' } });
  const page = await ctx.newPage();
  const out = { dashboard_links: [], ui_filter: {} };
  const rows = async () => (await page.locator('input[type="checkbox"][aria-label^="Select trace "]').evaluateAll(
    (els) => els.map((e) => e.getAttribute('aria-label').replace('Select trace ', '')))).map((i) => byId[i] || i);
  for (const tab of ['usage', 'quality', 'tool-calls']) {
    await page.goto(`${BASE}/#/experiments/0/dashboard/${tab}`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(7000);
    const hrefs = await page.locator('a[href*="filter="]').evaluateAll((as) => as.map((a) => a.getAttribute('href')));
    out.dashboard_links.push(...hrefs.map((h) => `${tab}: ${decodeURIComponent(h)}`));
  }
  // The UI's own filter builder.
  await page.goto(`${BASE}/#/experiments/0/traces`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(6000);
  const steps = [];
  out.ui_filter.buttons = (await page.getByRole('button').allInnerTexts()).map((b) => b.trim()).filter(Boolean).slice(0, 40);
  const btn = page.getByRole('button', { name: /filter/i }).first();
  steps.push(`filters button: ${await btn.count()}`);
  if (await btn.count()) {
    await btn.click();
    await page.waitForTimeout(1500);
    const dialog = page.locator('[role="dialog"], [role="menu"], [data-radix-popper-content-wrapper]').last();
    steps.push(`dialog text: ${(await dialog.innerText().catch(() => '')).replace(/\s+/g, ' ').slice(0, 400)}`);
    await page.screenshot({ path: '/out/shots/filters-popover.png' });
    // Try the comboboxes inside the popover: field, operator, value.
    const combos = dialog.getByRole('combobox');
    steps.push(`comboboxes: ${await combos.count()}`);
    const inputs = dialog.locator('input');
    steps.push(`inputs: ${await inputs.count()}`);
    out.ui_filter.popover_html = (await dialog.innerHTML().catch(() => '')).slice(0, 3000);
    // Pick the field `State` and the value ERROR through the popover; read the URL the UI writes.
    try {
      await combos.first().click();
      await page.waitForTimeout(800);
      out.ui_filter.field_options = (await page.getByRole('option').allInnerTexts()).slice(0, 30);
      const state = page.getByRole('option', { name: /^state$/i }).first();
      if (await state.count()) await state.click();
      await page.waitForTimeout(800);
      const valueBox = combos.nth(2);
      if (await valueBox.count()) {
        await valueBox.click();
        await page.waitForTimeout(800);
        out.ui_filter.value_options = (await page.getByRole('option').allInnerTexts()).slice(0, 30);
        const err = page.getByRole('option', { name: /error/i }).first();
        if (await err.count()) await err.click();
      }
      const apply = page.getByRole('button', { name: /apply/i }).first();
      if (await apply.count()) await apply.click();
      await page.waitForTimeout(4000);
      out.ui_filter.url_after_apply = page.url().replace(BASE, '');
      out.ui_filter.rows_after_apply = await rows();
      await page.screenshot({ path: '/out/shots/filters-applied.png' });
    } catch (e) {
      out.ui_filter.error = String(e).slice(0, 300);
    }
  }
  out.ui_filter.steps = steps;
  // Candidate encodings, checked by the rows they render.
  const cands = {
    'state::=::ERROR': 'state::=::ERROR',
    'state::EQUALS::ERROR': 'state::EQUALS::ERROR',
    'state::==::ERROR': 'state::==::ERROR',
    'STATE::=::ERROR': 'STATE::=::ERROR',
    'status::=::ERROR': 'status::=::ERROR',
    'tag::=::dead::reader.job.outcome': 'tag::=::dead::reader.job.outcome',
    'TAG::reader.job.outcome::=::dead': 'TAG::reader.job.outcome::=::dead',
  };
  out.candidates = {};
  void cands;
  // Round 4c: the URL keys the traces view persists (q, sort, dir, startTimeLabel, groupBy), after dismissing the
  // first-visit dialog. Rows are named, in the order shown.
  const got = page.getByRole('button', { name: 'Got it' });
  if (await got.count()) await got.first().click().catch(() => {});
  const urls = {
    'q=exhausted': 'q=exhausted',
    'q=dead': 'q=dead',
    'q=job tutor.answer': `q=${encodeURIComponent('job tutor.answer')}`,
    'q=fact_check': 'q=fact_check',
    'sort=execution_duration&dir=desc': 'sort=execution_duration&dir=desc',
    'q=tutor.answer&sort=execution_duration&dir=desc': 'q=tutor.answer&sort=execution_duration&dir=desc',
    'groupBy=session': 'groupBy=session',
    'startTimeLabel=LAST_24_HOURS': 'startTimeLabel=LAST_24_HOURS',
  };
  for (const [label, q] of Object.entries(urls)) {
    await page.goto(`${BASE}/#/experiments/0/traces?${q}`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(6000);
    const body = await page.evaluate(() => document.body.innerText);
    out.candidates[label] = { rows: await rows(), count_line: (body.match(/\d+ of \d+ traces|\d+ sessions?/) || [''])[0],
      url_after: page.url().replace(BASE, '') };
    await page.screenshot({ path: `/out/shots/url-${label.replace(/[^a-z0-9]+/gi, '_')}.png` });
  }
  if (out.ui_filter.url_after_apply) {
    await page.goto(BASE + out.ui_filter.url_after_apply, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(6000);
    out.ui_filter.reloaded_rows = await rows();
  }
  fs.writeFileSync('/out/filters.json', JSON.stringify(out, null, 1));
  await browser.close();
})();
