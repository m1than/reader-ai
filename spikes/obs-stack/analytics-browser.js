// Spike round 4 browser pass (never shipped; V.obs-analytics). Headless Chromium in the caddy container's network
// namespace; http://localhost:8446 is the spike's Caddy with SRV-OBS FR-OBS-9's CSP. On the synthetic traces of
// analytics.py it records: the traces list's column headers and whether token/cost values show; the span types shown
// in a trace's detail; the URL the search box produces (the saved-view format); the experiment's other tabs
// (overview/monitoring, sessions) and their chart titles; and CSP violations. Writes /out/browser.json and shots.
const fs = require('fs');
const { chromium } = require('/nm/playwright-core');

const ids = JSON.parse(fs.readFileSync('/out/browser-ids.json', 'utf8'));
const BASE = 'http://localhost:8446';
const out = { visits: {} };

(async () => {
  fs.mkdirSync('/out/shots', { recursive: true });
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 },
    extraHTTPHeaders: { Cookie: '__Host-reader_session=owner' } });
  const page = await ctx.newPage();
  const console_ = [];
  page.on('console', (m) => { if (/Content Security Policy|Refused to/i.test(m.text())) console_.push(m.text().slice(0, 200)); });
  await page.addInitScript(() => {
    window.__csp = [];
    document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(`${e.effectiveDirective} ${e.blockedURI}`));
  });
  const text = () => page.evaluate(() => document.body.innerText);
  const snap = async (name) => page.screenshot({ path: `/out/shots/${name}.png`, fullPage: false });
  const go = async (name, path, wait = 8000) => {
    await page.goto(BASE + path, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForTimeout(wait);
    await snap(name);
    return text();
  };

  // 1. Traces list: column headers, token and cost cells, trace names, tags.
  let t = await go('traces-list', '/#/experiments/0/traces');
  const headers = await page.locator('[role="columnheader"], th').allInnerTexts();
  out.visits.list = {
    headers: headers.map((h) => h.trim()).filter(Boolean).slice(0, 40),
    shows_trace_names: ['job tutor.answer', 'job pipeline.stage', 'GET /api/obs/summary'].filter((n) => t.includes(n)),
    shows_tokens: /11,?930|42,?050|61,?080/.test(t),
    shows_cost: /\$0\.0[0-9]|\$0\.62|0\.0631/.test(t),
    shows_tags: ['fact_check', 'review', 'exhausted', 'dead'].filter((n) => t.includes(n)),
  };
  // Column picker: which optional columns exist (tokens, cost, session, tags).
  const picker = page.getByRole('button', { name: /columns/i }).first();
  if (await picker.count()) {
    await picker.click().catch(() => {});
    await page.waitForTimeout(1500);
    out.visits.list.column_options = (await text()).split('\n').filter((l) => /token|cost|session|tag|user|latency|execution|state|status|request|response|name|time/i.test(l)).slice(0, 60);
    await snap('traces-columns');
    await page.keyboard.press('Escape');
  }

  // 2. Search box → URL (saved-view format).
  out.visits.list.inputs = await page.locator('input, textarea, [contenteditable="true"]').evaluateAll((els) =>
    els.map((e) => `${e.tagName} ${e.getAttribute('type') || ''} ${e.getAttribute('placeholder') || ''} ${e.getAttribute('aria-label') || ''}`.trim()).slice(0, 20));
  const box = page.locator('input[placeholder*="earch" i], input[aria-label*="earch" i], input[type="search"], input[placeholder*="ilter" i]').first();
  if (await box.count()) {
    await box.fill("tag.`reader.job.outcome` = 'dead'");
    await box.press('Enter');
    await page.waitForTimeout(5000);
    out.visits.search = { url: page.url().replace(BASE, ''), text_has_review: (await text()).includes('review') };
    await snap('search-dead');
  } else {
    out.visits.search = { error: 'no search box' };
  }
  // 2b. The same URL loaded fresh (a saved view is just this link).
  if (out.visits.search.url) {
    t = await go('search-dead-reloaded', out.visits.search.url);
    out.visits.search.reloaded_rows_mention_dead = t.includes('dead') || t.includes('exhausted');
  }

  // 2c. Saved views: URL filters of the traces tab (`filter=<field>::<op>::<value>[::<key>]`, repeated), read from the
  // UI bundle and checked here by the trace rows each URL renders (the row checkboxes carry the trace ids).
  const byId = Object.fromEntries(Object.entries(ids).map(([k, v]) => [`tr-${v}`, k]));
  const rows = async () => (await page.locator('input[type="checkbox"][aria-label^="Select trace "]').evaluateAll(
    (els) => els.map((e) => e.getAttribute('aria-label').replace('Select trace ', '')))).map((i) => byId[i] || i);
  const enc = (s) => encodeURIComponent(s);
  const views = {
    failed_compile_stages: `filter=${enc('state::=::ERROR')}&filter=${enc('TAG::=::pipeline.stage::reader.job.kind')}`,
    dead_jobs: `filter=${enc('TAG::=::dead::reader.job.outcome')}`,
    stage_review: `filter=${enc('TAG::=::review::reader.stage')}`,
    error_code_exhausted: `filter=${enc('TAG::=::exhausted::reader.error_code')}`,
    slow_tutor_answers: `filter=${enc('trace_name::=::job tutor.answer')}&filter=${enc('execution_duration::>::5000')}`,
    tutor_slowest_first: `filter=${enc('trace_name::=::job tutor.answer')}&sort=${enc('execution_duration::number::false')}`,
    one_run_session: `filter=${enc('session::=::10000000-0000-4000-8000-0000000000aa')}`,
    agent_spans: `filter=${enc('span.type::=::AGENT')}`,
    last_7_days_errors: `startTimeLabel=LAST_7_DAYS&filter=${enc('state::=::ERROR')}`,
  };
  out.visits.saved_views = {};
  for (const [name, q] of Object.entries(views)) {
    await go(`view-${name}`, `/#/experiments/0/traces?${q}`, 6000);
    out.visits.saved_views[name] = { query: q, rows: await rows(), url_after: page.url().replace(BASE, '') };
  }
  out.visits.saved_views.none = { rows: (await go('view-none', '/#/experiments/0/traces', 6000), await rows()) };
  // Group by session (the Traces/Sessions switcher).
  await go('sessions-switch', '/#/experiments/0/traces', 4000);
  const sw = page.getByRole('radio', { name: /sessions/i }).first();
  if (await sw.count()) {
    await sw.check({ force: true }).catch(() => {});
    await page.waitForTimeout(5000);
    await snap('sessions-view');
    const body = await text();
    out.visits.sessions_switch = { url: page.url().replace(BASE, ''), run_listed: body.includes('10000000-0000-4000-8000-0000000000aa') };
  }

  // 3. A trace's detail: span types and token figures.
  t = await go('detail-tutor-slow', `/#/experiments/0/traces?selectedEvaluationId=tr-${ids.tutor_slow}`, 9000);
  out.visits.detail = {
    span_names: ['job tutor.answer', 'agent.call', 'claude_code.interaction', 'claude_code.llm_request'].filter((n) => t.includes(n)),
    types_in_text: ['CHAIN', 'AGENT', 'LLM', 'Chain', 'Agent'].filter((n) => t.includes(n)),
    tokens_in_text: /11,?930|11930/.test(t),
    cost_in_text: /0\.01496|0\.015|\$0\.01/.test(t),
  };
  t = await go('detail-baseline', `/#/experiments/0/traces?selectedEvaluationId=tr-${ids.tutor_baseline}`, 9000);
  out.visits.detail_baseline = { types_in_text: ['CHAIN', 'AGENT', 'LLM'].filter((n) => t.includes(n)) };

  // 4. The experiment's other tabs.
  await go('experiment-home', '/#/experiments/0');
  const tabs = await page.locator('[role="tab"], a[href*="/experiments/0/"]').allInnerTexts();
  out.visits.tabs = [...new Set(tabs.map((x) => x.trim()).filter(Boolean))].slice(0, 30);
  for (const [label, re] of [['overview', /^overview$/i], ['dashboard', /^dashboard$/i], ['issues', /^issues$/i],
    ['sessions', /^sessions$/i]]) {
    await go(`experiment-home-${label}`, '/#/experiments/0/traces', 4000);
    const tab = page.locator('[role="tab"], a').filter({ hasText: re }).first();
    if (await tab.count()) {
      await tab.click().catch(() => {});
      await page.waitForTimeout(9000);
      await snap(`tab-${label}`);
      const body = await text();
      out.visits[label] = {
        url: page.url().replace(BASE, ''),
        headings: (await page.locator('h1, h2, h3, h4, [role="heading"]').allInnerTexts()).map((h) => h.trim()).filter(Boolean).slice(0, 30),
        mentions: ['Latency', 'Error', 'Token', 'Cost', 'Traces', 'Requests', 'P95', 'p95', 'Sessions', 'Tool',
          'claude-sonnet-5-5', 'job tutor.answer', 'job pipeline.stage'].filter((n) => body.includes(n)),
        run_session_listed: body.includes('10000000-0000-4000-8000-0000000000aa'),
        text_head: body.replace(/\s+/g, ' ').slice(0, 700),
      };
    } else {
      out.visits[label] = { error: `no tab matching ${re}` };
    }
  }

  out.csp = [...console_, ...(await page.evaluate(() => window.__csp).catch(() => []))];
  fs.writeFileSync('/out/browser.json', JSON.stringify(out, null, 1));
  await browser.close();
})();
