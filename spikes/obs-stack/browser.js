// Spike browser pass (never shipped; SRV-OBS review B4). Headless Chromium inside the caddy container's network
// namespace, so http://localhost:<port> is the spike's Caddy (no published port). For each site — 8443 the strict
// base CSP, 8445 no CSP, 8446 the candidate CSP — it opens the UI, the traces list and deep links to a known trace, and
// records CSP violations (securitypolicyviolation events and console lines), page errors, requests to any other host,
// and which span names render. Writes /out/browser.json and screenshots in /out/shots/.
const fs = require('fs');
const { chromium } = require('/nm/playwright-core');

const ids = JSON.parse(fs.readFileSync('/out/browser-ids.json', 'utf8'));
const SITES = { strict: 8443, none: 8445, candidate: 8446 };
const NAMES = ['pipeline.stage', 'job tutor.answer', 'agent.call', 'claude_code.interaction', 'claude_code.llm_request'];

(async () => {
  fs.mkdirSync('/out/shots', { recursive: true });
  const browser = await chromium.launch();
  const out = { ids, sites: {} };
  for (const [label, port] of Object.entries(SITES)) {
    const base = `http://localhost:${port}`;
    const ctx = await browser.newContext({ extraHTTPHeaders: { Cookie: '__Host-reader_session=owner' } });
    const page = await ctx.newPage();
    const rec = { console: [], pageerrors: [], foreign: [], failed: [], visits: [] };
    page.on('console', (m) => { if (['error', 'warning'].includes(m.type())) rec.console.push(m.text().slice(0, 300)); });
    page.on('pageerror', (e) => rec.pageerrors.push(String(e).slice(0, 300)));
    page.on('request', (r) => { const u = new URL(r.url()); if (u.hostname !== 'localhost') rec.foreign.push(u.origin); });
    page.on('response', (r) => { if (r.status() >= 400) rec.failed.push(`${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`); });
    page.on('requestfailed', (r) => rec.failed.push(`${r.method()} ${new URL(r.url()).pathname} ${r.failure()?.errorText}`));
    await page.addInitScript(() => {
      window.__csp = [];
      document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(
        { directive: e.effectiveDirective, blocked: e.blockedURI, sample: (e.sample || '').slice(0, 80) }));
    });
    const visits = [
      ['root', '/'],
      ['traces', '/#/experiments/0/traces'],
      ['deep_selectedEvaluationId', `/#/experiments/0/traces?selectedEvaluationId=tr-${ids.job_trace}`],
      ['deep_selectedTraceId', `/#/experiments/0/traces?selectedTraceId=tr-${ids.job_trace}`],
      ['deep_stage_selectedEvaluationId', `/#/experiments/0/traces?selectedEvaluationId=tr-${ids.stage_trace}`],
    ];
    for (const [name, path] of visits) {
      const v = { name, path };
      try {
        // Round 2: `networkidle` never came, so load the DOM and give the SPA 8 s.
        const resp = await page.goto(base + path, { waitUntil: 'domcontentloaded', timeout: 30000 });
        v.status = resp ? resp.status() : null;
        await page.waitForTimeout(8000);
        const text = await page.evaluate(() => document.body.innerText);
        v.text_len = text.length;
        v.names_visible = NAMES.filter((n) => text.includes(n));
        v.mentions_link = /\blinks?\b/i.test(text);
        v.csp = await page.evaluate(() => window.__csp);
        await page.screenshot({ path: `/out/shots/${label}-${name}.png` });
      } catch (e) {
        v.error = String(e).slice(0, 200);
      }
      rec.visits.push(v);
    }
    out.sites[label] = rec;
    await ctx.close();
  }
  await browser.close();
  fs.writeFileSync('/out/browser.json', JSON.stringify(out, null, 2));
  for (const [l, r] of Object.entries(out.sites)) {
    console.log(l, JSON.stringify(r.visits.map((v) => [v.name, v.text_len, v.names_visible, (v.csp || []).length, v.error])),
      'foreign', [...new Set(r.foreign)], 'pageerrors', r.pageerrors.length);
  }
})();
