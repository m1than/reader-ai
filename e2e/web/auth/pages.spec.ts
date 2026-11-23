// @trace FR-AUTH-14 FR-AUTH-19 FR-AUTH-20 FR-AUTH-21
// [browser] SRV-AUTH's two pages against WEB-UI's test server (the four static auth files under the Caddyfile CSP).
// Run on request: `cd web && npm run e2e -- auth`. The ceremony POSTs are stubbed with page.route and the WebAuthn
// calls replaced in the page, so every FR-AUTH-20 state is reachable without an authenticator; the real flow is the
// [live] checks of FR-AUTH-21 (slice.live.ts and the `auth` step of owner.slice.ts).
import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page, type Route } from '@playwright/test';

const WIDTHS = [1280, 1024, 390] as const;
const OPTIONS = {
  challenge: 'AAAA',
  rp: { name: 'reader-ai', id: '127.0.0.1' },
  user: { id: 'AAAA', name: 'owner', displayName: 'owner' },
  pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
};
const CREDENTIAL = { id: 'cred', rawId: 'cred', type: 'public-key', response: { clientDataJSON: 'x' } };
const STATIC_GETS = new Set(['/api/auth/signin', '/api/auth/bootstrap', '/api/auth/static/passkey.js', '/api/auth/static/auth.css']);
const SECRET_BODY = 'SERVER-SAYS-zq81';
const SECRET_EXCEPTION = 'EXCEPTION-SAYS-zq82';

type Reply = { status: number; body: unknown; headers?: Record<string, string> };
type Mode = 'ok' | 'pending' | 'NotAllowedError' | 'AbortError' | 'InvalidStateError' | 'unsupported' | 'noparse' | 'noparse-create';
type Post = { method: string; path: string; contentType: string | undefined; body: unknown };
interface Case {
  name: string;
  page: 'signin' | 'bootstrap';
  hash?: string;
  options?: Reply | 'network';
  verify?: Reply;
  mode: Mode;
  exception?: string;
  click: boolean;
  state: string;
  message: string | RegExp;
  button: 'enabled' | 'disabled' | 'hidden';
  /** Where focus is after the ceremony: the button (retryable) or the status line (button hidden). */
  focus?: 'button' | 'status';
  /** The exact non-GET requests, in order (FR-AUTH-14's sequence cut where the case stops). */
  posts: Post[];
  /** WebAuthn was called once, with the server's options. */
  webauthn: boolean;
}

const json = 'application/json';
const SIGNIN_OPTIONS: Post = { method: 'POST', path: '/api/auth/signin/options', contentType: json, body: {} };
const SIGNIN_VERIFY: Post = { method: 'POST', path: '/api/auth/signin/verify', contentType: json, body: CREDENTIAL };
const BOOT_OPTIONS: Post = { method: 'POST', path: '/api/auth/bootstrap/options', contentType: json, body: { token: 'tok3n' } };
const BOOT_VERIFY: Post = { method: 'POST', path: '/api/auth/bootstrap/verify', contentType: json, body: CREDENTIAL };
const ok = (body: unknown = OPTIONS): Reply => ({ status: 200, body });

const CASES: Case[] = [
  { name: 'sign-in ready', page: 'signin', mode: 'ok', click: false, state: 'ready', message: '', button: 'enabled', posts: [], webauthn: false },
  { name: 'sign-in waiting', page: 'signin', options: ok(), mode: 'pending', click: true, state: 'waiting', message: 'Waiting for your passkey…', button: 'disabled', posts: [SIGNIN_OPTIONS], webauthn: true },
  { name: 'sign-in cancelled', page: 'signin', options: ok(), mode: 'NotAllowedError', click: true, state: 'cancelled', message: 'The passkey request was cancelled or timed out. Try again when you are ready.', button: 'enabled', focus: 'button', posts: [SIGNIN_OPTIONS], webauthn: true },
  { name: 'sign-in aborted', page: 'signin', options: ok(), mode: 'AbortError', click: true, state: 'cancelled', message: /cancelled or timed out/, button: 'enabled', focus: 'button', posts: [SIGNIN_OPTIONS], webauthn: true },
  { name: 'sign-in rejected', page: 'signin', options: ok(), verify: { status: 400, body: { error: 'signin_failed' } }, mode: 'ok', click: true, state: 'rejected', message: 'That passkey was not accepted here. Try again, or use another passkey saved for this site.', button: 'enabled', focus: 'button', posts: [SIGNIN_OPTIONS, SIGNIN_VERIFY], webauthn: true },
  { name: 'sign-in rate limited', page: 'signin', options: { status: 429, body: { error: 'rate_limited' }, headers: { 'Retry-After': '61' } }, mode: 'ok', click: true, state: 'rate_limited', message: 'Too many attempts. Try again in 2 minutes.', button: 'enabled', focus: 'button', posts: [SIGNIN_OPTIONS], webauthn: false },
  { name: 'sign-in rate limited without header', page: 'signin', options: { status: 429, body: { error: 'rate_limited' } }, mode: 'ok', click: true, state: 'rate_limited', message: 'Too many attempts. Try again later.', button: 'enabled', focus: 'button', posts: [SIGNIN_OPTIONS], webauthn: false },
  { name: 'sign-in server error', page: 'signin', options: { status: 503, body: { error: SECRET_BODY } }, mode: 'ok', click: true, state: 'unreachable', message: 'reader-ai could not be reached. Check that it is running, then try again.', button: 'enabled', focus: 'button', posts: [SIGNIN_OPTIONS], webauthn: false },
  { name: 'sign-in network failure', page: 'signin', options: 'network', mode: 'ok', click: true, state: 'unreachable', message: /could not be reached/, button: 'enabled', focus: 'button', posts: [SIGNIN_OPTIONS], webauthn: false },
  { name: 'sign-in other failure', page: 'signin', options: { status: 403, body: { error: SECRET_BODY } }, mode: 'ok', click: true, state: 'failed', message: 'Something went wrong. Try again.', button: 'enabled', focus: 'button', posts: [SIGNIN_OPTIONS], webauthn: false },
  { name: 'sign-in odd exception', page: 'signin', options: ok(), mode: 'InvalidStateError', exception: SECRET_EXCEPTION, click: true, state: 'failed', message: 'Something went wrong. Try again.', button: 'enabled', focus: 'button', posts: [SIGNIN_OPTIONS], webauthn: true },
  { name: 'sign-in done', page: 'signin', options: ok(), verify: ok({ status: 'ok' }), mode: 'ok', click: true, state: 'done', message: 'Signed in. Opening reader-ai…', button: 'disabled', posts: [SIGNIN_OPTIONS, SIGNIN_VERIFY], webauthn: true },
  { name: 'sign-in unsupported', page: 'signin', mode: 'unsupported', click: false, state: 'unsupported', message: 'This browser cannot use passkeys here. Use a current Safari, Chrome, Edge or Firefox.', button: 'hidden', posts: [], webauthn: false },
  { name: 'sign-in without the parse function', page: 'signin', mode: 'noparse', click: false, state: 'unsupported', message: /cannot use passkeys here/, button: 'hidden', posts: [], webauthn: false },
  { name: 'bootstrap ready', page: 'bootstrap', hash: '#tok3n', mode: 'ok', click: false, state: 'ready', message: '', button: 'enabled', posts: [], webauthn: false },
  { name: 'bootstrap without the parse function', page: 'bootstrap', hash: '#tok3n', mode: 'noparse-create', click: false, state: 'unsupported', message: /cannot use passkeys here/, button: 'hidden', posts: [], webauthn: false },
  { name: 'bootstrap no token', page: 'bootstrap', mode: 'ok', click: false, state: 'no_token', message: 'This setup link is incomplete. Run `reader bootstrap` again for a new link.', button: 'hidden', posts: [], webauthn: false },
  { name: 'bootstrap expired', page: 'bootstrap', hash: '#tok3n', options: { status: 400, body: { error: 'bootstrap_failed' } }, mode: 'ok', click: true, state: 'expired', message: 'This setup link has expired or was already used. Run `reader bootstrap` again for a new link.', button: 'hidden', focus: 'status', posts: [BOOT_OPTIONS], webauthn: false },
  { name: 'bootstrap verify refused', page: 'bootstrap', hash: '#tok3n', options: ok(), verify: { status: 400, body: { error: 'bootstrap_failed' } }, mode: 'ok', click: true, state: 'not_registered', message: 'That passkey could not be registered. Try again; if it keeps failing, run `reader bootstrap` again.', button: 'enabled', focus: 'button', posts: [BOOT_OPTIONS, BOOT_VERIFY], webauthn: true },
  { name: 'bootstrap cancelled', page: 'bootstrap', hash: '#tok3n', options: ok(), mode: 'NotAllowedError', exception: SECRET_EXCEPTION, click: true, state: 'cancelled', message: /cancelled or timed out/, button: 'enabled', focus: 'button', posts: [BOOT_OPTIONS], webauthn: true },
  { name: 'bootstrap invalid state', page: 'bootstrap', hash: '#tok3n', options: ok(), mode: 'InvalidStateError', click: true, state: 'failed', message: 'Something went wrong. Try again.', button: 'enabled', focus: 'button', posts: [BOOT_OPTIONS], webauthn: true },
  { name: 'bootstrap done', page: 'bootstrap', hash: '#tok3n', options: ok(), verify: ok({ status: 'ok' }), mode: 'ok', click: true, state: 'done', message: 'Passkey created. Opening reader-ai…', button: 'disabled', posts: [BOOT_OPTIONS, BOOT_VERIFY], webauthn: true },
];

/** Replace the WebAuthn calls in the page (the test has no authenticator); record the options they receive. */
const stubWebAuthn = ({ mode, exception }: { mode: Mode; exception: string }) => {
  const w = window as unknown as { PublicKeyCredential?: Record<string, unknown>; __publicKey: unknown[] };
  w.__publicKey = [];
  if (mode === 'unsupported') {
    delete w.PublicKeyCredential;
    return;
  }
  const pkc = w.PublicKeyCredential!;
  pkc.parseCreationOptionsFromJSON = (o: unknown) => o;
  pkc.parseRequestOptionsFromJSON = (o: unknown) => o;
  if (mode === 'noparse') delete pkc.parseRequestOptionsFromJSON;
  if (mode === 'noparse-create') delete pkc.parseCreationOptionsFromJSON;
  const credential = { toJSON: () => ({ id: 'cred', rawId: 'cred', type: 'public-key', response: { clientDataJSON: 'x' } }) };
  const call = (arg: { publicKey: unknown }) => {
    w.__publicKey.push(arg.publicKey);
    if (mode === 'pending') return new Promise(() => undefined);
    if (mode === 'ok') return Promise.resolve(credential);
    return Promise.reject(new DOMException(exception, mode));
  };
  Object.defineProperty(navigator, 'credentials', { value: { create: call, get: call }, configurable: true });
};

const prepare = async (page: Page, c: Case) => {
  const posts: Post[] = [];
  const apiGets: string[] = [];
  const api: string[] = [];
  page.on('request', (r) => {
    const path = new URL(r.url()).pathname;
    if (path.startsWith('/api/')) api.push(`${r.method()} ${path}`);
    if (r.method() === 'GET') {
      if (path.startsWith('/api/')) apiGets.push(path);
      return;
    }
    posts.push({ method: r.method(), path, contentType: r.headers()['content-type'], body: r.postDataJSON() });
  });
  await page.addInitScript(stubWebAuthn, { mode: c.mode, exception: c.exception ?? 'stub' });
  await page.addInitScript(() => {
    (window as unknown as { __csp: string[] }).__csp = [];
    document.addEventListener('securitypolicyviolation', (e) => (window as unknown as { __csp: string[] }).__csp.push(e.violatedDirective));
  });
  const reply = (r: Reply | 'network' | undefined) => async (route: Route) => {
    if (r === 'network') return route.abort('failed');
    const x = r ?? { status: 404, body: { error: 'not_found' } };
    return route.fulfill({ status: x.status, contentType: json, headers: x.headers ?? {}, body: JSON.stringify(x.body) });
  };
  await page.route(`**/api/auth/${c.page}/options`, reply(c.options));
  await page.route(`**/api/auth/${c.page}/verify`, reply(c.verify));
  // A successful ceremony navigates to `/`: the response is held until `release()`. Playwright's locators wait for a
  // pending navigation, so the page reports its `done` snapshot itself, the moment `data-state` becomes `done`
  // (before `location.assign` can unload it), through an exposed function.
  let release: () => void = () => undefined;
  const held = new Promise<void>((done) => (release = done));
  await page.route(
    (url) => url.pathname === '/',
    async (route) => {
      await held;
      await route.fulfill({ status: 200, contentType: 'text/plain', body: 'home' });
    },
  );
  let snapshot: (s: Snapshot) => void = () => undefined;
  const done = new Promise<Snapshot>((resolve) => (snapshot = resolve));
  await page.exposeFunction('__doneSnapshot', (s: Snapshot) => snapshot(s));
  await page.addInitScript(() => {
    document.addEventListener('DOMContentLoaded', () => {
      const main = document.querySelector('main.auth');
      if (!main) return;
      new MutationObserver(() => {
        if (main.getAttribute('data-state') !== 'done') return;
        const button = document.getElementById('go') as HTMLButtonElement;
        const status = document.getElementById('status')!;
        const w = window as unknown as { __doneSnapshot: (s: unknown) => void; __publicKey: unknown[] };
        w.__doneSnapshot({
          state: 'done',
          message: status.textContent,
          disabled: button.disabled,
          hidden: button.hidden,
          statusFocused: document.activeElement === status,
          url: location.href,
          publicKey: w.__publicKey,
        });
      }).observe(main, { attributes: true, attributeFilter: ['data-state'] });
    });
  });
  return { posts, apiGets, api, release: () => release(), done };
};

type Snapshot = {
  state: string;
  message: string;
  disabled: boolean;
  hidden: boolean;
  statusFocused: boolean;
  url: string;
  publicKey: unknown[];
};

for (const width of WIDTHS) {
  for (const c of CASES) {
    test(`${c.name} at ${width} px (FR-AUTH-20, FR-AUTH-21)`, async ({ page }) => {
      await page.setViewportSize({ width, height: 800 });
      const { posts, apiGets, api, release, done } = await prepare(page, c);
      await page.goto(`/api/auth/${c.page}${c.hash ?? ''}`);
      const button = page.locator('#go');
      const status = page.locator('#status');
      const statics = [`GET /api/auth/${c.page}`, 'GET /api/auth/static/auth.css', 'GET /api/auth/static/passkey.js'];
      if (c.state === 'done') {
        // `done`: state, message, disabled button and focus come from the page's own snapshot, taken before the
        // navigation to `/` (held, then released); then the request oracle as for every case.
        await button.click();
        const s = await done;
        expect(s).toMatchObject({ state: 'done', message: c.message, disabled: true, hidden: false, statusFocused: false });
        expect(s.url).not.toContain('#');
        expect(s.publicKey).toEqual([OPTIONS]);
        release();
        await page.waitForURL((u) => u.pathname === '/');
        expect(posts).toEqual(c.posts);
        expect([...api.slice(0, 3)].sort()).toEqual([...statics].sort());
        expect(api.slice(3)).toEqual(c.posts.map((p) => `${p.method} ${p.path}`));
        return;
      }
      if (c.click) await button.click();
      await expect(page.locator('main.auth')).toHaveAttribute('data-state', c.state);
      if (c.message === '') await expect(status).toHaveText('');
      else await expect(status).toHaveText(c.message);
      if (c.button === 'hidden') await expect(button).toBeHidden();
      else if (c.button === 'disabled') await expect(button).toBeDisabled();
      else await expect(button).toBeEnabled();
      if (c.focus === 'button') await expect(button).toBeFocused();
      if (c.focus === 'status') await expect(status).toBeFocused();
      if (c.state === 'waiting' || c.state === 'done') await expect(status).not.toBeFocused();
      expect(page.url(), 'the fragment stays cleared').not.toContain('#');
      await expect(status).not.toContainText(SECRET_BODY);
      await expect(status).not.toContainText(SECRET_EXCEPTION);
      const axe = await new AxeBuilder({ page }).analyze();
      expect(axe.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical').map((v) => v.id)).toEqual([]);
      expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
      expect(await page.evaluate(() => (window as unknown as { __csp: string[] }).__csp)).toEqual([]);
      // FR-AUTH-14: the exact request sequence, and the options handed to WebAuthn unchanged.
      expect(posts).toEqual(c.posts);
      expect(apiGets.filter((p) => !STATIC_GETS.has(p))).toEqual([]);
      // The full /api list: the page's three static GETs (in any order), then exactly the case's POSTs.
      expect([...api.slice(0, 3)].sort()).toEqual([...statics].sort());
      expect(api.slice(3)).toEqual(c.posts.map((p) => `${p.method} ${p.path}`));
      const seen = await page.evaluate(() => (window as unknown as { __publicKey: unknown[] }).__publicKey);
      expect(seen).toEqual(c.webauthn ? [OPTIONS] : []);
    });
  }

  test(`look and focus at ${width} px (FR-AUTH-19)`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    await prepare(page, CASES[0]!);
    await page.goto('/api/auth/signin');
    await expect(page.getByRole('heading', { level: 1, name: 'Sign in' })).toBeVisible();
    await expect(page.locator('.auth-brand')).toHaveText('reader-ai');
    const card = await page.locator('.auth-card').boundingBox();
    expect(card).not.toBeNull();
    expect(card!.width).toBeLessThanOrEqual(26 * 16 + 1);
    expect(card!.x).toBeGreaterThanOrEqual(0);
    expect(Math.abs(card!.x + card!.width / 2 - width / 2)).toBeLessThanOrEqual(1);
    const look = await page.evaluate(() => {
      const b = getComputedStyle(document.getElementById('go')!);
      const h = getComputedStyle(document.querySelector('h1')!);
      return { height: b.height, radius: b.borderTopLeftRadius, h1: h.fontSize, font: h.fontFamily };
    });
    expect(look).toMatchObject({ height: '48px', radius: '12px', h1: '24px' });
    expect(look.font).toMatch(/^["']?Manrope/);
    await page.keyboard.press('Tab');
    await expect(page.locator('#go')).toBeFocused();
    const outline = await page.locator('#go').evaluate((b) => {
      const s = getComputedStyle(b);
      return `${s.outlineWidth} ${s.outlineStyle}`;
    });
    expect(outline).toBe('2px solid');
  });
}
