#!/usr/bin/env node
// Test server for the [browser] tests (WEB-UI INV-UI-3, INV-UI-4; WEB-RENDER). Serves web/dist with the production
// Content-Security-Policy read from deploy/Caddyfile, web/storybook-static under /storybook/, and stubs the API with
// the spec fixtures (SRV-AUTH /api/me; SRV-LESSON list, lesson and revision; SRV-KERNEL session routes; no WebSocket).
//   node test-server/server.mjs [port]      (default 4173, bound to 127.0.0.1)
import { createServer } from 'node:http';
import { readFileSync, statSync } from 'node:fs';
import { extname, join, normalize, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { staleBuilds } from './stale.mjs';

const web = resolve(fileURLToPath(new URL('..', import.meta.url)));
const repo = resolve(web, '..');
const port = Number(process.argv[2] ?? 4173);

/** The CSP of the Caddyfile's site block, so the test runs under exactly the production policy. */
export const cspFromCaddyfile = (text) => {
  const m = /Content-Security-Policy\s+"([^"]+)"/.exec(text);
  if (!m) throw new Error('no Content-Security-Policy in deploy/Caddyfile');
  return m[1];
};

// WEB-UI INV-UI-4 "Fresh builds": refuse a dist or storybook-static older than its inputs (stale.mjs).
const stale = staleBuilds(web);
if (stale.length > 0) {
  console.error(`test server: ${stale.join(' and ')} missing or older than the web sources; run \`npm run build && npm run build-storybook\` first`);
  process.exit(1);
}

const CSP = cspFromCaddyfile(readFileSync(join(repo, 'deploy/Caddyfile'), 'utf8'));
const fixtures = JSON.parse(readFileSync(join(repo, 'spec/fixtures/WEB-UI/lessons.json'), 'utf8'));
const notebook = readFileSync(join(repo, 'spec/fixtures/WEB-NOTEBOOK/lesson-w01.ipynb'), 'utf8');
const signups = JSON.parse(readFileSync(join(repo, 'spec/fixtures/WEB-ACCOUNTS/signups.json'), 'utf8'));
const me = { id: '0b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b10', handle: 'ivan', role: 'owner', display_name: 'Ivan', consent: { evaluation: true, tuning: false } };
// WEB-FRESH (paired change): the freshness routes answer from VEC-FRESHUI-1, one state per material id.
const fresh = JSON.parse(readFileSync(join(repo, 'spec/fixtures/WEB-FRESH/bodies.json'), 'utf8'));
const freshState = (id) => Object.values(fresh.states).find((s) => s.material_id === id);

const freshApi = (method, path) => {
  const m = /^\/api\/materials\/([^/]+)\/(freshness|bibliography)$/.exec(path);
  const state = m && freshState(decodeURIComponent(m[1]));
  if (!state) return null;
  if (method === 'GET' && m[2] === 'freshness') return [200, state.body];
  if (method === 'PUT' && m[2] === 'bibliography') return [200, fresh.bibliography_saved];
  if (method === 'POST' && m[2] === 'freshness') return [fresh.post_accepted.status, fresh.post_accepted.body];
  return null;
};

const TYPES = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css',
  '.json': 'application/json', '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.ico': 'image/x-icon', '.map': 'application/json',
};

// WEB-UI FR-UI-15: a compile of w02 shaped like a real one. No run until the POST; then `running` at fact_check with
// the draft's revision already set for two reads; then `converged` (w02 leaves GET /api/compiles). Global state, so
// the Playwright config runs one worker; `POST /__test__/reset[?w02=stopped]` restores a start.
const DRAFT_SHA = '4'.repeat(64);
const compile = { phase: 'none', reads: 0 };
const resetCompile = (mode) => Object.assign(compile, { phase: mode === 'stopped' ? 'stopped' : 'none', reads: 0 });
const draftDone = { step: 1, stage: 'draft', model: 'claude-opus-5-5', cost_usd: '0.600000', duration_s: 157, reused: false };
const runOf = (state, steps, extra = {}) => ({
  run_id: 'run-w02', lesson_id: 'w02', state, cap_usd: '8.00', steps, spent_usd: '0.60', task_budgets: {}, refusals: [],
  g2: null, latest_sha256: DRAFT_SHA, error: null, ...extra,
});
/** One read of w02's run state: advances the running sequence. */
const readRun = () => {
  if (compile.phase === 'running') {
    compile.reads += 1;
    if (compile.reads > 2) compile.phase = 'converged';
  }
  if (compile.phase === 'running') return runOf('running', [draftDone, { step: 2, stage: 'fact_check', status: 'running', reused: false }]);
  if (compile.phase === 'stopped')
    return runOf('stage_failed', [draftDone, { step: 2, stage: 'fact_check', status: 'dead', reused: false }], { error: 'provider_missing' });
  if (compile.phase === 'converged') return runOf('converged', [draftDone]);
  return null;
};
const withDraft = (l) => (l.lesson_id === 'w02' && compile.phase !== 'none' ? { ...l, latest_sha256: DRAFT_SHA } : l);
const lessonsList = () => ({ lessons: fixtures.list.lessons.map(withDraft) });

const compileRoute = (method, path) => {
  if (method === 'GET' && path === '/api/compiles') {
    const r = readRun();
    return [200, { runs: r && r.state !== 'converged' ? [r] : [] }];
  }
  if (method === 'GET' && path === '/api/lessons/w02/compile') {
    const r = readRun();
    return r ? [200, r] : [404, { error: 'not_found' }];
  }
  if (method === 'GET' && /^\/api\/lessons\/[^/]+\/compile$/.test(path)) return [404, { error: 'not_found' }];
  if (method === 'POST' && path === '/api/lessons/w02/compile') {
    if (compile.phase === 'running') return [409, { error: 'compile_running' }];
    Object.assign(compile, { phase: 'running', reads: 0 });
    return [202, { run_id: 'run-w02' }];
  }
  if (method === 'POST' && path === '/api/compiles/run-w02/resume') {
    if (compile.phase !== 'stopped') return [409, { error: 'not_failed' }];
    Object.assign(compile, { phase: 'running', reads: 0 });
    return [202, runOf('running', [draftDone, { step: 2, stage: 'fact_check', status: 'queued', reused: false }])];
  }
  if (method === 'GET' && path === '/api/lessons/w02/estimate')
    return [200, { lesson_id: 'w02', kchars: '22.104', low_usd: '0.47', high_usd: '1.11', run_cap_usd: '8.00', basis: 'L02', basis_kchars: '58.938', extrapolated: true }];
  return null;
};

const api = (method, path) => {
  if (method === 'GET' && path === '/api/me') return [200, me];
  if (method === 'GET' && path === '/api/lessons') return [200, lessonsList()];
  const lesson = /^\/api\/lessons\/([^/]+)$/.exec(path);
  if (method === 'GET' && lesson) {
    const s = lessonsList().lessons.find((l) => l.lesson_id === decodeURIComponent(lesson[1]));
    // SRV-LESSON FR-LESSON-8: metadata nested under `lesson`.
    const revisions = s?.latest_sha256
      ? [{ sha256: s.latest_sha256, byte_size: 1, cell_count: 1, created_at: s.updated_at, stages: ['fixture'] }]
      : [];
    return s ? [200, { lesson: s, revisions }] : [404, { error: 'not_found' }];
  }
  // WEB-UI FR-UI-14: WEB-ACCOUNTS's routes from VEC-ACCOUNTS-1 (the [browser] specs may override them).
  if (method === 'GET' && path === '/api/auth/signups') return [200, signups.one];
  const action = /^\/api\/auth\/signups\/([^/]+)\/(approve|reject|disable)$/.exec(path);
  if (method === 'POST' && action) {
    const o = signups.outcomes[action[2]];
    return [o.status, o.body];
  }
  const rev = /^\/api\/lessons\/([^/]+)\/revisions\/([0-9a-f]{64})$/.exec(path);
  if (method === 'GET' && rev) return [200, JSON.parse(notebook)];
  if (method === 'POST' && path === '/api/kernel/sessions') return [201, { session_id: 'a'.repeat(32), lesson_id: 'w01' }];
  if (method === 'POST' && (/\/interrupt$/.test(path) || path === '/api/kernel/reset')) return [204, null];
  if (method === 'POST' && path === '/api/auth/signout') return [204, null];
  // WEB-UI FR-UI-12 / WEB-PLAN: one imported synthetic source without a stored plan.
  if (method === 'GET' && path === '/api/materials')
    return [200, { materials: [{ id: 'synthetic-plan', status: 'imported', page_count: 30 }, { id: 'queued-one', status: 'queued', page_count: null }] }];
  return freshApi(method, path) ?? [404, { error: 'not_found' }];
};

/** WEB-PLAN preview and confirm stubs: a synthetic 30-page plan; fits from 2 weeks at 30 min or more a day. */
const planPreview = (body) => {
  const weeks = Number(body.total_weeks);
  const minutes = Number(body.minutes_per_day);
  const days = weeks * 7;
  const count = Math.min(days, 28);
  const each = Math.round(150 / count);
  const fits = each <= minutes && count <= days;
  return {
    plan_sha256: 'c'.repeat(64),
    inputs: { material_id: body.material_id, total_weeks: weeks, minutes_per_day: minutes, rate: body.rate, start_date: body.today, today: body.today },
    lessons: Array.from({ length: count }, (_, i) => ({ id: `p${i + 1}-${i + 1}`, pages: [i + 1, i + 1], minutes: each, fixed: false, title: `Synthetic chapter ${Math.floor(i / 5) + 1}` })),
    summary: {
      lesson_count: count, fixed_count: 0, new_count: count, study_days: days, start_date: body.today, end: body.today,
      days_left: days, spare_days: Math.max(0, days - count), minutes_mean: each, minutes_max: each + 1,
      required_minutes: each * count, finish_by: body.today, fits, reasons: fits ? [] : ['minutes'],
      suggest: fits ? null : { minutes_per_day: Math.ceil((each + 1) / 5) * 5, total_weeks: weeks + 1 },
    },
    estimate: {
      course_low_usd: '1.23', course_high_usd: '2.90', first_lesson: { low_usd: '0.04', high_usd: '0.10' }, run_cap_usd: '8.00',
      basis: 'L02', basis_kchars: '58.938', mean_lesson_kchars: '2.143', extrapolated: true,
    },
  };
};

// WEB-IMPORT / WEB-UI FR-UI-13: the upload routes from VEC-IMPORT-1. An upload walks queued → converting → importing →
// indexing → ready, one state per poll; the cookie `stub-first-run=1` gives an owner with no lessons and no materials.
const imports = JSON.parse(readFileSync(join(repo, 'spec/fixtures/WEB-IMPORT/responses.json'), 'utf8'));
const WALK = ['queued', 'converting', 'importing', 'indexing', 'ready'];
const steps = new Map(); // upload id → its next state, so parallel test workers do not share one walk
const uploadRoute = (method, path, firstRun) => {
  if (firstRun && method === 'GET' && path === '/api/lessons') return [200, { lessons: [] }];
  if (firstRun && method === 'GET' && path === '/api/materials') return [200, { materials: [], inbox_retained_bytes: 0 }];
  if (method === 'POST' && path === '/api/materials/uploads') {
    const id = `00000000-0000-4000-8000-${String(steps.size + 1).padStart(12, '0')}`;
    steps.set(id, 0);
    return [201, { ...imports.views.queued, id }];
  }
  if (method === 'GET' && path === '/api/materials/converter') return [200, imports.converter.online];
  const one = /^\/api\/materials\/uploads\/([^/]+)(\/cancel|\/retry)?$/.exec(path);
  if (one && method === 'GET' && !one[2]) {
    const id = decodeURIComponent(one[1]);
    const step = steps.get(id) ?? 0;
    steps.set(id, Math.min(step + 1, WALK.length - 1));
    return [200, { ...imports.views[WALK[step]], id }];
  }
  if (one && method === 'POST' && one[2] === '/cancel') return [200, { cancelled: true }];
  if (one && method === 'DELETE' && !one[2]) return [200, { discarded: true }];
  return null;
};

const planRoute = (method, path, body) => {
  if (method === 'POST' && path === '/api/plans/preview') return [200, planPreview(body)];
  if (method === 'POST' && path === '/api/plans/confirm')
    return [200, { course_id: 'c', plan_course_id: 'plan-cccccccccccc', lessons: [{ plan_lesson_id: 'p1-1', lesson_id: 'l1', ordinal: 1 }], created: 1, removed: 0, replayed: false, compile: null }];
  return null;
};

// WEB-MANAGE (paired WEB-UI change): the management routes answer from VEC-MANAGE-1, whose course and lesson ids match
// VEC-UI-2. Stateless apart from the role: `POST /__test__/reset?role=learner` serves the learner until the next reset.
const manage = JSON.parse(readFileSync(join(repo, 'spec/fixtures/WEB-MANAGE/responses.json'), 'utf8'));
const session = { role: 'owner' };
const manageRoute = (method, path, query, body) => {
  const m = (re) => re.exec(path);
  const ok = (body) => [200, body];
  const learner = session.role === 'learner';
  if (method === 'GET' && path === '/api/me') return ok(learner ? manage.me_learner : me);
  // A learner owns no course: the list is scoped (empty), the owner-only collections answer 403 (INV-MANAGE-2).
  if (learner && method === 'GET' && path === '/api/courses') return ok({ courses: [] });
  if (learner && /^\/api\/(courses|materials|auth\/users|settings|audit|compiles|plans)/.test(path)) return [403, { error: 'owner_only' }];
  if (method === 'GET') {
    if (path === '/api/courses') return ok(manage.courses[query.get('state') ?? 'active'] ?? { courses: [] });
    let r = m(/^\/api\/courses\/([^/]+)\/lessons$/);
    if (r) return ok(manage.deleted_lessons[decodeURIComponent(r[1])] ?? { lessons: [] });
    if (m(/^\/api\/courses\/[^/]+\/impact$/)) return ok(manage.course_impact);
    if (path === '/api/plans' && query.get('course_id')) return ok(manage.stored_plan);
    if (path === '/api/lessons/w01') return ok({ lesson: fixtures.list.lessons[0], revisions: manage.lesson_detail.revisions });
    if (m(/^\/api\/lessons\/[^/]+\/compiles$/)) return ok(manage.compile_history);
    if (m(/^\/api\/lessons\/[^/]+\/notes$/)) return ok(manage.notes);
    if (path === '/api/bookmarks') return ok(manage.bookmarks);
    if (path === '/api/materials' && query.get('state') === 'deleted') return ok(manage.materials.deleted);
    r = m(/^\/api\/materials\/([^/]+)\/impact$/);
    if (r) return ok(manage.material_impact[decodeURIComponent(r[1])] ?? { courses: [], lessons: 0, uploads_open: 0 });
    if (path === '/api/auth/users') return ok(manage.people);
    if (path === '/api/auth/credentials') return ok(manage.credentials_three);
    if (path === '/api/auth/sessions') return ok(manage.sessions);
    if (path === '/api/settings') return ok(manage.settings);
    if (path === '/api/audit') return ok(query.get('before') ? manage.audit_page2 : manage.audit_page1);
    return null;
  }
  if (method === 'POST' && path === '/api/courses/order') return ok(manage.courses.active);
  const course = m(/^\/api\/courses\/([^/]+)(\/(archive|unarchive|restore))?$/);
  if (course) {
    const all = [...manage.courses.active.courses, ...manage.courses.archived.courses, ...manage.courses.deleted.courses];
    const row = all.find((c) => c.course_id === decodeURIComponent(course[1]));
    if (!row) return [404, { error: 'not_found' }];
    if (method === 'DELETE') return ok(manage.course_deleted);
    return ok(course[3] ? row : { ...row, version: row.version + 1 });
  }
  if (m(/^\/api\/courses\/[^/]+\/lessons\/order$/)) return ok({});
  if (m(/^\/api\/lessons\/[^/]+\/(skip|unskip|restore)$/)) return ok({});
  if (m(/^\/api\/lessons\/[^/]+\/progress$/)) return ok(manage.progress_done);
  if (m(/^\/api\/lessons\/[^/]+\/revert$/)) return ok(manage.revert_applied);
  if (m(/^\/api\/lessons\/[^/]+\/questions\/clear$/)) return ok(manage.tutor_cleared);
  if (m(/^\/api\/lessons\/[^/]+\/notes$/)) return [201, manage.note_created];
  if (m(/^\/api\/lessons\/[^/]+\/notes\/[^/]+$/)) return method === 'DELETE' ? [204, null] : ok(manage.note_created);
  if (m(/^\/api\/lessons\/[^/]+\/bookmarks\/[^/]+$/)) return [method === 'DELETE' ? 204 : 201, null];
  if (m(/^\/api\/lessons\/[^/]+$/)) return method === 'DELETE' ? ok({}) : ok(fixtures.list.lessons[0]);
  if (m(/^\/api\/compiles\/[^/]+\/cancel$/)) return ok({});
  if (m(/^\/api\/materials\/[^/]+\/restore$/)) return ok(manage.material_restored);
  if (m(/^\/api\/materials\/(?!uploads)[^/]+$/)) return method === 'DELETE' ? ok({ deleted_at: '2026-11-24T12:00:00Z' }) : ok({});
  if (m(/^\/api\/auth\/credentials\/[^/]+$/)) return ok(method === 'DELETE' ? { revoked_sessions: 2 } : {});
  if (m(/^\/api\/auth\/sessions\/revoke-others$/)) return ok({ revoked: 2 });
  if (m(/^\/api\/auth\/sessions\/[^/]+$/)) return [204, null];
  if (m(/^\/api\/auth\/users\/[^/]+\/sessions\/revoke$/)) return ok({ revoked: 1 });
  if (m(/^\/api\/auth\/users\/[^/]+(\/credentials\/[^/]+)?$/)) return [204, null];
  if (m(/^\/api\/auth\/signups\/[^/]+\/enable$/)) return ok({ status: 'active' });
  if (m(/^\/api\/settings\/[^/]+$/)) return ok(manage.settings);
  // WEB-MANAGE FR-MANAGE-7 "Add a passkey" (SRV-AUTH FR-AUTH-6): options from VEC-MANAGE-1 (rp id `localhost`);
  // verify accepts a registration whose client data is a `webauthn.create` over that challenge, else
  // `register_failed`. No attestation is checked: the stub pins the ceremony's shape, the server pins the crypto.
  if (method === 'POST' && path === '/api/auth/register/options') return ok(manage.register_options);
  if (method === 'POST' && path === '/api/auth/register/verify') return registerVerify(body);
  return null;
};

const registerVerify = (body) => {
  try {
    const client = JSON.parse(Buffer.from(String(body?.response?.clientDataJSON ?? ''), 'base64url').toString('utf8'));
    const shaped = body.type === 'public-key' && typeof body.id === 'string' && typeof body.response.attestationObject === 'string';
    if (shaped && client.type === 'webauthn.create' && client.challenge === manage.register_options.challenge) return [200, { ok: true }];
  } catch {
    // falls through to the refusal
  }
  return [400, { error: 'register_failed' }];
};

const file = (root, path) => {
  const target = normalize(join(root, decodeURIComponent(path)));
  if (!target.startsWith(root)) return null;
  try {
    return statSync(target).isFile() ? target : null;
  } catch {
    return null;
  }
};

export const apiRequests = [];

// SRV-AUTH FR-AUTH-21 (paired WEB-UI change): exactly SRV-AUTH's static auth files, under the same CSP and with the
// api's content types; FR-UI-14 adds the sign-up page (five entries). No other path is served from that directory.
// The sign-in page is served as the api serves it while sign-up is open (FR-AUTH-31): its marker replaced by the
// fixed link fragment.
const AUTH_STATIC = join(repo, 'server/src/reader/auth/static');
const AUTH_FILES = {
  '/api/auth/signin': ['signin.html', 'text/html; charset=utf-8'],
  '/api/auth/bootstrap': ['bootstrap.html', 'text/html; charset=utf-8'],
  '/api/auth/signup': ['signup.html', 'text/html; charset=utf-8'],
  '/api/auth/static/passkey.js': ['passkey.js', 'text/javascript; charset=utf-8'],
  '/api/auth/static/auth.css': ['auth.css', 'text/css; charset=utf-8'],
};
const SIGNIN_MARKER = '<!--signup-link-->';
const authFile = (name) => {
  const bytes = readFileSync(join(AUTH_STATIC, name));
  if (name !== 'signin.html') return bytes;
  const fragment = readFileSync(join(AUTH_STATIC, 'signup-link.html'), 'utf8').trim();
  return Buffer.from(bytes.toString('utf8').replace(SIGNIN_MARKER, fragment), 'utf8');
};

createServer((req, res) => {
  const url = new URL(req.url ?? '/', 'http://127.0.0.1');
  const path = url.pathname;
  if (req.method === 'GET' && Object.hasOwn(AUTH_FILES, path)) {
    const [name, type] = AUTH_FILES[path];
    res.writeHead(200, { 'Content-Type': type, 'Content-Security-Policy': CSP, 'Cache-Control': 'no-cache' });
    res.end(authFile(name));
    return;
  }
  if (req.method === 'POST' && path === '/__test__/reset') {
    resetCompile(url.searchParams.get('w02'));
    session.role = url.searchParams.get('role') === 'learner' ? 'learner' : 'owner';
    res.writeHead(204).end();
    return;
  }
  if (path.startsWith('/api/')) {
    apiRequests.push(`${req.method} ${path}`);
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      let parsed = {};
      try {
        parsed = chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : {};
      } catch {
        parsed = {};
      }
      const firstRun = (req.headers.cookie ?? '').includes('stub-first-run=1');
      const [status, body] =
        manageRoute(req.method, path, url.searchParams, parsed) ??
        uploadRoute(req.method, path, firstRun) ??
        compileRoute(req.method, path) ??
        planRoute(req.method, path, parsed) ??
        api(req.method, path);
      res.writeHead(status, body === null ? {} : { 'Content-Type': 'application/json' });
      res.end(body === null ? undefined : JSON.stringify(body));
    });
    return;
  }
  if (path.startsWith('/storybook/')) {
    const root = join(web, 'storybook-static');
    const f = file(root, path.slice('/storybook'.length) === '/' ? '/index.html' : path.slice('/storybook'.length));
    if (!f) {
      res.writeHead(404).end();
      return;
    }
    res.writeHead(200, { 'Content-Type': TYPES[extname(f)] ?? 'application/octet-stream' });
    res.end(readFileSync(f));
    return;
  }
  const root = join(web, 'dist');
  const f = file(root, path) ?? join(root, 'index.html');
  res.writeHead(200, { 'Content-Type': TYPES[extname(f)] ?? 'application/octet-stream', 'Content-Security-Policy': CSP });
  res.end(readFileSync(f));
}).listen(port, '127.0.0.1', () => console.log(`test server on http://127.0.0.1:${port} (CSP: ${CSP})`));
