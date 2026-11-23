// The vertical slice as the owner (V.live; e2e/live/slice.sh → verify-all.sh --slice → web_live.py --owner).
// Steps are chosen by READER_SLICE_STEPS (comma-separated); each test skips unless its step is listed:
//   routes   sign in as the owner (bootstrap + CDP virtual passkey), then the owner routes answer: materials, search,
//            course import (409 no_material before an import), compile/ask/run on unknown ids (404). Spends nothing.
//            Once the material is imported, the course POST creates the course and its lessons: that is the
//            owner's G1 approval (SRV-PIPELINE FR-PIPELINE-1); it is idempotent, so reruns change nothing.
//   import   the pilot material is imported (polls GET /api/materials); page, span and outline counts.
//   embed    search.embed has finished: polls POST /api/search until it returns hits; minutes from this test's start.
//   bench    questions-v2 (40) through POST /api/search, k 10: recall@1/@10, MRR, latency. Spends nothing.
//   compile  imports the course (G1), compiles L02 (POST /api/lessons/{id}/compile), polls GET /api/compiles/{run}.
//   lesson   opens the compiled L02: screenshot, one lab cell run in the kernel, citations rendered.
//   tutor    three questions through the Ask panel; latency and grounded per question.
//   ui       WEB-UI FR-UI-10/-11 and WEB-NOTEBOOK FR-NOTEBOOK-11 on the real L02 at 1280, 1024 and 390 px: axe
//            (serious and critical) on the list and the lesson, no horizontal scroll, prose type and column, the
//            skip link and the contents (toggle at 390, column at 1280 and 1024, an entry focuses its cell). Review
//            screenshots (sign-in, list, L02 parts, not-found) go to READER_LIVE_OUT/ui. Spends nothing (no Run, no Ask).
//   auth     SRV-AUTH FR-AUTH-19..21 on the running stack: the sign-in and bootstrap pages at 1280, 1024 and 390 px
//            (axe, overflow, the button's focus outline; screenshots to READER_LIVE_OUT/auth), then slice.live.ts's
//            sign-out and sign-in sequence for the owner with the same CDP virtual passkey (/api/me 401, then 200).
//            Spends nothing.
//   tutor-bench  SRV-TUTOR's bench (VEC-TUTOR-7): questions-v2 with v03 → t01 (40) over the HTTP API; ≈ $1.8.
// Written: READER_LIVE_OUT/slice-results.json with numbers, states and ids only — never book text, a question, a
// snippet or an answer. Screenshots go to READER_LIVE_OUT/screenshots and READER_LIVE_OUT/ui, and the `ui` step also
// writes the L02 revision, the lesson list and the tutor history (they hold book text and questions) to
// READER_LIVE_OUT/ui/data for a local replay; slice.sh refuses an OUT inside the repository, so none of it is
// committed.
import AxeBuilder from '@axe-core/playwright';
import { expect, test, type APIRequestContext, type BrowserContext, type CDPSession, type Page } from '@playwright/test';
import { mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { benchSet, hitsGold, mergeRuns, summary, type BenchRecord } from '../../../web/src/tutor/bench.ts';

const OUT = resolve(process.env.READER_LIVE_OUT ?? 'out');
const SHOTS = resolve(OUT, 'screenshots');
mkdirSync(SHOTS, { recursive: true });
const STEPS = new Set((process.env.READER_SLICE_STEPS ?? 'routes').split(',').map((s) => s.trim()));
const LESSON_PLAN_ID = 'L02';
const MATERIAL_ID = 'bashir-2025';
const NIL = '00000000-0000-4000-8000-000000000000';
const TERMINAL = new Set(['converged', 'open_findings', 'budget_exhausted', 'stage_failed']);
// The agent's own questions about L02's topic (chapter 2); the first comes from SRV-TUTOR's bench-fixed.yaml (t01).
const TUTOR_EXTRA = [
  'Что в этой главе понимается под децентрализацией и зачем она нужна?',
  'Какие уровни или способы децентрализации системы описаны в этой главе?',
];

type Plan = { material: { sha256: string } };
type Question = { id: string; chapter: number; gold_pages: number[]; question: string };
type Hit = { page: number };

const plan = (): Plan => JSON.parse(readFileSync(process.env.READER_SLICE_PLAN ?? '', 'utf-8')) as Plan;
const questions = (): Question[] => JSON.parse(readFileSync(process.env.READER_SLICE_QUESTIONS ?? '', 'utf-8')) as Question[];
// SRV-TUTOR's bench-fixed.yaml (t01), written by web_live.py's slice_inputs
const fixedQuestions = (): Question[] => JSON.parse(readFileSync(process.env.READER_SLICE_TUTOR_FIXED ?? '', 'utf-8')) as Question[];
const only = (step: string) => test.skip(!STEPS.has(step), `step ${step} not requested`);
const now = () => performance.now();
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

const results: Record<string, unknown> = { steps: [...STEPS] };
let context: BrowserContext;
let page: Page;
let api: APIRequestContext;
let cdp: CDPSession;
let lessonId = '';

const shot = async (name: string) => page.screenshot({ path: resolve(SHOTS, `${name}.png`), fullPage: true });
const post = (path: string, data: unknown) => api.post(path, { data, headers: { Origin: new URL(test.info().project.use.baseURL!).origin } });
const search = async (q: string, k = 10) => {
  const res = await post('/api/search', { q, k, material: plan().material.sha256 });
  expect(res.status()).toBe(200);
  return (await res.json()) as { results: Hit[]; degraded: boolean };
};

const ensureLesson = async () => {
  if (lessonId) return;
  const res = await post('/api/pipeline/courses', plan());
  expect(res.status(), 'course import (G1)').toBe(200);
  const body = (await res.json()) as { course_id: string; lessons: { plan_lesson_id: string; lesson_id: string }[] };
  lessonId = body.lessons.find((l) => l.plan_lesson_id === LESSON_PLAN_ID)!.lesson_id;
  results.course = { course_id: body.course_id, lessons: body.lessons.length, l02_lesson_id: lessonId };
};

test.describe.configure({ mode: 'serial' });

test.beforeAll(async ({ browser }) => {
  context = await browser.newContext({ ignoreHTTPSErrors: false });
  page = await context.newPage();
  api = page.request;
  cdp = await context.newCDPSession(page);
  await cdp.send('WebAuthn.enable', { enableUI: false });
  await cdp.send('WebAuthn.addVirtualAuthenticator', {
    options: {
      protocol: 'ctap2', ctap2Version: 'ctap2_1', transport: 'internal', hasResidentKey: true,
      hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true,
      defaultBackupEligibility: true, defaultBackupState: true,
    },
  });
});

// After every test, so a later timeout or kill keeps the numbers already measured (e.g. after the compile spend).
const save = () => writeFileSync(resolve(OUT, 'slice-results.json'), `${JSON.stringify(results, null, 2)}\n`);
test.afterEach(save);

test.afterAll(async () => {
  save();
  await context.close();
});

test('owner passkey registration through the bootstrap page', async () => {
  const token = process.env.READER_BOOTSTRAP_TOKEN ?? '';
  expect(token.length, 'web_live.py hands a bootstrap token').toBeGreaterThan(20);
  await page.goto('/api/auth/signin');
  await page.evaluate((t) => history.replaceState(null, '', `/api/auth/bootstrap#${t}`), token);
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Register your passkey' })).toBeVisible();
  expect(page.url(), 'passkey.js clears the fragment').not.toContain('#');
  await page.getByRole('button', { name: 'Create your passkey' }).click(); // SRV-AUTH FR-AUTH-19 (was "Register passkey")
  await page.waitForURL((u) => u.pathname === '/', { timeout: 30_000 });
  const me = await api.get('/api/me');
  expect(me.status()).toBe(200);
  expect(((await me.json()) as { role: string }).role).toBe('owner');
});

test('routes: the owner API answers before any spend', async () => {
  only('routes');
  const materials = await api.get('/api/materials');
  expect(materials.status()).toBe(200);
  const listed = ((await materials.json()) as { materials: { id: string; status: string }[] }).materials;
  const material = listed.find((m) => m.id === MATERIAL_ID);
  expect(material, 'bashir-2025 declared in reader.yaml').toBeTruthy();
  const s = await search('децентрализация');
  const course = await post('/api/pipeline/courses', plan());
  const imported = material!.status === 'imported';
  expect(course.status(), 'course import: 200 after an import, 409 no_material before').toBe(imported ? 200 : 409);
  expect((await post(`/api/lessons/${NIL}/compile`, {})).status()).toBe(404);
  expect((await api.get(`/api/compiles/${NIL}`)).status()).toBe(404);
  expect((await post(`/api/lessons/${NIL}/ask`, { question: 'test?' })).status()).toBe(404);
  expect((await post('/api/search', { q: '' })).status()).toBe(400);
  results.routes = { material_status: material!.status, search_hits: s.results.length, search_degraded: s.degraded, course_import: course.status() };
});

test('import: the pilot material is imported', async () => {
  only('import');
  test.setTimeout(10 * 60_000);
  let row: Record<string, unknown> | undefined;
  for (const deadline = now() + 9 * 60_000; now() < deadline; await sleep(5_000)) {
    const listed = ((await (await api.get('/api/materials')).json()) as { materials: Record<string, unknown>[] }).materials;
    row = listed.find((m) => m.id === MATERIAL_ID);
    if (row && (row.status === 'imported' || row.status === 'dead')) break; // store.api_status's terminal states
  }
  results.import = {
    status: row?.status, converter_version: row?.converter_version, page_count: row?.page_count,
    span_count: row?.span_count, outline_count: row?.outline_count, pages_without_text: row?.pages_without_text,
  };
  expect(row?.status).toBe('imported');
});

test('embed: search.embed finished (search returns hits)', async () => {
  only('embed');
  test.setTimeout(80 * 60_000);
  const t0 = now();
  let hits = 0;
  for (const deadline = t0 + 75 * 60_000; now() < deadline; await sleep(30_000)) {
    hits = (await search('децентрализация')).results.length;
    if (hits > 0) break;
  }
  results.embed = { minutes_waited: +((now() - t0) / 60_000).toFixed(2), first_hits: hits };
  expect(hits).toBeGreaterThan(0);
});

test('bench: questions-v2 recall through POST /api/search', async () => {
  only('bench');
  test.setTimeout(20 * 60_000);
  const qs = questions();
  let at1 = 0, at10 = 0, rr = 0, degraded = 0;
  const ms: number[] = [];
  const perQuestion: { id: string; rank: number | null }[] = [];
  for (const q of qs) {
    const t = now();
    const r = await search(q.question, 10);
    ms.push(now() - t);
    if (r.degraded) degraded += 1;
    const idx = r.results.findIndex((h) => q.gold_pages.includes(h.page));
    perQuestion.push({ id: q.id, rank: idx < 0 ? null : idx + 1 });
    if (idx === 0) at1 += 1;
    if (idx >= 0) { at10 += 1; rr += 1 / (idx + 1); }
  }
  ms.sort((a, b) => a - b);
  results.bench = {
    questions: qs.length, hits_at_10: at10, recall_at_1: +(at1 / qs.length).toFixed(3),
    recall_at_10: +(at10 / qs.length).toFixed(3), mrr: +(rr / qs.length).toFixed(3), degraded,
    p50_ms: Math.round(ms[Math.floor(ms.length / 2)]!), p95_ms: Math.round(ms[Math.ceil(ms.length * 0.95) - 1]!),
    ranks: perQuestion,
  };
});

test('compile: L02 through the API until a terminal state', async () => {
  only('compile');
  test.setTimeout(75 * 60_000);
  await ensureLesson();
  const t0 = now();
  const started = await post(`/api/lessons/${lessonId}/compile`, {});
  expect(started.status(), 'compile accepted').toBe(202);
  const runId = ((await started.json()) as { run_id: string }).run_id;
  let status: Record<string, unknown> = {};
  for (const deadline = t0 + 70 * 60_000; now() < deadline; await sleep(15_000)) {
    status = (await (await api.get(`/api/compiles/${runId}`)).json()) as Record<string, unknown>;
    if (TERMINAL.has(String(status.state))) break;
  }
  const g2 = (status.g2 ?? null) as { publishable?: boolean; failing?: string[]; blocking_open?: number } | null;
  results.compile = {
    run_id: runId, state: status.state, wall_s: Math.round((now() - t0) / 1000), spent_usd: status.spent_usd,
    cap_usd: status.cap_usd, refusals: status.refusals,
    steps: (status.steps as Record<string, unknown>[] | undefined)?.map((s) => ({
      step: s.step, stage: s.stage, model: s.model, cost_usd: s.cost_usd, duration_s: s.duration_s, reused: s.reused, status: s.status,
    })),
    g2: g2 ? { publishable: g2.publishable, failing: g2.failing, blocking_open: g2.blocking_open } : null,
  };
  const meta = (await (await api.get(`/api/lessons/${lessonId}`)).json()) as { revisions: { sha256: string; cell_count: number }[] };
  const latest = meta.revisions[0];
  if (latest) {
    const nb = (await (await api.get(`/api/lessons/${lessonId}/revisions/${latest.sha256}`)).json()) as {
      cells: { cell_type: string; metadata?: { reader?: { kind?: string; finding?: { blocking?: boolean } } } }[];
    };
    const kinds = nb.cells.map((c) => c.metadata?.reader?.kind ?? c.cell_type);
    const count = (k: string) => kinds.filter((x) => x === k).length;
    results.lesson_revision = {
      sha256: latest.sha256, cells: nb.cells.length, explain: count('explain'), lab: count('lab'),
      open_findings: count('open'),
      blocking_open: nb.cells.filter((c) => c.metadata?.reader?.kind === 'open' && c.metadata.reader.finding?.blocking).length,
    };
  }
  expect(TERMINAL.has(String(status.state)), `terminal state (got ${String(status.state)})`).toBe(true);
  expect(status.state).not.toBe('stage_failed');
});

const lab = () => page.locator('.lab').first();

test('lesson: L02 renders, a lab cell runs, citations render', async () => {
  only('lesson');
  test.setTimeout(5 * 60_000);
  await ensureLesson();
  await page.goto(`/lessons/${lessonId}`);
  await expect(page.locator('h1, h2').first()).toBeVisible({ timeout: 30_000 });
  await shot('slice-01-l02-notebook');
  const refs = page.locator('ol.references li');
  const references = await refs.count();
  const labs = await page.locator('.lab').count();
  results.lesson = { references, labs };
  expect(references, 'span citations render as references').toBeGreaterThan(0);
  expect(labs, 'the lesson has a lab cell').toBeGreaterThan(0);
  await lab().scrollIntoViewIfNeeded();
  const t = now();
  await lab().getByRole('button', { name: 'Run', exact: true }).click();
  // Scoped to the run-state status (WEB-NOTEBOOK FR-NOTEBOOK-9): an output reading exactly "done" must not match.
  const label = lab().getByRole('status').filter({ hasText: /^(done|error|timed out)$/ });
  await expect(label).toBeVisible({ timeout: 120_000 });
  const state = (await label.first().textContent())?.trim();
  const outputs = await lab().locator('.outputs').innerText().catch(() => '');
  results.lesson = { references, labs, lab_state: state, lab_output_chars: outputs.length, lab_run_s: +((now() - t) / 1000).toFixed(1) };
  await shot('slice-02-l02-lab-run');
  expect(state).toBe('done');
  expect(outputs.length).toBeGreaterThan(0);
});

test('tutor: three questions through the Ask panel', async () => {
  only('tutor');
  test.setTimeout(6 * 60_000);
  await ensureLesson();
  if (!page.url().includes(lessonId)) await page.goto(`/lessons/${lessonId}`);
  const panel = page.getByRole('region', { name: 'Tutor' });
  // Design pass (SRV-TUTOR FR-TUTOR-11): the panel is closed by default; the lesson toolbar opens it.
  if (!(await panel.isVisible())) await page.locator('.lesson-toolbar').getByRole('button', { name: 'Ask the tutor' }).click({ timeout: 30_000 });
  await expect(panel).toBeVisible({ timeout: 30_000 });
  // t01 replaces questions-v2 v03, whose premise the book does not support (SRV-TUTOR VEC-TUTOR-7, 2026-11-23)
  const t01 = fixedQuestions()[0]!;
  type Asked = { n: number; question_id: string | null; ask_status: number; latency_s: number | null;
    state: string | null; grounded: boolean | null; error_code: string | null };
  const asked: Asked[] = [];
  const waiting = panel.getByText('The tutor is answering…');
  for (const [i, q] of [t01.question, ...TUTOR_EXTRA].entries()) {
    await panel.getByLabel('Ask the tutor').fill(q);
    const t = now();
    // This question's own POST, not the previous answer still on screen (deep-reviewer, 2026-11-23).
    const posted = page.waitForResponse((r) => r.request().method() === 'POST' && /\/api\/lessons\/[^/]+\/ask$/.test(r.url()));
    await panel.getByRole('button', { name: 'Ask' }).click();
    const res = await posted;
    const qid = res.status() === 202 ? ((await res.json()) as { question_id: string }).question_id : null;
    const entry: Asked = { n: i + 1, question_id: qid, ask_status: res.status(), latency_s: null, state: null, grounded: null, error_code: null };
    asked.push(entry);
    if (!qid) continue;
    // Server TTL is 600 s (tutor params); spike tutor-call measured 1.5–3.5 s per call, so 180 s is generous.
    await expect(waiting).toBeHidden({ timeout: 180_000 });
    // The panel polls on a 1/2/5 s backoff, so these latencies are quantised up to about 5 s (upper bounds).
    entry.latency_s = +((now() - t) / 1000).toFixed(1);
    const st = (await (await api.get(`/api/lessons/${lessonId}/questions/${qid}`)).json()) as {
      state: string; grounded?: boolean | null; error_code?: string | null;
    };
    Object.assign(entry, { state: st.state, grounded: st.grounded ?? null, error_code: st.error_code ?? null });
    if (i === 0) await shot('slice-03-tutor-answer');
  }
  const latencies = asked.flatMap((a) => (a.latency_s === null ? [] : [a.latency_s])).sort((a, b) => a - b);
  results.tutor = {
    asked,
    grounded_rate: +(asked.filter((q) => q.grounded === true).length / asked.length).toFixed(3),
    p95_latency_s: latencies.length ? latencies[Math.ceil(latencies.length * 0.95) - 1] : null,
    latency_note: 'UI wall time from click to the answering notice clearing; quantised by the 1/2/5 s poll',
    cost_per_question_usd: null, // no allowed interface exposes the tutor's agent_spend rows
  };
  expect(asked.every((q) => q.state === 'answered')).toBe(true);
});

const WIDTHS = [1280, 1024, 390] as const;

test('ui: review screenshots and axe at 1280, 1024 and 390 px', async ({ browser }) => {
  only('ui');
  test.setTimeout(10 * 60_000);
  await ensureLesson();
  const dir = resolve(OUT, 'ui');
  mkdirSync(resolve(dir, 'data'), { recursive: true });
  const meta = (await (await api.get(`/api/lessons/${lessonId}`)).json()) as { lesson: { latest_sha256: string | null } };
  const sha = meta.lesson.latest_sha256;
  const lessonsText = await (await api.get('/api/lessons')).text();
  writeFileSync(resolve(dir, 'data', 'lessons.json'), lessonsText);
  type Listed = { lesson_id: string; course_id: string; course_title: string; title: string; latest_sha256: string | null };
  const listed = (JSON.parse(lessonsText) as { lessons: Listed[] }).lessons;
  const unpublished = listed.find((x) => !x.latest_sha256);
  const l02Title = listed.find((x) => x.lesson_id === lessonId)?.title ?? '';
  writeFileSync(resolve(dir, 'data', 'lesson.json'), JSON.stringify(meta));
  if (sha) writeFileSync(resolve(dir, 'data', 'revision.json'), await (await api.get(`/api/lessons/${lessonId}/revisions/${sha}`)).text());
  writeFileSync(resolve(dir, 'data', 'questions.json'), await (await api.get(`/api/lessons/${lessonId}/questions`)).text());
  const axe: Record<string, string[]> = {};
  const signinAxe: Record<string, string[]> = {}; // SRV-AUTH's own page: recorded only (follow-up amendment)
  type Layout = {
    overflow_px: number; widest_p_px: number; p_font_size: string; p_line_height: string; p_font_family: string; h1_px: string;
    h1_family: string; chars_per_line_median: number | null; article_lang: string | null; td_hyphens: string | null;
    td_overflow_wrap: string | null; toolbar_button_radius: string | null; toolbar_button_height: number | null;
    lab_px: number | null; header_px: number | null;
  };
  const layout: Record<string, Layout> = {};
  const serious = (r: { violations: { impact?: string | null; id: string; nodes: unknown[] }[] }) =>
    r.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical').map((v) => `${v.id}:${v.nodes.length}`);
  // WEB-UI FR-UI-10: no horizontal page scroll, prose column, type, no serious or critical axe issue.
  const check = async (name: string) => {
    axe[name] = serious(await new AxeBuilder({ page }).analyze());
    layout[name] = await page.evaluate(() => {
      const ps = [...document.querySelectorAll('[data-markdown] p')] as HTMLElement[];
      const cs = ps[0] ? getComputedStyle(ps[0]) : null;
      // Characters per line, an estimate: text length over line count for paragraphs of 3 or more lines.
      const perLine = ps
        .map((p) => {
          const lh = parseFloat(getComputedStyle(p).lineHeight);
          const lines = Math.round(p.getBoundingClientRect().height / lh);
          return lines >= 3 ? (p.textContent ?? '').length / lines : null;
        })
        .filter((x): x is number => x !== null)
        .sort((a, b) => a - b);
      const h1 = document.querySelector('#content h1');
      // Design pass (FR-UI-10, 2026-11-23): page title family, lesson language, table breaking, toolbar buttons, the
      // lab block's width against the reading column, the header's height.
      const td = document.querySelector('[data-markdown] td');
      const tdCs = td ? getComputedStyle(td) : null;
      const tb = document.querySelector('.lesson-toolbar .acme-button');
      const tbCs = tb ? getComputedStyle(tb) : null;
      const labBlock = document.querySelector('.cell[data-kind="lab"] .lab') as HTMLElement | null;
      const header = document.querySelector('header.app-header') as HTMLElement | null;
      return {
        // clientWidth excludes a vertical scrollbar, which innerWidth includes.
        overflow_px: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        widest_p_px: Math.max(0, ...ps.map((p) => Math.round(p.getBoundingClientRect().width))),
        p_font_size: cs?.fontSize ?? '',
        p_line_height: cs?.lineHeight ?? '',
        p_font_family: cs?.fontFamily ?? '',
        h1_px: h1 ? getComputedStyle(h1).fontSize : '',
        h1_family: h1 ? getComputedStyle(h1).fontFamily : '',
        chars_per_line_median: perLine.length ? Math.round(perLine[Math.floor(perLine.length / 2)]!) : null,
        article_lang: document.querySelector('#content div.lesson')?.getAttribute('lang') ?? null,
        td_hyphens: tdCs?.hyphens ?? null,
        td_overflow_wrap: tdCs?.overflowWrap ?? null,
        toolbar_button_radius: tbCs?.borderTopLeftRadius ?? null,
        toolbar_button_height: tb ? Math.round(tb.getBoundingClientRect().height) : null,
        lab_px: labBlock ? Math.round(labBlock.getBoundingClientRect().width) : null,
        header_px: header ? Math.round(header.getBoundingClientRect().height) : null,
      };
    });
  };
  const el = async (name: string, selector: string) => {
    const loc = page.locator(selector).first();
    if ((await loc.count()) === 0) return;
    await loc.scrollIntoViewIfNeeded();
    await loc.screenshot({ path: resolve(dir, `${name}.png`) });
  };
  const nav = () => page.getByRole('navigation', { name: 'Lesson contents' });
  const toggle = () => page.getByRole('button', { name: 'Contents' });
  for (const w of WIDTHS) {
    // Sign-in page in a fresh context (no session).
    const anon = await browser.newContext({ viewport: { width: w, height: 900 } });
    const p = await anon.newPage();
    await p.goto('/api/auth/signin');
    await p.screenshot({ path: resolve(dir, `${w}-signin.png`), fullPage: true });
    signinAxe[`${w}-signin`] = serious(await new AxeBuilder({ page: p }).analyze());
    await anon.close();

    await page.setViewportSize({ width: w, height: 900 });
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Lessons' })).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ path: resolve(dir, `${w}-list.png`), fullPage: true });
    await check(`${w}-list`);
    // Design pass, FR-UI-6: every published lesson is a visible link on `/` before any click; a group of more than 12
    // lessons starts collapsed with exactly its published rows.
    for (const l of listed.filter((x) => x.latest_sha256))
      await expect.soft(page.locator(`#content a[href="/lessons/${l.lesson_id}"]`).first(), `published ${l.lesson_id} linked (${w})`).toBeVisible();
    const byCourse = new Map<string, Listed[]>();
    for (const x of listed) byCourse.set(x.course_id, [...(byCourse.get(x.course_id) ?? []), x]);
    for (const [courseId, ls] of byCourse) {
      if (ls.length <= 12) continue;
      const group = page.locator(`#content #course-${courseId}`);
      await expect.soft(group.getByRole('button', { expanded: false }).first(), `course ${courseId} collapsed (${w})`).toBeVisible();
      await expect.soft(group.getByRole('listitem'), `course ${courseId} shows its published rows (${w})`).toHaveCount(ls.filter((x) => x.latest_sha256).length);
    }
    // FR-UI-17: the sidebar beside the page at 1280, behind the header's Courses button below 75rem.
    const sidebar = page.getByRole('navigation', { name: 'Courses' });
    if (w === 1280) {
      await expect.soft(sidebar.first(), 'sidebar visible (1280)').toBeVisible();
    } else {
      await expect.soft(sidebar.first(), `sidebar hidden (${w})`).toBeHidden();
      await page.getByRole('button', { name: 'Courses' }).click();
      await expect.soft(page.getByRole('dialog').getByRole('navigation', { name: 'Courses' }), `drawer opens (${w})`).toBeVisible();
      await page.screenshot({ path: resolve(dir, `${w}-list-sidebar-drawer.png`) });
      await page.keyboard.press('Escape');
    }
    // FR-UI-11: the skip link is the first Tab stop and moves focus to main#content.
    await page.locator('body').focus();
    await page.keyboard.press('Tab');
    await expect.soft(page.getByRole('link', { name: 'Skip to content' })).toBeFocused();
    await page.screenshot({ path: resolve(dir, `${w}-skip-link.png`) });
    await page.keyboard.press('Enter');
    await expect.soft(page.locator('main#content')).toBeFocused();

    await page.goto(`/lessons/${lessonId}`);
    await expect(page.locator('.cell').first()).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ path: resolve(dir, `${w}-lesson-top.png`) });
    if (w !== 390) await page.screenshot({ path: resolve(dir, `${w}-lesson-full.png`), fullPage: true });
    await el(`${w}-lesson-references`, '.cell:has(.references)');
    await el(`${w}-lesson-lab`, '.cell:has(.lab)');
    await el(`${w}-lesson-open`, '.cell[data-kind="open"]');
    await el(`${w}-lesson-recall`, '.recall-deck');
    await check(`${w}-lesson`);
    // Design pass: breadcrumbs (FR-NOTEBOOK-11), the sidebar's current lesson at 1280 (FR-UI-17), the tutor panel
    // opened from the toolbar (SRV-TUTOR FR-TUTOR-11).
    await expect.soft(page.getByRole('navigation', { name: 'Breadcrumb' }).locator('[aria-current="page"]'), `breadcrumb current (${w})`).toHaveText(l02Title);
    if (w === 1280)
      await expect.soft(page.getByRole('navigation', { name: 'Courses' }).first().locator(`a[href="/lessons/${lessonId}"]`), 'sidebar marks L02').toHaveAttribute('aria-current', 'page');
    await page.locator('.lesson-toolbar').getByRole('button', { name: 'Ask the tutor' }).click();
    await el(`${w}-lesson-tutor`, '#tutor');
    axe[`${w}-lesson-tutor-open`] = serious(await new AxeBuilder({ page }).include('#tutor').analyze());
    await page.getByRole('button', { name: 'Close the tutor' }).click();
    // WEB-NOTEBOOK FR-NOTEBOOK-11: the contents column at 1280 and 1024, behind the toggle at 390.
    await page.evaluate(() => window.scrollTo(0, 0));
    const firstEntry = nav().getByRole('link').first();
    if (w === 390) {
      await expect.soft(firstEntry, 'contents hidden before the toggle (390)').toBeHidden();
      await toggle().click();
      await expect.soft(firstEntry, 'contents visible after the toggle (390)').toBeVisible();
      await page.screenshot({ path: resolve(dir, `${w}-lesson-contents-open.png`) });
    } else {
      await expect.soft(toggle(), `no Contents button (${w})`).toBeHidden();
      await expect.soft(firstEntry, `contents visible (${w})`).toBeVisible();
    }
    // Following the last entry (its target far down the page, not in view before) scrolls to it and focuses it.
    const lastEntry = nav().getByRole('link').last();
    const href = (await lastEntry.getAttribute('href')) ?? '';
    await expect.soft(page.locator(href), `${href} not in view before the click (${w})`).not.toBeInViewport();
    await lastEntry.click();
    await expect.soft(page.locator(href), `${href} focused (${w})`).toBeFocused();
    await expect.soft(page.locator(href), `${href} in view (${w})`).toBeInViewport();
    if (w === 390) await expect.soft(firstEntry, 'contents close after an entry (390)').toBeHidden();
    // FR-NOTEBOOK-11 (design pass): after the scroll exactly one entry is the current section.
    await expect.soft(nav().locator('a[aria-current="location"]'), `one current section (${w})`).toHaveCount(1);
    // Keyboard: the first Tab stops from the top of the lesson, screenshot of the focused control.
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.locator('body').focus();
    for (let i = 0; i < 4; i += 1) await page.keyboard.press('Tab');
    await page.screenshot({ path: resolve(dir, `${w}-lesson-focus.png`) });

    await page.goto('/no-such-page');
    await page.screenshot({ path: resolve(dir, `${w}-notfound.png`), fullPage: true });
    await check(`${w}-notfound`);
    // Design pass (FR-UI-10): no overflow on an unpublished lesson and on /import either (GETs only; no plan is
    // previewed or confirmed here).
    if (unpublished) {
      await page.goto(`/lessons/${unpublished.lesson_id}`);
      await expect(page.locator('#content h1')).toBeVisible({ timeout: 30_000 });
      await page.screenshot({ path: resolve(dir, `${w}-unpublished.png`), fullPage: true });
      await check(`${w}-unpublished`);
    }
    await page.goto('/import');
    await expect(page.getByRole('heading', { name: 'Import a source' })).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ path: resolve(dir, `${w}-import.png`), fullPage: true });
    await check(`${w}-import`);
  }
  // Design pass (FR-UI-10, FR-UI-16): the list and L02 in the dark scheme at 1280 px, axe and overflow.
  const dark = await browser.newContext({ storageState: await context.storageState(), viewport: { width: 1280, height: 900 }, colorScheme: 'dark' });
  const dp = await dark.newPage();
  for (const [name, path] of [['dark-1280-list', '/'], ['dark-1280-lesson', `/lessons/${lessonId}`]] as const) {
    await dp.goto(path);
    await expect(dp.locator('#content h1').first()).toBeVisible({ timeout: 30_000 });
    await dp.screenshot({ path: resolve(dir, `${name}.png`) });
    axe[name] = serious(await new AxeBuilder({ page: dp }).analyze());
    const overflow = await dp.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect.soft(overflow, `no horizontal scroll ${name}`).toBeLessThanOrEqual(0);
  }
  await dark.close();
  results.ui = { widths: [...WIDTHS], axe_serious_critical: axe, signin_axe_serious_critical_report_only: signinAxe, layout, revision: sha };
  for (const [name, issues] of Object.entries(axe)) expect.soft(issues, `axe ${name}`).toEqual([]);
  for (const [name, l] of Object.entries(layout)) {
    expect.soft(l.overflow_px, `no horizontal scroll ${name}`).toBeLessThanOrEqual(0);
    const w = Number(name.split('-')[0]);
    // Design pass (FR-UI-11): the header is present and one row (its declared 3.5rem min-height) at every width.
    expect.soft(l.header_px, `header present ${name}`).not.toBeNull();
    expect.soft(l.header_px ?? Infinity, `header one row ${name}`).toBeLessThanOrEqual(56);
    if (name.endsWith('-list')) {
      // FR-UI-10 (design pass): the app page title in the sans.
      expect.soft(l.h1_px, `page title size ${name}`).toBe(w === 390 ? '24px' : '28px');
      expect.soft(l.h1_family.startsWith('ui-serif'), `page title family ${name}`).toBe(false);
    }
    if (!name.endsWith('-lesson')) continue;
    expect.soft(l.widest_p_px, `reading column > 0 ${name}`).toBeGreaterThan(0);
    expect.soft(l.widest_p_px, `reading column ≤ 608 ${name}`).toBeLessThanOrEqual(608);
    expect.soft(l.p_font_size, `prose size ${name}`).toBe('17px');
    expect.soft(Math.abs(parseFloat(l.p_line_height) - 28.05), `prose line height ${name}`).toBeLessThanOrEqual(0.1);
    expect.soft(l.p_font_family.startsWith('ui-serif'), `prose family ${name}`).toBe(true);
    // Design pass (FR-UI-10): heading scale, language, table breaking, toolbar buttons, the wide lab block. Each hook
    // must be present before its value is checked (review).
    for (const [hook, value] of [
      ['#content div.lesson[lang]', l.article_lang], ['[data-markdown] td', l.td_hyphens],
      ['.lesson-toolbar .acme-button', l.toolbar_button_height], ['.cell[data-kind="lab"] .lab', l.lab_px],
    ] as const)
      expect.soft(value, `${hook} present ${name}`).not.toBeNull();
    expect.soft(l.h1_px, `h1 size ${name}`).toBe(w === 390 ? '28px' : '32px');
    expect.soft(l.article_lang, `lesson lang ${name}`).toBe('ru');
    expect.soft(l.td_hyphens, `td hyphens ${name}`).toBe('auto');
    expect.soft(l.td_overflow_wrap, `td overflow-wrap ${name}`).toBe('break-word');
    expect.soft(l.toolbar_button_radius, `toolbar button radius ${name}`).toBe('6px');
    expect.soft(l.toolbar_button_height, `toolbar button height ${name}`).toBe(28);
    if (w === 390) expect.soft(l.lab_px ?? 0, `lab within the column ${name}`).toBeLessThanOrEqual(l.widest_p_px + 1);
    else {
      expect.soft(l.lab_px ?? 0, `lab wider than prose ${name}`).toBeGreaterThanOrEqual(l.widest_p_px + 64);
      expect.soft(l.lab_px ?? 0, `lab at most 800 ${name}`).toBeLessThanOrEqual(800);
    }
  }
});

// SRV-AUTH FR-AUTH-21 [live]: the restyled pages on the running stack, and the passkey flow proven unchanged.
test('auth: the sign-in and bootstrap pages, then a real sign-out and sign-in', async ({ browser }) => {
  only('auth');
  test.setTimeout(5 * 60_000);
  const dir = resolve(OUT, 'auth');
  mkdirSync(dir, { recursive: true });
  const axe: Record<string, string[]> = {};
  const overflow: Record<string, number> = {};
  for (const w of WIDTHS) {
    // A fresh context per page: no session, so the pages render as a visitor sees them. The bootstrap page is
    // opened without a token (the no-token state), so no bootstrap request is made.
    for (const name of ['signin', 'bootstrap'] as const) {
      const anon = await browser.newContext({ viewport: { width: w, height: 900 } });
      const p = await anon.newPage();
      await p.goto(`/api/auth/${name}`);
      await expect(p.locator('main.auth')).toHaveAttribute('data-state', name === 'signin' ? 'ready' : 'no_token');
      await p.screenshot({ path: resolve(dir, `${w}-${name}.png`) });
      const r = await new AxeBuilder({ page: p }).analyze();
      axe[`${w}-${name}`] = r.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical').map((v) => v.id);
      overflow[`${w}-${name}`] = await p.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      if (name === 'signin') {
        await p.keyboard.press('Tab');
        await expect.soft(p.locator('#go'), `button focused (${w})`).toBeFocused();
        const outline = await p.locator('#go').evaluate((b) => `${getComputedStyle(b).outlineWidth} ${getComputedStyle(b).outlineStyle}`);
        expect.soft(outline, `focus outline (${w})`).toBe('2px solid');
        await p.screenshot({ path: resolve(dir, `${w}-signin-focus.png`) });
      }
      await anon.close();
    }
  }
  // slice.live.ts's sequence ("sign out, then sign in through the real page with the same passkey"), for the owner.
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto('/');
  // Design pass (WEB-UI FR-UI-5, FR-UI-11): Sign out is an item of the user menu.
  await page.getByRole('button', { name: /^Account menu for / }).click();
  await page.getByRole('menuitem', { name: 'Sign out' }).click();
  await page.waitForURL(/\/api\/auth\/signin$/);
  expect((await api.get('/api/me')).status(), 'signed out').toBe(401);
  await page.screenshot({ path: resolve(dir, 'signed-out.png') });
  await page.getByRole('button', { name: 'Sign in with a passkey' }).click();
  await page.waitForURL((u) => u.pathname === '/', { timeout: 30_000 });
  const me = await api.get('/api/me');
  expect(me.status(), 'signed in again through the page').toBe(200);
  expect(((await me.json()) as { role: string }).role).toBe('owner');
  results.auth = { widths: [...WIDTHS], axe_serious_critical: axe, overflow_px: overflow, signin_again: me.status() };
  for (const [k, v] of Object.entries(axe)) expect.soft(v, `axe ${k}`).toEqual([]);
  for (const [k, v] of Object.entries(overflow)) expect.soft(v, `overflow ${k}`).toBeLessThanOrEqual(0);
});

// SRV-TUTOR NFR-TUTOR-1..3 over the HTTP API (VEC-TUTOR-7): questions-v2 with v03 → t01 (40 independent questions),
// all asked from L02 without a cell (retrieval is book-wide), each polled every 0.5 s to a terminal state. Each run
// writes its own record file to READER_TUTOR_BENCH_DIR (default: the run's OUT) and asks only questions no earlier
// run file there recorded; READER_TUTOR_BENCH_FROM skips further by index. A 429 whose body code is
// daily_budget_exhausted stops the run (resume the next UTC day); the summary merges every run file. Numbers, states
// and ids only.
test('tutor-bench: the 40 bench questions over the HTTP API', async () => {
  only('tutor-bench');
  test.setTimeout(150 * 60_000);
  await ensureLesson();
  const set = benchSet(questions(), fixedQuestions());
  const setIds = set.map((q) => q.id);
  const dir = resolve(process.env.READER_TUTOR_BENCH_DIR || OUT);
  mkdirSync(dir, { recursive: true });
  const prior = readdirSync(dir)
    .filter((f) => /^tutor-bench-.+\.json$/.test(f))
    .sort()
    .map((f) => (JSON.parse(readFileSync(resolve(dir, f), 'utf-8')) as { records: BenchRecord[] }).records);
  const done = new Set(mergeRuns(prior).map((r) => r.id));
  const from = Number(process.env.READER_TUTOR_BENCH_FROM ?? '0');
  const runFile = resolve(dir, `tutor-bench-${new Date().toISOString().replace(/[:.]/g, '')}.json`);
  const records: BenchRecord[] = [];
  let stoppedAt: string | null = null;
  const report = () => {
    writeFileSync(runFile, `${JSON.stringify({ lesson_id: lessonId, records }, null, 2)}\n`);
    results.tutor_bench = {
      lesson_id: lessonId, dir_runs: prior.length + 1, from, stopped_at: stoppedAt, asked_this_run: records.length,
      summary: summary(mergeRuns([...prior, records]), setIds),
    };
    save();
  };
  for (const q of set.slice(from).filter((x) => !done.has(x.id))) {
    // The retrieval predictor (NFR-TUTOR-3): is a gold page among the tutor's 8 hits? POST /api/search spends nothing.
    const goldInTop8 = hitsGold((await search(q.question, 8)).results.map((h) => h.page), q.gold_pages);
    const body = { question: q.question };
    const t = now();
    let res = await post(`/api/lessons/${lessonId}/ask`, body);
    for (let tries = 0; res.status() === 409 && tries < 20; tries += 1) { // the previous row is still finalising
      await sleep(1000);
      res = await post(`/api/lessons/${lessonId}/ask`, body);
    }
    if (res.status() === 429) {
      const code = ((await res.json().catch(() => ({}))) as { error?: string }).error;
      expect(code, `ask ${q.id}: a 429 other than the daily cap`).toBe('daily_budget_exhausted');
      stoppedAt = q.id;
      break;
    }
    expect(res.status(), `ask ${q.id}`).toBe(202);
    const qid = ((await res.json()) as { question_id: string }).question_id;
    type Got = { state: string; grounded?: boolean;
      answer?: { insufficient: boolean; blocks: { supported: boolean; citations: { page: number }[] }[] };
      usage?: { usd: number; output_tokens: number; thinking_tokens: number | null } };
    let got: Got = { state: 'queued' };
    while (got.state === 'queued' && now() - t < 630_000) {
      await sleep(500);
      got = (await (await api.get(`/api/lessons/${lessonId}/questions/${qid}`)).json()) as Got;
    }
    const blocks = got.answer?.blocks ?? [];
    records.push({
      id: q.id, chapter: q.chapter, question_id: qid, state: got.state, wall_s: +((now() - t) / 1000).toFixed(2),
      usd: got.usage?.usd ?? 0, output_tokens: got.usage?.output_tokens ?? 0, thinking_tokens: got.usage?.thinking_tokens ?? null,
      grounded: got.grounded === true, insufficient: got.answer?.insufficient === true, blocks: blocks.length,
      blocks_with_verified_quote: blocks.filter((b) => b.supported).length,
      gold_in_top8: goldInTop8, gold_cited: hitsGold(blocks.flatMap((b) => b.citations.map((c) => c.page)), q.gold_pages),
    });
    report();
  }
  report();
});
