// @trace FR-AUTH-14 FR-AUTH-19 FR-AUTH-20 FR-AUTH-21
// [browser] SRV-AUTH's two pages against WEB-UI's test server (the four static auth files under the Caddyfile CSP).
// Run on request: `cd web && npm run e2e -- auth`. The ceremony POSTs are stubbed with page.route and the WebAuthn
// calls replaced in the page, so every FR-AUTH-20 state is reachable without an authenticator; the real flow is the
// [live] `auth` step of e2e/web/live/owner.slice.ts.
import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page, type Route } from '@playwright/test';

const WIDTHS = [1280, 1024, 390] as const;
const OPTIONS = {
  challenge: 'AAAA',
  rp: { name: 'reader-ai', id: '127.0.0.1' },
  user: { id: 'AAAA', name: 'owner', displayName: 'owner' },
  pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
  allowCredentials: [],
};
const CREDENTIAL = { id: 'cred', rawId: 'cred', type: 'public-key', response: { clientDataJSON: 'x' } };

type Reply = { status: number; body: unknown; headers?: Record<string, string> };
type Mode = 'ok' | 'pending' | 'NotAllowedError' | 'AbortError' | 'InvalidStateError' | 'unsupported' | null;
interface Case {
  name: string;
  page: 'signin' | 'bootstrap';
  hash?: string;
  options?: Reply | 'network';
  verify?: Reply;
  mode: Mode;
  click: boolean;
  state: string;
  message: string | RegExp;
  button: 'enabled' | 'disabled' | 'hidden';
}

const ok = (body: unknown = OPTIONS): Reply => ({ status: 200, body });
const CASES: Case[] = [
  { name: 'sign-in ready', page: 'signin', mode: 'ok', click: false, state: 'ready', message: '', button: 'enabled' },
  { name: 'sign-in waiting', page: 'signin', options: ok(), mode: 'pending', click: true, state: 'waiting', message: 'Waiting for your passkey…', button: 'disabled' },
  { name: 'sign-in cancelled', page: 'signin', options: ok(), mode: 'NotAllowedError', click: true, state: 'cancelled', message: 'The passkey request was cancelled or timed out. Try again when you are ready.', button: 'enabled' },
  { name: 'sign-in aborted', page: 'signin', options: ok(), mode: 'AbortError', click: true, state: 'cancelled', message: /cancelled or timed out/, button: 'enabled' },
  { name: 'sign-in rejected', page: 'signin', options: ok(), verify: { status: 400, body: { error: 'signin_failed' } }, mode: 'ok', click: true, state: 'rejected', message: 'That passkey was not accepted here. Try again, or use another passkey saved for this site.', button: 'enabled' },
  { name: 'sign-in rate limited', page: 'signin', options: { status: 429, body: { error: 'rate_limited' }, headers: { 'Retry-After': '61' } }, mode: 'ok', click: true, state: 'rate_limited', message: 'Too many attempts. Try again in 2 minutes.', button: 'enabled' },
  { name: 'sign-in rate limited without header', page: 'signin', options: { status: 429, body: { error: 'rate_limited' } }, mode: 'ok', click: true, state: 'rate_limited', message: 'Too many attempts. Try again later.', button: 'enabled' },
  { name: 'sign-in server error', page: 'signin', options: { status: 503, body: { error: 'unavailable' } }, mode: 'ok', click: true, state: 'unreachable', message: 'reader-ai could not be reached. Check that it is running, then try again.', button: 'enabled' },
  { name: 'sign-in network failure', page: 'signin', options: 'network', mode: 'ok', click: true, state: 'unreachable', message: /could not be reached/, button: 'enabled' },
  { name: 'sign-in other failure', page: 'signin', options: { status: 403, body: { error: 'forbidden' } }, mode: 'ok', click: true, state: 'failed', message: 'Something went wrong. Try again.', button: 'enabled' },
  { name: 'sign-in done', page: 'signin', options: ok(), verify: ok({ status: 'ok' }), mode: 'ok', click: true, state: 'done', message: 'Signed in. Opening reader-ai…', button: 'disabled' },
  { name: 'sign-in unsupported', page: 'signin', mode: 'unsupported', click: false, state: 'unsupported', message: 'This browser cannot use passkeys here. Use a current Safari, Chrome, Edge or Firefox.', button: 'hidden' },
  { name: 'bootstrap ready', page: 'bootstrap', hash: '#tok3n', mode: 'ok', click: false, state: 'ready', message: '', button: 'enabled' },
  { name: 'bootstrap no token', page: 'bootstrap', mode: 'ok', click: false, state: 'no_token', message: 'This setup link is incomplete. Run `reader bootstrap` again for a new link.', button: 'hidden' },
  { name: 'bootstrap expired', page: 'bootstrap', hash: '#tok3n', options: { status: 400, body: { error: 'bootstrap_failed' } }, mode: 'ok', click: true, state: 'expired', message: 'This setup link has expired or was already used. Run `reader bootstrap` again for a new link.', button: 'hidden' },
  { name: 'bootstrap known device', page: 'bootstrap', hash: '#tok3n', options: ok(), mode: 'InvalidStateError', click: true, state: 'known', message: /already has a passkey for reader-ai/, button: 'hidden' },
  { name: 'bootstrap cancelled', page: 'bootstrap', hash: '#tok3n', options: ok(), mode: 'NotAllowedError', click: true, state: 'cancelled', message: /cancelled or timed out/, button: 'enabled' },
  { name: 'bootstrap done', page: 'bootstrap', hash: '#tok3n', options: ok(), verify: ok({ status: 'ok' }), mode: 'ok', click: true, state: 'done', message: 'Passkey created. Opening reader-ai…', button: 'disabled' },
];

/** Replace the WebAuthn calls in the page (the test has no authenticator). */
const stubWebAuthn = (mode: Mode) => {
  if (mode === 'unsupported') {
    delete (window as { PublicKeyCredential?: unknown }).PublicKeyCredential;
    return;
  }
  const credential = { toJSON: () => ({ id: 'cred', rawId: 'cred', type: 'public-key', response: { clientDataJSON: 'x' } }) };
  const call = () =>
    mode === 'pending'
      ? new Promise(() => undefined)
      : mode === 'ok'
        ? Promise.resolve(credential)
        : Promise.reject(new DOMException('stub', mode ?? 'NotAllowedError'));
  Object.defineProperty(navigator, 'credentials', { value: { create: call, get: call }, configurable: true });
  const pkc = window.PublicKeyCredential as unknown as Record<string, unknown>;
  pkc.parseCreationOptionsFromJSON = (o: unknown) => o;
  pkc.parseRequestOptionsFromJSON = (o: unknown) => o;
};

const prepare = async (page: Page, c: Case) => {
  const requests: Array<{ method: string; path: string; body: unknown }> = [];
  await page.addInitScript(stubWebAuthn, c.mode);
  await page.addInitScript(() => {
    (window as unknown as { __csp: string[] }).__csp = [];
    document.addEventListener('securitypolicyviolation', (e) => (window as unknown as { __csp: string[] }).__csp.push(e.violatedDirective));
  });
  const reply = (r: Reply | 'network' | undefined) => async (route: Route) => {
    const req = route.request();
    requests.push({ method: req.method(), path: new URL(req.url()).pathname, body: req.postDataJSON() });
    if (r === 'network') return route.abort('failed');
    const x = r ?? { status: 404, body: { error: 'not_found' } };
    return route.fulfill({ status: x.status, contentType: 'application/json', headers: x.headers ?? {}, body: JSON.stringify(x.body) });
  };
  await page.route(`**/api/auth/${c.page}/options`, reply(c.options));
  await page.route(`**/api/auth/${c.page}/verify`, reply(c.verify));
  // A successful ceremony navigates to `/`; answer it here so the page stays inspectable.
  await page.route('**/', (route) =>
    new URL(route.request().url()).pathname === '/' ? route.fulfill({ status: 200, contentType: 'text/plain', body: 'home' }) : route.fallback(),
  );
  return requests;
};

for (const width of WIDTHS) {
  for (const c of CASES) {
    test(`${c.name} at ${width} px (FR-AUTH-20, FR-AUTH-21)`, async ({ page }) => {
      await page.setViewportSize({ width, height: 800 });
      const requests = await prepare(page, c);
      await page.goto(`/api/auth/${c.page}${c.hash ?? ''}`);
      const button = page.locator('#go');
      if (c.click) await button.click();
      if (c.state === 'done') await page.waitForURL('**/');
      if (c.state !== 'done') {
        await expect(page.locator('main.auth')).toHaveAttribute('data-state', c.state);
        if (c.message === '') await expect(page.locator('#status')).toHaveText('');
        else await expect(page.locator('#status')).toHaveText(c.message);
        if (c.button === 'hidden') await expect(button).toBeHidden();
        else if (c.button === 'disabled') await expect(button).toBeDisabled();
        else await expect(button).toBeEnabled();
        const axe = await new AxeBuilder({ page }).analyze();
        expect(axe.violations.filter((v) => v.impact === 'serious' || v.impact === 'critical').map((v) => v.id)).toEqual([]);
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        expect(overflow).toBeLessThanOrEqual(0);
        expect(await page.evaluate(() => (window as unknown as { __csp: string[] }).__csp)).toEqual([]);
      }
      // FR-AUTH-14: the ceremony requests are exactly the unchanged sequence, as far as this case gets.
      const expected =
        c.page === 'signin'
          ? [
              { method: 'POST', path: '/api/auth/signin/options', body: {} },
              { method: 'POST', path: '/api/auth/signin/verify', body: CREDENTIAL },
            ]
          : [
              { method: 'POST', path: '/api/auth/bootstrap/options', body: { token: 'tok3n' } },
              { method: 'POST', path: '/api/auth/bootstrap/verify', body: CREDENTIAL },
            ];
      expect(requests).toEqual(expected.slice(0, requests.length));
      if (c.verify) expect(requests).toHaveLength(2);
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
    expect(card!.x + card!.width).toBeLessThanOrEqual(width);
    // Centred horizontally (± 1 px).
    expect(Math.abs(card!.x + card!.width / 2 - width / 2)).toBeLessThanOrEqual(1);
    const font = await page.locator('h1').evaluate((h) => getComputedStyle(h).fontFamily);
    expect(font.startsWith('Manrope')).toBe(true);
    await page.keyboard.press('Tab');
    await expect(page.locator('#go')).toBeFocused();
    const outline = await page.locator('#go').evaluate((b) => {
      const s = getComputedStyle(b);
      return `${s.outlineWidth} ${s.outlineStyle}`;
    });
    expect(outline).toBe('2px solid');
  });
}

test('the bootstrap fragment is read once and cleared (FR-AUTH-14)', async ({ page }) => {
  await prepare(page, CASES.find((c) => c.name === 'bootstrap ready')!);
  await page.goto('/api/auth/bootstrap#tok3n');
  await expect(page.locator('main.auth')).toHaveAttribute('data-state', 'ready');
  expect(page.url()).not.toContain('#');
});
