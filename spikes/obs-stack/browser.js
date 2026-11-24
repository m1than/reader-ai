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
  // Round 3 (review N1): a click from another port of the same site (the app's /observability stand-in on :8447)
  // to the dashboard's deep link, then subresource and fetch requests from that page to the dashboard. The gate log
  // (gate.log) shows each request's Sec-Fetch-* triple and the gate's answer; `probe=` names each one.
  {
    const ctx = await browser.newContext({ extraHTTPHeaders: { Cookie: '__Host-reader_session=owner' } });
    const page = await ctx.newPage();
    const deep = `/#/experiments/0/traces?selectedEvaluationId=tr-${ids.job_trace}`;
    const click = {};
    try {
      await page.goto(`http://localhost:8447/#${encodeURIComponent(deep)}`, { waitUntil: 'domcontentloaded' });
      await Promise.all([page.waitForURL(/localhost:8446/, { timeout: 30000 }), page.click('#go')]);
      await page.waitForTimeout(8000);
      const text = await page.evaluate(() => document.body.innerText);
      click.url = page.url();
      click.names_visible = NAMES.filter((n) => text.includes(n));
      await page.screenshot({ path: '/out/shots/click-from-app-port.png' });
      await page.goto('http://localhost:8447/', { waitUntil: 'domcontentloaded' });
      click.subresources = await page.evaluate(async () => {
        const u = (p) => `http://localhost:8446/ajax-api/2.0/mlflow/experiments/search?max_results=1&probe=${p}`;
        const load = (tag, p) => new Promise((res) => {
          const el = document.createElement(tag);
          el.onload = () => res('load'); el.onerror = () => res('error');
          el.src = u(p); document.body.appendChild(el);
        });
        const r = { img: await load('img', 'img'), script: await load('script', 'script') };
        try { const f = await fetch(u('fetch-no-cors'), { mode: 'no-cors', credentials: 'include' }); r.fetch_no_cors = f.type; }
        catch (e) { r.fetch_no_cors = 'rejected'; }
        try { const f = await fetch(u('fetch-cors'), { credentials: 'include' }); r.fetch_cors = f.status; }
        catch (e) { r.fetch_cors = 'rejected'; }
        return r;
      });
    } catch (e) {
      click.error = String(e).slice(0, 200);
    }
    out.click = click;
    console.log('click', JSON.stringify(click));
    await ctx.close();
  }
  await browser.close();
  fs.writeFileSync('/out/browser.json', JSON.stringify(out, null, 2));
  for (const [l, r] of Object.entries(out.sites)) {
    console.log(l, JSON.stringify(r.visits.map((v) => [v.name, v.text_len, v.names_visible, (v.csp || []).length, v.error])),
      'foreign', [...new Set(r.foreign)], 'pageerrors', r.pageerrors.length);
  }
})();
