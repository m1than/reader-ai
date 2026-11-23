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
//   tutor-bench  SRV-TUTOR's bench (VEC-TUTOR-7): 40 generated questions + t01 over the HTTP API; spends ≈ $2.
// Written: READER_LIVE_OUT/slice-results.json with numbers, states and ids only — never book text, a question, a
// snippet or an answer. Screenshots go to READER_LIVE_OUT/screenshots (outside the repository when slice.sh runs).
import { expect, test, type APIRequestContext, type BrowserContext, type CDPSession, type Page } from '@playwright/test';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { benchQuestions, summary, type BenchRecord } from '../../../web/src/tutor/bench.ts';

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
type Question = { id: string; gold_pages: number[]; question: string };
type Hit = { page: number };

const plan = (): Plan => JSON.parse(readFileSync(process.env.READER_SLICE_PLAN ?? '', 'utf-8')) as Plan;
const questions = (): Question[] => JSON.parse(readFileSync(process.env.READER_SLICE_QUESTIONS ?? '', 'utf-8')) as Question[];
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
  await page.getByRole('button', { name: 'Register passkey' }).click();
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
  // Scoped to the controls: an output reading exactly "done" must not match the state label.
  const label = lab().locator('.lab-controls').getByText(/^(done|error|timed out)$/);
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
  await expect(panel).toBeVisible({ timeout: 30_000 });
  // t01 replaces questions-v2 v03, whose premise the book does not support (SRV-TUTOR VEC-TUTOR-7, 2026-11-23)
  const v03 = (JSON.parse(readFileSync(process.env.READER_SLICE_TUTOR_FIXED ?? '', 'utf-8')) as { question: string }[])[0]!;
  type Asked = { n: number; question_id: string | null; ask_status: number; latency_s: number | null;
    state: string | null; grounded: boolean | null; error_code: string | null };
  const asked: Asked[] = [];
  const waiting = panel.getByText('The tutor is answering…');
  for (const [i, q] of [v03.question, ...TUTOR_EXTRA].entries()) {
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

// SRV-TUTOR NFR-TUTOR-1..3 over the HTTP API (VEC-TUTOR-7): 40 generated questions plus t01, asked one at a time on
// their cells, each polled every 0.5 s to a terminal state. Stops at the first daily_budget_exhausted and records
// the index; READER_TUTOR_BENCH_FROM resumes there the next UTC day. Numbers, states and ids only.
test('tutor-bench: n >= 40 questions over the HTTP API', async () => {
  only('tutor-bench');
  test.setTimeout(150 * 60_000);
  await ensureLesson();
  const meta = (await (await api.get(`/api/lessons/${lessonId}`)).json()) as { lesson: { latest_sha256: string } };
  const revision = meta.lesson.latest_sha256;
  const notebook = (await (await api.get(`/api/lessons/${lessonId}/revisions/${revision}`)).json()) as { cells: object[] };
  const fixed = JSON.parse(readFileSync(process.env.READER_SLICE_TUTOR_FIXED ?? '', 'utf-8')) as { id: string; question: string }[];
  const all = [
    ...benchQuestions(notebook).map((q, i) => ({ id: `g${i + 1}`, cellId: q.cellId as string | null, question: q.question })),
    ...fixed.map((f) => ({ id: f.id, cellId: null as string | null, question: f.question })),
  ];
  const from = Number(process.env.READER_TUTOR_BENCH_FROM ?? '0');
  const records: BenchRecord[] = [];
  let stoppedAt: number | null = null;
  for (let i = from; i < all.length; i += 1) {
    const q = all[i]!;
    const body = { question: q.question, revision, ...(q.cellId ? { cell_id: q.cellId } : {}) };
    const t = now();
    let res = await post(`/api/lessons/${lessonId}/ask`, body);
    for (let tries = 0; res.status() === 409 && tries < 20; tries += 1) { // the previous row is still finalising
      await sleep(1000);
      res = await post(`/api/lessons/${lessonId}/ask`, body);
    }
    if (res.status() === 429) { stoppedAt = i; break; }
    expect(res.status(), `ask ${q.id}`).toBe(202);
    const qid = ((await res.json()) as { question_id: string }).question_id;
    type Got = { state: string; grounded?: boolean; answer?: { insufficient: boolean; blocks: { supported: boolean }[] };
      usage?: { usd: number; output_tokens: number; thinking_tokens: number | null } };
    let got: Got = { state: 'queued' };
    while (got.state === 'queued' && now() - t < 630_000) {
      await sleep(500);
      got = (await (await api.get(`/api/lessons/${lessonId}/questions/${qid}`)).json()) as Got;
    }
    const blocks = got.answer?.blocks ?? [];
    records.push({
      id: q.id, question_id: qid, state: got.state, wall_s: +((now() - t) / 1000).toFixed(2),
      usd: got.usage?.usd ?? 0, output_tokens: got.usage?.output_tokens ?? 0, thinking_tokens: got.usage?.thinking_tokens ?? null,
      grounded: got.grounded === true, insufficient: got.answer?.insufficient === true, blocks: blocks.length,
      blocks_with_verified_quote: blocks.filter((b) => b.supported).length,
    });
    results.tutor_bench = { lesson_id: lessonId, revision, from, stopped_at: stoppedAt, records, summary: summary(records) };
    save();
  }
  results.tutor_bench = { lesson_id: lessonId, revision, from, stopped_at: stoppedAt, records, summary: summary(records) };
});
