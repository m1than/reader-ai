// Spike round 4, Jaeger option browser probe (never shipped). Jaeger's UI through the spike Caddy :8448 (owner
// gate + SRV-OBS's CSP): the compile-like trace's waterfall, CSP violations, requests to other hosts.
// Writes /out/jaeger-browser.json and /out/jaeger-timeline.png.
const fs = require('fs');
const { chromium } = require('/nm/playwright-core');

const ids = JSON.parse(fs.readFileSync('/out/browser-ids.json', 'utf8'));
const BASE = 'http://localhost:8448';

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1920, height: 1080 },
    extraHTTPHeaders: { Cookie: '__Host-reader_session=owner' } });
  const page = await ctx.newPage();
  const out = { console: [], foreign: [], failed: [] };
  page.on('console', (m) => { if (['error', 'warning'].includes(m.type())) out.console.push(m.text().slice(0, 240)); });
  page.on('request', (r) => { const u = new URL(r.url()); if (u.hostname !== 'localhost') out.foreign.push(u.origin); });
  page.on('response', (r) => { if (r.status() >= 400) out.failed.push(`${r.status()} ${new URL(r.url()).pathname}`); });
  await page.addInitScript(() => {
    window.__csp = [];
    document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(`${e.effectiveDirective} ${e.blockedURI} ${(e.sample || '').slice(0, 60)}`));
  });
  const resp = await page.goto(`${BASE}/trace/${ids.compile_like}`, { waitUntil: 'domcontentloaded' });
  out.status = resp ? resp.status() : null;
  out.csp_header = resp ? resp.headers()['content-security-policy'] : null;
  await page.waitForTimeout(8000);
  await page.screenshot({ path: '/out/jaeger-timeline.png' });
  const body = await page.evaluate(() => document.body.innerText);
  out.text_mentions = ['job pipeline.stage', 'stage fact_check', 'claude_code.llm_request', 'Trace Start', 'Duration',
    'Services', 'Depth', 'Total Spans'].filter((w) => body.includes(w));
  out.csp = await page.evaluate(() => window.__csp);
  await page.goto(`${BASE}/search`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(5000);
  await page.screenshot({ path: '/out/jaeger-search.png' });
  out.csp_search = await page.evaluate(() => window.__csp);
  fs.writeFileSync('/out/jaeger-browser.json', JSON.stringify(out, null, 1));
  await browser.close();
})();
