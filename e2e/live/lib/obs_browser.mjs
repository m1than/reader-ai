// SRV-OBS [live], the browser part (Playwright, real Chromium over TLS), run by e2e/live/lib/obs_live.py with the
// owner's id-only session in READER_SESSION. /observability → a trace link → https://localhost:8443 with the span
// tree; no CSP violation on the UI, the traces list and a trace's detail; one write in the UI (an assessment note
// saved, then cleared). Prints `PASS name` / `FAIL name<TAB>detail` and `INFO {…}`: statuses and counts only.
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(join(here, '../../web/live/package.json'));
const { chromium } = require('@playwright/test');

const url = process.env.READER_URL;
const D = 'https://localhost:8443';
const dir = process.env.SHOTS;
let fails = 0;
const line = (ok, name, extra = '') => {
  if (!ok) fails += 1;
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${extra && !ok ? `\t${extra}` : ''}`);
};
const info = (o) => console.log(`INFO ${JSON.stringify(o)}`);
const skip = (name, why) => console.log(`SKIP ${name}\t${why}`);
const browser = await chromium.launch({ args: [`--ignore-certificate-errors-spki-list=${process.env.READER_CA_SPKI}`] });
const csp = [];
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await context.addCookies([
    { name: '__Host-reader_session', value: process.env.READER_SESSION, url, secure: true, httpOnly: true, sameSite: 'Lax' },
  ]);
  await context.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', (e) => {
      (window.__csp ??= []).push(`${e.violatedDirective} ${e.blockedURI}`.slice(0, 160));
    });
  });
  const watch = (p, tag) =>
    p.on('console', (m) => {
      if (/Content Security Policy|Refused to/i.test(m.text())) csp.push(`${tag}: ${m.text().slice(0, 160)}`);
    });
  const page = await context.newPage();
  watch(page, 'app');
  await page.goto(`${url}/observability`);
  await page.getByRole('heading', { level: 1, name: 'Observability' }).waitFor({ timeout: 30_000 });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${dir}/observability.png`, fullPage: true });
  const links = page.getByRole('link', { name: 'Trace', exact: true }); // not the "traces dashboard" link
  const n = await links.count();
  const summaryIds = JSON.parse(readFileSync(`${dir}/summary-trace-ids.json`, 'utf-8'));
  info({ observability_trace_links: n, summary_trace_ids: summaryIds.length });
  if (summaryIds.length === 0) {
    // Only the click depends on the summary's links; it is a counted SKIP, never a silent pass (live-fixes review).
    skip('a trace link opens https://localhost:8443 with the span tree', 'the summary gives no trace id');
  } else if (n === 0) {
    line(false, 'the summary gives trace ids, so /observability shows Trace links', String(summaryIds.length));
  } else {
    // A click from /observability arrives same-site/navigate/document and passes the gate (FR-OBS-9).
    const [tab] = await Promise.all([context.waitForEvent('page'), links.first().click()]);
    watch(tab, 'dashboard (click)');
    await tab.waitForLoadState('domcontentloaded');
    line(tab.url().startsWith(D), 'a trace link opens https://localhost:8443', new URL(tab.url()).origin);
    const spans = tab.getByText(/agent\.call|job |GET |POST /).first();
    const shown = await spans.waitFor({ timeout: 20_000 }).then(() => true, () => false);
    line(shown, 'the clicked trace shows its span tree (not the loading skeleton)');
    await tab.screenshot({ path: `${dir}/dash-clicked-trace.png` });
    await tab.close();
  }
  const dp = await context.newPage();
  watch(dp, 'dashboard');
  const r0 = await dp.goto(`${D}/`);
  line(r0?.status() === 200, 'the dashboard UI loads', String(r0?.status()));
  await dp.waitForTimeout(4000);
  await dp.goto(`${D}/#/experiments/0/traces`);
  await dp.waitForTimeout(6000);
  await dp.screenshot({ path: `${dir}/dash-traces.png` });
  // The detail, its CSP and the UI write use the newest trace from MLflow's own traces/search (request traces exist
  // at zero credit), so they never depend on the summary's links.
  const target = JSON.parse(readFileSync(`${dir}/dashboard-trace.json`, 'utf-8'));
  if (target) {
    const id = target.startsWith('tr-') ? target : `tr-${target}`;
    await dp.goto(`${D}/#/experiments/0/traces?selectedEvaluationId=${id}`);
    await dp.waitForTimeout(6000);
    await dp.screenshot({ path: `${dir}/dash-trace-detail.png` });
    const probes = async () =>
      dp.evaluate(async (tid) => {
        const j = await (await fetch(`/ajax-api/3.0/mlflow/traces/get?trace_id=${tid}`)).json();
        const all = j.trace?.trace_info?.assessments ?? [];
        return all.filter((a) => JSON.stringify(a).includes('obslive-probe')).map((a) => a.assessment_id);
      }, id);
    try {
      await dp.getByRole('button', { name: 'Assess' }).first().click();
      await dp.waitForTimeout(1500);
      const notes = dp.locator('textarea:visible').last();
      await notes.fill('obslive-probe');
      await dp.getByRole('button', { name: 'Save' }).last().click();
      await dp.waitForTimeout(2500);
      line((await probes()).length > 0, 'UI write: an assessment note is saved on the trace');
      await notes.fill('');
      await dp.getByRole('button', { name: 'Save' }).last().click().catch(() => {});
      await dp.waitForTimeout(2500);
    } finally {
      // Whatever the UI left, the probe's assessments are deleted through MLflow's REST (same origin, the gate).
      const left = await probes().catch(() => []);
      for (const aid of left) {
        await dp.evaluate(
          async ([tid, a]) => fetch(`/ajax-api/3.0/mlflow/traces/${tid}/assessments/${a}`, { method: 'DELETE' }),
          [id, aid],
        );
      }
    }
    line((await probes()).length === 0, 'UI write: the probe note is gone afterwards');
  } else {
    line(false, 'MLflow has a trace for the detail and the UI write');
  }
  const pageCsp = [];
  for (const p of context.pages()) pageCsp.push(...(await p.evaluate(() => window.__csp ?? []).catch(() => [])));
  const all = [...csp, ...pageCsp];
  line(all.length === 0, 'no CSP violation (events and console)', all.slice(0, 6).join(' | '));
} finally {
  await browser.close();
}
process.exit(fails ? 1 : 0);
