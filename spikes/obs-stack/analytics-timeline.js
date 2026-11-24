// Spike round 4, timeline probe (never shipped; the owner's "is there a Jaeger-like view of spans?"). Opens the
// compile-like trace (45 spans, two stages in parallel) in MLflow 3.18.0's trace detail, records every control the
// detail offers, switches on anything that reads as a timeline/Gantt/waterfall, and saves trace-tree.png and
// trace-timeline.png (plus the bar geometry it finds) to /out. Writes /out/timeline.json.
const fs = require('fs');
const { chromium } = require('/nm/playwright-core');

const ids = JSON.parse(fs.readFileSync('/out/browser-ids.json', 'utf8'));
const BASE = 'http://localhost:8446';

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1920, height: 1080 },
    extraHTTPHeaders: { Cookie: '__Host-reader_session=owner' } });
  const page = await ctx.newPage();
  const out = {};
  await page.goto(`${BASE}/#/experiments/0/traces?selectedEvaluationId=tr-${ids.compile_like}`,
    { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(9000);
  const got = page.getByRole('button', { name: 'Got it' });
  if (await got.count()) await got.first().click().catch(() => {});
  // Close the MLflow Assistant side panel (it takes a third of the width).
  for (const name of ['Close guidance', 'Close']) {
    const b = page.getByRole('button', { name, exact: true });
    if (await b.count()) await b.last().click().catch(() => {});
  }
  await page.waitForTimeout(1000);
  await page.screenshot({ path: '/out/trace-tree.png' });
  const controls = async () => page.locator('button, [role="tab"], [role="radio"], [role="switch"], [role="menuitem"]')
    .evaluateAll((els) => els.map((e) => `${e.getAttribute('role') || e.tagName.toLowerCase()}: ${(e.innerText || e.getAttribute('aria-label') || e.getAttribute('title') || '').trim().replace(/\s+/g, ' ')}`)
      .filter((s) => !s.endsWith(': ')).slice(0, 80));
  out.controls = await controls();
  out.text_mentions = ['Timeline', 'Gantt', 'Waterfall', 'Duration', 'Span tree', 'Show timeline', 'Hide timeline']
    .filter(async () => true);
  const body = await page.evaluate(() => document.body.innerText);
  out.mentions = ['Timeline', 'timeline', 'Gantt', 'Waterfall', 'Duration', 'Show timeline', 'Hide timeline']
    .filter((w) => body.includes(w));
  // Anything that reads as a timeline control: click it.
  const cand = page.locator('button, [role="tab"], [role="radio"], [role="switch"], [role="menuitem"]')
    .filter({ hasText: /timeline|gantt|waterfall/i });
  out.timeline_controls = await cand.allInnerTexts();
  const aria = page.locator('[aria-label*="imeline" i], [title*="imeline" i]');
  out.timeline_aria = await aria.evaluateAll((els) => els.map((e) => `${e.tagName} ${e.getAttribute('aria-label') || e.getAttribute('title')}`));
  const target = (await cand.count()) ? cand.first() : ((await aria.count()) ? aria.first() : null);
  if (target) {
    await target.click().catch((e) => { out.click_error = String(e).slice(0, 200); });
    await page.waitForTimeout(2500);
  }
  await page.screenshot({ path: '/out/trace-timeline.png' });
  // Bars: elements whose width is a fraction of a shared track, with a left offset (a timeline draws spans so).
  out.bars = await page.evaluate(() => {
    const found = [];
    for (const el of document.querySelectorAll('div, span, rect')) {
      const style = el.getAttribute('style') || '';
      if (/left:\s*[\d.]+%/.test(style) && /width:\s*[\d.]+%/.test(style)) {
        found.push(style.replace(/\s+/g, ' ').slice(0, 80));
      } else if (el.tagName.toLowerCase() === 'rect' && el.getAttribute('width')) {
        found.push(`rect x=${el.getAttribute('x')} w=${el.getAttribute('width')}`);
      }
    }
    return { count: found.length, sample: found.slice(0, 12) };
  });
  out.controls_after = await controls();
  out.durations_in_text = (await page.evaluate(() => document.body.innerText)).match(/\b\d+(\.\d+)?\s?(ms|s)\b/g)?.slice(0, 20) ?? [];
  fs.writeFileSync('/out/timeline.json', JSON.stringify(out, null, 1));
  await browser.close();
})();
