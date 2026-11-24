// @trace FR-MANAGE-7
// [browser] WEB-MANAGE FR-MANAGE-7 "Add a passkey": SRV-AUTH FR-AUTH-6's register ceremony run in the Account tab
// under the production CSP, with a CDP virtual authenticator (a platform passkey: ctap2, internal, user
// verification) against the WEB-UI test server's register stubs. Run on request:
// `cd web && npm run build && npm run build-storybook && npm run e2e -- manage/passkey`.
// The page is reached as localhost, not 127.0.0.1: a WebAuthn rp id cannot be an IP address (VEC-MANAGE-1's
// `register_options` name rp id `localhost`).
import { expect, test, type CDPSession, type Page } from '@playwright/test';

const ORIGIN = 'http://localhost:4173';

const authenticator = async (page: Page): Promise<{ cdp: CDPSession; authenticatorId: string }> => {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('WebAuthn.enable', { enableUI: false });
  const { authenticatorId } = await cdp.send('WebAuthn.addVirtualAuthenticator', {
    options: { protocol: 'ctap2', transport: 'internal', hasResidentKey: true, hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true },
  });
  return { cdp, authenticatorId };
};

const registerCalls = (page: Page) => {
  const out: Array<{ path: string; body: Record<string, unknown> }> = [];
  page.on('request', (r) => {
    const url = new URL(r.url());
    if (url.pathname.startsWith('/api/auth/register/')) out.push({ path: url.pathname, body: JSON.parse(r.postData() ?? '{}') as Record<string, unknown> });
  });
  return out;
};

const passkeys = async (page: Page) => {
  await page.goto(`${ORIGIN}/settings`);
  await page.getByRole('tab', { name: 'Account' }).click();
  const region = page.getByRole('region', { name: 'Passkeys' });
  await expect(region.getByRole('listitem')).toHaveCount(3);
  return region;
};

test.beforeEach(async ({ page }) => {
  await page.request.post('/__test__/reset');
});

test('adds a passkey from the Account tab: options, create, verify with the credential JSON', async ({ page }) => {
  const { cdp, authenticatorId } = await authenticator(page);
  const calls = registerCalls(page);
  const violations: string[] = [];
  page.on('console', (m) => {
    if (/Content Security Policy/i.test(m.text())) violations.push(m.text());
  });
  const region = await passkeys(page);
  await expect(region.getByText(/sign in on the new device/)).toHaveCount(0);
  const verified = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/auth/register/verify');
  await region.getByRole('button', { name: 'Add a passkey' }).click();
  expect((await verified).status()).toBe(200);
  await expect(region.getByText('Passkey added.')).toBeVisible();

  const { credentials } = await cdp.send('WebAuthn.getCredentials', { authenticatorId });
  expect(credentials).toHaveLength(1);
  expect(credentials[0]!.rpId).toBe('localhost');
  expect(calls.map((c) => c.path)).toEqual(['/api/auth/register/options', '/api/auth/register/verify']);
  expect(calls[0]!.body).toEqual({});
  const body = calls[1]!.body as { id: string; type: string; response: { clientDataJSON: string; attestationObject: string } };
  expect(body.type).toBe('public-key');
  expect(body.id).toBe(Buffer.from(credentials[0]!.credentialId, 'base64').toString('base64url'));
  const client = JSON.parse(Buffer.from(body.response.clientDataJSON, 'base64url').toString('utf8')) as { type: string; origin: string };
  expect(client).toMatchObject({ type: 'webauthn.create', origin: ORIGIN });
  expect(violations).toEqual([]);
});

test('a refused user verification reads as a calm cancel and sends no verify', async ({ page }) => {
  const { cdp, authenticatorId } = await authenticator(page);
  await cdp.send('WebAuthn.setUserVerified', { authenticatorId, isUserVerified: false });
  const calls = registerCalls(page);
  const region = await passkeys(page);
  await region.getByRole('button', { name: 'Add a passkey' }).click();
  await expect(region.getByText('The passkey request was cancelled or timed out. Nothing was added.')).toBeVisible();
  await expect(region.getByRole('alert')).toHaveCount(0);
  expect(calls.map((c) => c.path)).toEqual(['/api/auth/register/options']);
  const { credentials } = await cdp.send('WebAuthn.getCredentials', { authenticatorId });
  expect(credentials).toHaveLength(0);
});
