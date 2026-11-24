// @trace FR-AUTH-31 FR-AUTH-32 FR-AUTH-14 FR-AUTH-21 FR-AUTH-47
// [browser] SRV-AUTH's sign-up page, the sign-in page's `pending_approval` state and link, and the sign-in `next`
// allow-list, against WEB-UI's test server (five static auth files under the Caddyfile CSP), in the auth-dark and
// auth-light projects at 1280, 1024 and 390 px. Ceremony POSTs are stubbed with page.route, WebAuthn is replaced in
// the page. Run on request: `cd web && npm run e2e -- auth`.
import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page, type Route } from '@playwright/test';

const WIDTHS = [1280, 1024, 390] as const;
const OPTIONS = {
  challenge: 'AAAA',
  rp: { name: 'reader-ai', id: '127.0.0.1' },
  user: { id: 'AAAA', name: 'lc-1a2b', displayName: 'Анна' },
  pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
};
const CREDENTIAL = { id: 'cred', rawId: 'cred', type: 'public-key', response: { clientDataJSON: 'x' } };
const SECRET_BODY = 'SERVER-SAYS-zq91';
const SECRET_EXCEPTION = 'EXCEPTION-SAYS-zq92';
const json = 'application/json';

type Reply = { status: number; body: unknown; headers?: Record<string, string> };
type Mode = 'ok' | 'pending' | 'NotAllowedError' | 'unsupported';
type Post = { method: string; path: string; contentType: string | undefined; body: unknown };
interface Case {
  name: string;
  handle?: string;
  displayName?: string;
  options?: Reply | 'network';
  verify?: Reply;
  mode: Mode;
  click: boolean;
  state: string;
  message: string | RegExp;
  button: 'enabled' | 'disabled' | 'hidden';
  focus?: 'button' | 'status' | 'handle' | 'display-name';
  invalid?: 'handle' | 'display-name';
  posts: Post[];
  webauthn: boolean;
}

const OPT = (handle = 'lc-1a2b', display_name = 'Анна'): Post => ({ method: 'POST', path: '/api/auth/signup/options', contentType: json, body: { handle, display_name } });
const VER: Post = { method: 'POST', path: '/api/auth/signup/verify', contentType: json, body: CREDENTIAL };
const ok = (body: unknown = OPTIONS): Reply => ({ status: 200, body });

const CASES: Case[] = [
  { name: 'ready', mode: 'ok', click: false, state: 'ready', message: '', button: 'enabled', posts: [], webauthn: false },
  { name: 'handle invalid (page check)', handle: 'Ab', mode: 'ok', click: true, state: 'handle_invalid', message: 'Use 3–32 lower-case letters, digits and hyphens, starting with a letter.', button: 'enabled', focus: 'handle', invalid: 'handle', posts: [], webauthn: false },
  { name: 'handle with a double hyphen', handle: 'ab--c', mode: 'ok', click: true, state: 'handle_invalid', message: /lower-case letters/, button: 'enabled', focus: 'handle', invalid: 'handle', posts: [], webauthn: false },
  { name: 'handle with a trailing hyphen', handle: 'abc-', mode: 'ok', click: true, state: 'handle_invalid', message: /lower-case letters/, button: 'enabled', focus: 'handle', invalid: 'handle', posts: [], webauthn: false },
  { name: 'display name empty (page check)', displayName: '   ', mode: 'ok', click: true, state: 'display_name_invalid', message: 'Enter a display name of up to 64 characters.', button: 'enabled', focus: 'display-name', invalid: 'display-name', posts: [], webauthn: false },
  { name: 'display name refused by the server', options: { status: 400, body: { error: 'display_name_invalid' } }, mode: 'ok', click: true, state: 'display_name_invalid', message: 'Enter a display name of up to 64 characters.', button: 'enabled', focus: 'display-name', invalid: 'display-name', posts: [OPT()], webauthn: false },
  { name: 'handle unavailable on options', options: { status: 409, body: { error: 'handle_unavailable' } }, mode: 'ok', click: true, state: 'handle_unavailable', message: 'This handle is not available. Choose another.', button: 'enabled', focus: 'handle', invalid: 'handle', posts: [OPT()], webauthn: false },
  { name: 'handle unavailable on verify', options: ok(), verify: { status: 409, body: { error: 'handle_unavailable' } }, mode: 'ok', click: true, state: 'handle_unavailable', message: 'This handle is not available. Choose another.', button: 'enabled', focus: 'handle', invalid: 'handle', posts: [OPT(), VER], webauthn: true },
  { name: 'sign-ups full', options: { status: 429, body: { error: 'signup_full' } }, mode: 'ok', click: true, state: 'signup_full', message: 'Sign-ups are paused until the owner reviews the waiting requests. Try again later.', button: 'hidden', focus: 'status', posts: [OPT()], webauthn: false },
  { name: 'sign-ups full on verify', options: ok(), verify: { status: 429, body: { error: 'signup_full' } }, mode: 'ok', click: true, state: 'signup_full', message: /Sign-ups are paused/, button: 'hidden', focus: 'status', posts: [OPT(), VER], webauthn: true },
  { name: 'rate limited', options: { status: 429, body: { error: 'rate_limited' }, headers: { 'Retry-After': '120' } }, mode: 'ok', click: true, state: 'rate_limited', message: 'Too many attempts. Try again in 2 minutes.', button: 'enabled', focus: 'button', posts: [OPT()], webauthn: false },
  { name: 'not registered', options: ok(), verify: { status: 400, body: { error: 'signup_failed' } }, mode: 'ok', click: true, state: 'not_registered', message: 'That passkey could not be registered. Try again.', button: 'enabled', focus: 'button', posts: [OPT(), VER], webauthn: true },
  { name: 'cancelled', options: ok(), mode: 'NotAllowedError', click: true, state: 'cancelled', message: /cancelled or timed out/, button: 'enabled', focus: 'button', posts: [OPT()], webauthn: true },
  { name: 'unreachable', options: { status: 500, body: { error: SECRET_BODY } }, mode: 'ok', click: true, state: 'unreachable', message: /could not be reached/, button: 'enabled', focus: 'button', posts: [OPT()], webauthn: false },
  { name: 'network', options: 'network', mode: 'ok', click: true, state: 'unreachable', message: /could not be reached/, button: 'enabled', focus: 'button', posts: [OPT()], webauthn: false },
  { name: 'failed', options: { status: 403, body: { error: SECRET_BODY } }, mode: 'ok', click: true, state: 'failed', message: 'Something went wrong. Try again.', button: 'enabled', focus: 'button', posts: [OPT()], webauthn: false },
  { name: 'waiting', options: ok(), mode: 'pending', click: true, state: 'waiting', message: 'Waiting for your passkey…', button: 'disabled', posts: [OPT()], webauthn: true },
  { name: 'pending', options: ok(), verify: { status: 202, body: { status: 'pending' } }, mode: 'ok', click: true, state: 'pending', message: 'Account created. The owner must approve it before you can sign in.', button: 'hidden', focus: 'status', posts: [OPT(), VER], webauthn: true },
  { name: 'unsupported', mode: 'unsupported', click: false, state: 'unsupported', message: /cannot use passkeys here/, button: 'hidden', posts: [], webauthn: false },
];

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
  const credential = { toJSON: () => ({ id: 'cred', rawId: 'cred', type: 'public-key', response: { clientDataJSON: 'x' } }) };
  const call = (arg: { publicKey: unknown }) => {
    w.__publicKey.push(arg.publicKey);
    if (mode === 'pending') return new Promise(() => undefined);
    if (mode === 'ok') return Promise.resolve(credential);
    return Promise.reject(new DOMException(exception, mode));
  };
  Object.defineProperty(navigator, 'credentials', { value: { create: call, get: call }, configurable: true });
};

const record = (page: Page) => {
  const posts: Post[] = [];
  const all: string[] = [];
  page.on('request', (r) => {
    const path = new URL(r.url()).pathname;
    all.push(`${r.method()} ${path}`);
    if (r.method() !== 'GET') posts.push({ method: r.method(), path, contentType: r.headers()['content-type'], body: r.postDataJSON() });
  });
  return { posts, all };
};

const reply = (r: Reply | 'network' | undefined) => async (route: Route) => {
  if (r === 'network') return route.abort('failed');
  const x = r ?? { status: 404, body: { error: 'not_found' } };
  return route.fulfill({ status: x.status, contentType: json, headers: x.headers ?? {}, body: JSON.stringify(x.body) });
};

const cspWatch = async (page: Page) =>
  page.addInitScript(() => {
    (window as unknown as { __csp: string[] }).__csp = [];
    document.addEventListener('securitypolicyviolation', (e) => (window as unknown as { __csp: string[] }).__csp.push(e.violatedDirective));
  });

const common = async (page: Page) => {
  const axe = await new AxeBuilder({ page }).analyze();
  expect(axe.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical').map((v) => v.id)).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
  expect(await page.evaluate(() => (window as unknown as { __csp: string[] }).__csp)).toEqual([]);
};

for (const width of WIDTHS) {
  for (const c of CASES) {
    test(`sign-up ${c.name} at ${width} px (FR-AUTH-31, FR-AUTH-32)`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      const { posts, all } = record(page);
      await page.addInitScript(stubWebAuthn, { mode: c.mode, exception: SECRET_EXCEPTION });
      await cspWatch(page);
      await page.route('**/api/auth/signup/options', reply(c.options));
      await page.route('**/api/auth/signup/verify', reply(c.verify));
      await page.goto('/api/auth/signup');
      const button = page.locator('#go');
      const status = page.locator('#status');
      if (c.state !== 'unsupported') {
        await page.locator('#handle').fill(c.handle ?? 'lc-1a2b');
        await page.locator('#display-name').fill(c.displayName ?? 'Анна');
      }
      if (c.click) await button.click();
      await expect(page.locator('main.auth')).toHaveAttribute('data-state', c.state);
      if (c.message === '') await expect(status).toHaveText('');
      else await expect(status).toHaveText(c.message);
      if (c.button === 'hidden') await expect(button).toBeHidden();
      else if (c.button === 'disabled') await expect(button).toBeDisabled();
      else await expect(button).toBeEnabled();
      if (c.state === 'waiting') {
        await expect(page.locator('#handle')).toBeDisabled();
        await expect(page.locator('#display-name')).toBeDisabled();
      }
      if (c.focus === 'button') await expect(button).toBeFocused();
      if (c.focus === 'status') await expect(status).toBeFocused();
      if (c.focus === 'handle') await expect(page.locator('#handle')).toBeFocused();
      if (c.focus === 'display-name') await expect(page.locator('#display-name')).toBeFocused();
      for (const field of ['handle', 'display-name'] as const) {
        const el = page.locator(`#${field}`);
        if (c.invalid === field) {
          await expect(el).toHaveAttribute('aria-invalid', 'true');
          await expect(el).toHaveAttribute('aria-describedby', 'status');
        } else await expect(el).not.toHaveAttribute('aria-invalid', 'true');
      }
      await expect(status).not.toContainText(SECRET_BODY);
      await expect(status).not.toContainText(SECRET_EXCEPTION);
      expect(page.url()).toMatch(/\/api\/auth\/signup$/); // `pending` does not navigate
      await common(page);
      expect(posts).toEqual(c.posts);
      const statics = ['GET /api/auth/signup', 'GET /api/auth/static/auth.css', 'GET /api/auth/static/passkey.js'];
      expect([...all.slice(0, 3)].sort()).toEqual([...statics].sort());
      expect(all.slice(3)).toEqual(c.posts.map((p) => `${p.method} ${p.path}`));
      expect(await page.evaluate(() => (window as unknown as { __publicKey: unknown[] }).__publicKey)).toEqual(c.webauthn ? [OPTIONS] : []);
    });
  }

  test(`sign-up without its script stays in loading at ${width} px (FR-AUTH-31)`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.route('**/api/auth/static/passkey.js', (route) => route.abort('failed'));
    await page.goto('/api/auth/signup');
    await expect(page.locator('main.auth')).toHaveAttribute('data-state', 'loading');
    await expect(page.locator('#go')).toBeHidden();
    await expect(page.locator('#status')).toHaveText('Preparing… If nothing happens, reload the page.');
  });

  test(`sign-in pending_approval and the sign-up link at ${width} px (FR-AUTH-31)`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const { posts } = record(page);
    await page.addInitScript(stubWebAuthn, { mode: 'ok', exception: 'x' });
    await cspWatch(page);
    await page.route('**/api/auth/signin/options', reply(ok()));
    await page.route('**/api/auth/signin/verify', reply({ status: 403, body: { error: 'pending_approval' } }));
    await page.goto('/api/auth/signin');
    const link = page.getByRole('link', { name: 'New here? Create an account' });
    await expect(link).toHaveCount(1);
    await expect(link).toHaveAttribute('href', '/api/auth/signup');
    await page.locator('#go').click();
    await expect(page.locator('main.auth')).toHaveAttribute('data-state', 'pending_approval');
    await expect(page.locator('#status')).toHaveText("This account is waiting for the owner's approval. Try again later.");
    await expect(page.locator('#go')).toBeEnabled();
    await expect(page.locator('#go')).toBeFocused();
    await common(page);
    expect(posts.map((p) => p.path)).toEqual(['/api/auth/signin/options', '/api/auth/signin/verify']);
  });
}

// FR-AUTH-14 / FR-AUTH-21 amended: the held `done` navigation's target.
const NEXT_CASES: Array<[string, string, string]> = [
  ['signin', '?next=/signups', '/signups'],
  ['signin', '', '/'],
  ['signin', '?next=https://example.com', '/'],
  ['signin', '?next=//example.com', '/'],
  ['signin', '?next=%2Fsignups', '/'],
  ['signin', '?next=/signups/x', '/'],
  ['signin', '?next=/signups&next=/x', '/'],
  ['signin', '?x=1&next=/signups', '/'],
  ['bootstrap', '?next=/signups', '/'],
  // FR-AUTH-47 (V/manage): the second constant, by the same raw comparison.
  ['signin', '?next=/settings', '/settings'],
  ['signin', '?next=/settings/x', '/'],
  ['signin', '?next=%2Fsettings', '/'],
  ['signin', '?x=1&next=/settings', '/'],
  ['bootstrap', '?next=/settings', '/'],
];

for (const [kind, search, expected] of NEXT_CASES) {
  test(`${kind}${search} navigates to ${expected} after done (FR-AUTH-31)`, async ({ page }) => {
    await page.addInitScript(stubWebAuthn, { mode: 'ok', exception: 'x' });
    await page.route(`**/api/auth/${kind}/options`, reply(ok()));
    await page.route(`**/api/auth/${kind}/verify`, reply(ok({ status: 'ok' })));
    const targets: string[] = [];
    await page.route(
      (url) => url.pathname !== `/api/auth/${kind}` && !url.pathname.startsWith('/api/'),
      async (route) => {
        targets.push(new URL(route.request().url()).pathname + new URL(route.request().url()).search);
        await route.fulfill({ status: 200, contentType: 'text/plain', body: 'landed' });
      },
    );
    await page.goto(`/api/auth/${kind}${search}${kind === 'bootstrap' ? '#tok3n' : ''}`);
    await page.locator('#go').click();
    await expect.poll(() => targets).toEqual([expected]);
  });
}
