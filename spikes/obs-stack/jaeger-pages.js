// Spike round 5 (never shipped; review B8): Jaeger UI 2.23.0 through the spike Caddy :8448 (per-site gate, CSP_JAEGER).
// Visits every top-level page, records each CSP violation with its source file and line, and recomputes the sha256 of
// every inline <script> of the served index (what an [image] test would do). Writes /out/jaeger-pages.json.
const fs = require('fs');
const crypto = require('crypto');
const { chromium } = require('/nm/playwright-core');

const ids = JSON.parse(fs.readFileSync('/out/browser-ids.json', 'utf8'));
const BASE = 'http://localhost:8448';
const PAGES = ['/search', `/trace/${ids.compile_like}`, `/trace/${ids.compile_like}?uiEmbed=v0`,
  `/trace/${ids.compile_like}/${ids.compile_like}`, '/dependencies', '/deep-dependencies', '/monitor', '/quality-metrics',
  '/compare', '/search?service=reader-worker'];

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1920, height: 1080 },
    extraHTTPHeaders: { Cookie: '__Host-reader_session=owner' } });
  const page = await ctx.newPage();
  const out = { pages: {}, foreign: [] };
  page.on('request', (r) => { const u = new URL(r.url()); if (u.hostname !== 'localhost') out.foreign.push(u.origin); });
  await page.addInitScript(() => {
    window.__csp = [];
    document.addEventListener('securitypolicyviolation', (e) => window.__csp.push({ directive: e.effectiveDirective,
      blocked: e.blockedURI, source: e.sourceFile, line: e.lineNumber, column: e.columnNumber,
      sample: (e.sample || '').slice(0, 80) }));
  });
  for (const p of PAGES) {
    try {
      const resp = await page.goto(BASE + p, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await page.waitForTimeout(5000);
      out.pages[p] = { status: resp ? resp.status() : null, csp: await page.evaluate(() => window.__csp),
        title: (await page.evaluate(() => document.body.innerText)).replace(/\s+/g, ' ').slice(0, 160) };
    } catch (e) {
      out.pages[p] = { error: String(e).slice(0, 200) };
    }
  }
  // The inline scripts of the index, hashed as CSP does (sha256 over the element's text, base64).
  const html = await (await ctx.request.get(`${BASE}/search`)).text();
  out.inline_script_hashes = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)]
    .map((m) => `sha256-${crypto.createHash('sha256').update(m[1], 'utf8').digest('base64')}`);
  out.csp_header = (await ctx.request.get(`${BASE}/search`)).headers()['content-security-policy'];
  fs.writeFileSync('/out/jaeger-pages.json', JSON.stringify(out, null, 1));
  await browser.close();
})();
