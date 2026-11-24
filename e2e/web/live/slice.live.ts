// Live browser checks of the slice (stage 1 plus V.WEB-min, V.SRV-LESSON-min, V.KERNEL-min) against the running stack.
// Covers, by spec item: SRV-AUTH FR-AUTH-4..8, FR-AUTH-12, INV-AUTH-3; WEB-UI FR-UI-5/6, INV-UI-4; WEB-NOTEBOOK
// FR-NOTEBOOK-1..7; WEB-RENDER FR-RENDER-2..6, INV-RENDER-2; SRV-KERNEL FR-KERNEL-7/10; OPS-SANDBOX INV-SANDBOX-1/2/3.
// Outside the spec gate (e2e/live): these are evidence for the live checks, not a module's Red tests.
//
// One serial session: register (no trace), then everything else under a context trace written at the end.
// Retries: none. Every wait is bounded by Playwright's expect/action timeouts or an explicit deadline.
import AxeBuilder from '@axe-core/playwright';
import { expect, test, type BrowserContext, type CDPSession, type Page } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { request as httpsRequest } from 'node:https';
import { resolve } from 'node:path';

const OFFLINE = process.env.READER_LIVE_OFFLINE === '1';
const OUT = resolve(process.env.READER_LIVE_OUT ?? 'out');
const SHOTS = resolve(OUT, 'screenshots');
mkdirSync(SHOTS, { recursive: true });

// Fixture expectations: F01 (SRV-LESSON, loaded for live-check by web_live.py) live, W01 (WEB-NOTEBOOK) offline.
const LESSON = OFFLINE
  ? { course: 'Fixture course', title: 'Fixture lesson W01: one cell of every kind', path: '/lessons/w01', heading: 'Digests', reference: null }
  : { course: 'Fixture course (synthetic)', title: 'F01 — fixture lesson (synthetic)', path: null, heading: 'Catching up', reference: 'Synthetic material F01, p. 1' };

const stackOnly = () => test.skip(OFFLINE, 'needs the running stack');

let context: BrowserContext;
let page: Page;
let cdp: CDPSession | undefined;
let authenticatorId = '';
const csp: string[] = [];
const a11y: string[] = [];

const shot = async (name: string) => page.screenshot({ path: resolve(SHOTS, `${name}.png`), fullPage: true });

test.describe.configure({ mode: 'serial' });

test.beforeAll(async ({ browser }) => {
  context = await browser.newContext({
    recordVideo: { dir: resolve(OUT, 'video') },
    ignoreHTTPSErrors: false,
  });
  page = await context.newPage();
  page.on('console', (m) => {
    if (m.type() === 'error' && /Content Security Policy/i.test(m.text())) csp.push(m.text().slice(0, 200));
  });
  await page.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', (e) => {
      console.error(`Content Security Policy violation: ${e.violatedDirective} ${e.blockedURI}`);
    });
  });
  if (!OFFLINE) {
    // A platform passkey like Apple Passwords: discoverable, UV, backup-eligible and backed up (spike passkey-localhost).
    // CDP cannot hold the signature counter at 0 as iCloud does; the server accepts both (SRV-AUTH counter table).
    cdp = await context.newCDPSession(page);
    await cdp.send('WebAuthn.enable', { enableUI: false });
    const added = await cdp.send('WebAuthn.addVirtualAuthenticator', {
      options: {
        protocol: 'ctap2',
        ctap2Version: 'ctap2_1',
        transport: 'internal',
        hasResidentKey: true,
        hasUserVerification: true,
        isUserVerified: true,
        automaticPresenceSimulation: true,
        defaultBackupEligibility: true,
        defaultBackupState: true,
      },
    });
    authenticatorId = added.authenticatorId;
  }
});

test.afterAll(async () => {
  writeFileSync(resolve(OUT, 'a11y.txt'), a11y.length ? `${a11y.join('\n')}\n` : 'no serious or critical issues\n');
  await context.tracing.stop({ path: resolve(OUT, 'trace.zip') }).catch(() => undefined);
  await context.close();
});

test('TLS: the chain verifies against Caddy’s local root', async () => {
  stackOnly();
  const url = new URL('/api/healthz', test.info().project.use.baseURL);
  const status = await new Promise<number>((done, fail) => {
    const req = httpsRequest(url, { method: 'GET', timeout: 15_000 }, (res) => {
      res.resume();
      done(res.statusCode ?? 0);
    });
    req.on('error', fail);
    req.end();
  });
  expect(status).toBe(200);
});

test('passkey registration through the bootstrap page (learner live-check)', async () => {
  stackOnly();
  const token = process.env.READER_BOOTSTRAP_TOKEN ?? '';
  expect(token.length, 'web_live.py hands a bootstrap token').toBeGreaterThan(20);
  // The token goes in through the fragment only, never as a goto URL (call logs and errors would carry it).
  await page.goto('/api/auth/signin');
  await page.evaluate((t) => history.replaceState(null, '', `/api/auth/bootstrap#${t}`), token);
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Register your passkey' })).toBeVisible();
  expect(page.url(), 'passkey.js clears the fragment').not.toContain('#');
  await page.getByRole('button', { name: 'Create your passkey' }).click(); // SRV-AUTH FR-AUTH-19 (was "Register passkey")
  await page.waitForURL((u) => u.pathname === '/', { timeout: 30_000 });
  const { credentials } = await cdp!.send('WebAuthn.getCredentials', { authenticatorId });
  expect(credentials).toHaveLength(1);
  expect(credentials[0]!.isResidentCredential).toBe(true);
  // From here on, a context trace (screenshots and DOM snapshots) is kept; the token is already gone from the page.
  await context.tracing.start({ screenshots: true, snapshots: true });
});

test('/api/me reports the learner; session cookie is __Host-, HttpOnly, Secure, SameSite', async () => {
  stackOnly();
  const me = await page.request.get('/api/me');
  expect(me.status()).toBe(200);
  const body = (await me.json()) as { handle: string; role: string };
  expect(body).toMatchObject({ handle: 'live-check', role: 'learner' });
  const cookies = await context.cookies();
  const session = cookies.filter((c) => c.name.startsWith('__Host-'));
  expect(session.length).toBeGreaterThan(0);
  for (const c of session) {
    expect(c).toMatchObject({ httpOnly: true, secure: true, path: '/' });
    expect(['Strict', 'Lax']).toContain(c.sameSite);
    expect(c.domain.startsWith('.')).toBe(false);
  }
});

test('POST without Origin is refused (403)', async ({ playwright }) => {
  stackOnly();
  const api = await playwright.request.newContext({ baseURL: test.info().project.use.baseURL, ignoreHTTPSErrors: true });
  const res = await api.post('/api/auth/signin/options', { data: {}, headers: { 'Content-Type': 'application/json' } });
  expect(res.status()).toBe(403);
  await api.dispose();
});

test('sign out, then sign in through the real page with the same passkey', async () => {
  stackOnly();
  await page.goto('/');
  // Design pass (WEB-UI FR-UI-5, FR-UI-11): Sign out is an item of the user menu.
  await page.getByRole('button', { name: /^Account menu for / }).click();
  await page.getByRole('menuitem', { name: 'Sign out' }).click();
  await page.waitForURL(/\/api\/auth\/signin$/);
  expect((await page.request.get('/api/me')).status()).toBe(401);
  await page.getByRole('button', { name: 'Sign in with a passkey' }).click();
  await page.waitForURL((u) => u.pathname === '/', { timeout: 30_000 });
  const me = await page.request.get('/api/me');
  expect(me.status()).toBe(200);
  expect(((await me.json()) as { role: string }).role).toBe('learner');
  const { credentials } = await cdp!.send('WebAuthn.getCredentials', { authenticatorId });
  expect(credentials, 'still exactly one passkey').toHaveLength(1);
  await shot('01-signed-in');
});

test('lesson list shows the fixture lesson', async () => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: LESSON.course })).toBeVisible();
  // The design pass's Continue block links a published lesson a second time (WEB-UI FR-UI-6).
  await expect(page.getByRole('link', { name: LESSON.title }).first()).toBeVisible();
  await shot('02-lesson-list');
  const axe = await new AxeBuilder({ page }).analyze();
  for (const v of axe.violations.filter((x) => x.impact === 'serious' || x.impact === 'critical'))
    a11y.push(`list ${v.impact} ${v.id}: ${v.help} (${v.nodes.length})`);
});

test('lesson renders: markdown, references, open finding', async () => {
  await page.getByRole('link', { name: LESSON.title }).first().click();
  if (LESSON.path) await expect(page).toHaveURL(new RegExp(`${LESSON.path}$`));
  await expect(page.getByRole('heading', { name: LESSON.heading })).toBeVisible();
  if (OFFLINE) {
    await expect(page.locator('.katex').first()).toBeVisible();
    await expect(page.locator('pre.shiki').first()).toBeVisible();
  }
  if (LESSON.reference) await expect(page.getByText(LESSON.reference).first()).toBeVisible();
  // The design pass shows an open finding as the open cell's warning callout (WEB-NOTEBOOK FR-NOTEBOOK-3).
  await expect(page.locator('.cell[data-kind="open"] .callout[data-tone="warning"]').first()).toBeVisible();
  await expect(page.getByRole('link', { name: /open finding/ })).toBeVisible();
  await shot('03-lesson');
  const axe = await new AxeBuilder({ page }).analyze();
  for (const v of axe.violations.filter((x) => x.impact === 'serious' || x.impact === 'critical'))
    a11y.push(`lesson ${v.impact} ${v.id}: ${v.help} (${v.nodes.length})`);
});

// --- Notebook (SRV-KERNEL relay, OPS-SANDBOX kernel) ---------------------------------------------------------------
const lab = () => page.locator('.lab').first();
// Design pass (WEB-NOTEBOOK FR-NOTEBOOK-9): the lab is a CodeMirror editor ("Lab 1 code", contenteditable in a shadow
// root that Playwright's locators pierce); the plain text area shows only until it has loaded.
const runCode = async (code: string) => {
  const editor = lab().locator('.cm-content');
  await expect(editor).toBeVisible({ timeout: 30_000 });
  await editor.click();
  await page.keyboard.press('ControlOrMeta+a');
  await page.keyboard.insertText(code);
  await lab().getByRole('button', { name: 'Run', exact: true }).click();
};
const outputs = () => lab().locator('.outputs');
const state = (label: string, timeout = 60_000) => expect(lab().getByText(label, { exact: true })).toBeVisible({ timeout });

test('cell runs: print(1) outputs 1', async () => {
  stackOnly();
  await runCode('print(1)');
  await state('done', 90_000); // first run includes the first-use kernel reset (SRV-KERNEL FR-KERNEL-9)
  await expect(outputs()).toContainText('1');
  await shot('04-cell-output');
});

test('math and code render in kernel outputs (KaTeX, Shiki)', async () => {
  stackOnly();
  await runCode(
    'from IPython.display import Latex, Markdown, display\n' +
      "display(Latex(r'$\\frac{a}{b}$'))\n" +
      "display(Markdown('```python\\nx = 1\\n```'))",
  );
  await state('done');
  await expect(outputs().locator('.katex').first()).toBeVisible();
  await expect(outputs().locator('pre.shiki').first()).toBeVisible();
  await shot('05-rendered-outputs');
});

test('HTML and SVG outputs are shown as text, never parsed', async () => {
  stackOnly();
  await runCode(
    'from IPython.display import HTML, SVG, display\n' +
      'display(HTML(\'<b id="e2e-html">bold</b><img src=x onerror="document.title=1">\'))\n' +
      'display({"image/svg+xml": \'<svg xmlns="http://www.w3.org/2000/svg" id="e2e-svg"><script>document.title=2</script></svg>\'}, raw=True)',
  );
  await state('done');
  await expect(page.locator('#e2e-html')).toHaveCount(0);
  await expect(page.locator('#e2e-svg')).toHaveCount(0);
  await expect(page.locator('img[src^="blob:"]')).toHaveCount(0);
  expect(await page.title()).not.toMatch(/^[12]$/);
});

test('an error ends the cell in error', async () => {
  stackOnly();
  await runCode('raise ValueError("e2e")');
  await state('error');
  await expect(outputs()).toContainText('ValueError');
});

test('interrupt a busy loop', async () => {
  stackOnly();
  await runCode('while True:\n    pass');
  await state('running', 15_000);
  await lab().getByRole('button', { name: 'Interrupt' }).click();
  await state('interrupted', 30_000);
});

test('sandbox: no egress, no secrets, no tokens in the environment', async () => {
  stackOnly();
  await runCode(
    'import os, socket\n' +
      'def probe(host, port):\n' +
      '    try:\n' +
      '        socket.create_connection((host, port), timeout=3).close(); return "open"\n' +
      '    except Exception as e:\n' +
      '        return type(e).__name__\n' +
      'print("egress-1.1.1.1", probe("1.1.1.1", 443))\n' +
      'print("api-8000", probe("api", 8000))\n' +
      'print("secrets-dir", os.path.exists("/run/secrets"))\n' +
      'bad = [k for k in os.environ if any(w in k.upper() for w in ("TOKEN", "SECRET", "PASSWORD", "KEY"))]\n' +
      'print("env-suspects", sorted(bad))',
  );
  await state('done');
  const text = (await outputs().innerText()).replace(/\s+/g, ' ');
  expect(text).not.toMatch(/egress-1\.1\.1\.1 open/);
  expect(text).not.toMatch(/api-8000 open/);
  expect(text).toContain('secrets-dir False');
  expect(text).toContain('env-suspects []');
});

test('reset with confirmation, then the cell runs again', async () => {
  stackOnly();
  // The design pass moved Reset kernel into the toolbar's "More lesson actions" menu (WEB-NOTEBOOK FR-NOTEBOOK-7).
  const more = page.getByRole('button', { name: 'More lesson actions' });
  await more.click();
  await page.getByRole('menuitem', { name: /Reset kernel/ }).click();
  await expect(page.getByRole('dialog')).toContainText('Reset the kernel?');
  await page.getByRole('button', { name: 'Reset', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeHidden({ timeout: 60_000 });
  await more.click();
  await expect(page.getByRole('menuitem', { name: /Reset kernel/ })).toBeEnabled({ timeout: 60_000 });
  await page.keyboard.press('Escape');
  await runCode('print(2)');
  await state('done', 90_000);
  await expect(outputs()).toContainText('2');
});

test('Run all runs the lesson’s labs from their original source', async () => {
  stackOnly();
  await page.getByRole('button', { name: 'Run all' }).click();
  await state('done', 90_000);
  // F01's lab prints attacker_success(0.1, 5) and (0.3, 5), rounded to 7 places (Nakamoto 2008, section 11).
  // F01 has one lab, so "Run all stops on the first error" is covered by the WEB-NOTEBOOK vectors, not here.
  await expect(outputs()).toContainText('0.0009137 0.1773523');
});

test('no Content-Security-Policy violation during the whole session', async () => {
  expect(csp).toEqual([]);
});
