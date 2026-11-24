// @trace FR-MANAGE-6 FR-MANAGE-7 FR-MANAGE-8 FR-MANAGE-10 FR-MANAGE-11 INV-MANAGE-1 INV-MANAGE-2 INV-MANAGE-4 VEC-MANAGE-1 VEC-MANAGE-2
// WEB-MANAGE /settings: tabs per role, Appearance (theme, text size, reading width, language), Account (passkeys and
// sessions), People, Instance (read and edit), Data (recently deleted and activity), with the request oracle.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { readJson, webRoot } from '../app/testing/fixtures';
import { load } from '../app/testing/load';
import { fakeServer, fixture, refusal, type FakeServer } from './testkit';

afterEach(() => {
  cleanup();
  for (const k of ['reader-ai.text-size', 'reader-ai.width', 'reader-ai.theme', 'reader-ai.locale']) localStorage.removeItem(k);
  for (const a of ['data-text-size', 'data-width', 'data-theme']) document.documentElement.removeAttribute(a);
});

const mount = async (server: FakeServer, path = '/settings', navigateAway: (url: string) => void = vi.fn()) => {
  const { AppRoot } = await load(() => import('../app/AppRoot'));
  await act(async () => {
    render(<AppRoot initialPath={path} fetch={server.fetch} navigateAway={navigateAway} />);
  });
  await screen.findByRole('heading', { level: 1, name: /Settings|Настройки/ });
};
const tab = async (name: string) => {
  await act(async () => fireEvent.click(await screen.findByRole('tab', { name })));
  return screen.findByRole('tabpanel');
};
const ID = { lena: '1b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b11', boris: '3b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b13', petr: '6b5c1f3e-6d2a-4f7e-9a41-2c8d5e7f9b16' };

describe('the page and its tabs (FR-MANAGE-6, INV-MANAGE-2)', () => {
  it('gives the owner five tabs and a learner two, with no owner route called for a learner', async () => {
    const owner = fakeServer();
    await mount(owner);
    expect(screen.getAllByRole('tab').map((t) => t.textContent)).toEqual(['Appearance', 'Account', 'People', 'Instance', 'Data']);
    cleanup();
    const learner = fakeServer({ role: 'learner' });
    await mount(learner);
    expect(screen.getAllByRole('tab').map((t) => t.textContent)).toEqual(['Appearance', 'Account']);
    await tab('Account');
    await waitFor(() => expect(learner.to('/api/auth/credentials').length).toBe(1));
    expect(learner.to('/api/auth/users', '/api/settings', '/api/audit', '/api/courses', '/api/materials')).toEqual([]);
    expect(learner.writes()).toEqual([]);
  });

  it('is reached from the account menu', async () => {
    const server = fakeServer();
    const { AppRoot } = await load(() => import('../app/AppRoot'));
    await act(async () => {
      render(<AppRoot initialPath="/" fetch={server.fetch} navigateAway={vi.fn()} />);
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Account menu for Ivan' }));
    const link = await screen.findByRole('menuitem', { name: 'Settings' });
    await act(async () => fireEvent.click(link));
    expect(await screen.findByRole('heading', { level: 1, name: 'Settings' })).toBeTruthy();
  });
});

describe('Appearance (FR-MANAGE-6, FR-MANAGE-10)', () => {
  it('writes and applies text size, reading width and theme', async () => {
    await mount(fakeServer());
    const panel = await tab('Appearance');
    const size = within(panel).getByRole('radiogroup', { name: 'Text size' });
    expect(within(size).getByRole('radio', { name: 'Medium' }).getAttribute('aria-checked')).toBe('true');
    fireEvent.click(within(size).getByRole('radio', { name: 'Large' }));
    expect(localStorage.getItem('reader-ai.text-size')).toBe('large');
    expect(document.documentElement.getAttribute('data-text-size')).toBe('large');
    const width = within(panel).getByRole('radiogroup', { name: 'Reading width' });
    fireEvent.click(within(width).getByRole('radio', { name: 'Wide' }));
    expect(localStorage.getItem('reader-ai.width')).toBe('wide');
    expect(document.documentElement.getAttribute('data-width')).toBe('wide');
    const theme = within(panel).getByRole('radiogroup', { name: 'Theme' });
    fireEvent.click(within(theme).getByRole('radio', { name: 'Dark' }));
    expect(localStorage.getItem('reader-ai.theme')).toBe('dark');
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');
  });

  it('falls back to the default for an unknown stored value', async () => {
    localStorage.setItem('reader-ai.text-size', 'gigantic');
    localStorage.setItem('reader-ai.width', '9000');
    await mount(fakeServer());
    const panel = await tab('Appearance');
    expect(within(within(panel).getByRole('radiogroup', { name: 'Text size' })).getByRole('radio', { name: 'Medium' }).getAttribute('aria-checked')).toBe('true');
    expect(within(within(panel).getByRole('radiogroup', { name: 'Reading width' })).getByRole('radio', { name: 'Normal' }).getAttribute('aria-checked')).toBe('true');
  });

  it('switches the language without a reload, keeps focus, and labels each option in its own language', async () => {
    await mount(fakeServer());
    const panel = await tab('Appearance');
    const group = within(panel).getByRole('radiogroup', { name: 'Language' });
    expect(within(group).getByRole('radio', { name: 'Follow the browser' }).getAttribute('aria-checked')).toBe('true');
    expect(within(group).getByRole('radio', { name: 'English' }).closest('[lang]')?.getAttribute('lang')).toBe('en');
    const ru = within(group).getByRole('radio', { name: 'Русский' });
    expect(ru.closest('[lang]')?.getAttribute('lang')).toBe('ru');
    ru.focus();
    await act(async () => fireEvent.click(ru));
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Настройки'));
    expect(localStorage.getItem('reader-ai.locale')).toBe('ru');
    expect(document.documentElement.lang).toBe('ru');
    expect(document.activeElement?.closest('[role="radio"], input')).toBeTruthy();
    const back = screen.getByRole('radio', { name: 'English' });
    await act(async () => fireEvent.click(back));
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Settings'));
    expect(localStorage.getItem('reader-ai.locale')).toBe('en');
  });

  it('theme.js applies text size and width before first paint, with defaults for unknown values', () => {
    const source = readFileSync(`${webRoot}src/public/theme.js`, 'utf8');
    const run = (stored: Record<string, string>) => {
      const attrs = new Map<string, string>();
      const root = { setAttribute: (k: string, v: string) => void attrs.set(k, v), removeAttribute: (k: string) => void attrs.delete(k) };
      runInNewContext(source, { document: { documentElement: root }, localStorage: { getItem: (k: string) => stored[k] ?? null }, navigator: { languages: ['en-US'] } });
      return attrs;
    };
    const a = run({ 'reader-ai.text-size': 'small', 'reader-ai.width': 'narrow' });
    expect(a.get('data-text-size')).toBe('small');
    expect(a.get('data-width')).toBe('narrow');
    const b = run({ 'reader-ai.text-size': 'huge', 'reader-ai.width': 'x' });
    expect(b.get('data-text-size') ?? 'medium').toBe('medium');
    expect(b.get('data-width') ?? 'normal').toBe('normal');
  });
});

describe('Account (FR-MANAGE-7)', () => {
  it('lists passkeys and sessions and sends each own action', async () => {
    const server = fakeServer();
    await mount(server);
    const panel = await tab('Account');
    const passkeys = await within(panel).findByRole('region', { name: 'Passkeys' });
    await waitFor(() => expect(within(passkeys).getAllByRole('listitem')).toHaveLength(3));
    expect(within(passkeys).getAllByText('synced')).toHaveLength(2);
    fireEvent.click(within(passkeys).getAllByRole('button', { name: /^Remove/ })[1]!);
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/Your other sessions are signed out too; this one stays\./)).toBeTruthy();
    expect(within(dialog).getByText(/This cannot be undone\./)).toBeTruthy();
    await act(async () => fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' })));
    const sessions = await within(panel).findByRole('region', { name: 'Sessions' });
    const rows = await waitFor(() => within(sessions).getAllByRole('listitem'));
    expect(within(rows[0]!).getByText('This session')).toBeTruthy();
    await act(async () => fireEvent.click(within(rows[1]!).getByRole('button', { name: 'Sign out' })));
    await act(async () => fireEvent.click(within(sessions).getByRole('button', { name: 'Sign out everywhere else' })));
    await waitFor(() =>
      expect(server.writes()).toEqual([
        { method: 'DELETE', path: '/api/auth/credentials/p-ivan-2', body: {} },
        { method: 'DELETE', path: '/api/auth/sessions/s-other-1', body: {} },
        { method: 'POST', path: '/api/auth/sessions/revoke-others', body: {} },
      ]),
    );
  });

  it('refuses to remove the last passkey and renames a passkey', async () => {
    const fx = fixture();
    const server = fakeServer({ fx, overrides: { 'GET /api/auth/credentials': { status: 200, body: fx.credentials_one } } });
    await mount(server);
    const panel = await tab('Account');
    const passkeys = await within(panel).findByRole('region', { name: 'Passkeys' });
    expect(await within(passkeys).findByText('Add another passkey before removing this one.')).toBeTruthy();
    expect(within(passkeys).queryByRole('button', { name: /^Remove/ })).toBeNull();
    fireEvent.click(within(passkeys).getByRole('button', { name: 'Rename MacBook' }));
    const box = within(passkeys).getByRole('textbox', { name: 'Passkey name' });
    fireEvent.change(box, { target: { value: 'Work laptop' } });
    await act(async () => fireEvent.keyDown(box, { key: 'Enter' }));
    await waitFor(() => expect(server.writes()).toEqual([{ method: 'POST', path: '/api/auth/credentials/p-ivan-1', body: { label: 'Work laptop' } }]));
  });

  it('asks for a recent sign-in when the server answers reauth_required', async () => {
    const server = fakeServer({ overrides: { 'POST /api/auth/sessions/revoke-others': refusal('reauth_required') } });
    const navigateAway = vi.fn();
    await mount(server, '/settings', navigateAway);
    const panel = await tab('Account');
    const sessions = await within(panel).findByRole('region', { name: 'Sessions' });
    await act(async () => fireEvent.click(await within(sessions).findByRole('button', { name: 'Sign out everywhere else' })));
    expect(await screen.findByText('Sign in again to do this (required every 10 minutes for account actions).')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Sign in again' }).getAttribute('href')).toBe('/api/auth/signin?next=/settings');
    // A recent-sign-in refusal stays on the page: the shell's redirect is for `unauthenticated` only.
    expect(navigateAway).not.toHaveBeenCalled();
  });

  it('sends an unauthenticated 401 to the shell’s sign-in redirect, with no reauth line (FR-MANAGE-1)', async () => {
    const server = fakeServer({ overrides: { 'POST /api/auth/sessions/revoke-others': refusal('unauthenticated') } });
    const navigateAway = vi.fn();
    await mount(server, '/settings', navigateAway);
    const panel = await tab('Account');
    const sessions = await within(panel).findByRole('region', { name: 'Sessions' });
    await act(async () => fireEvent.click(await within(sessions).findByRole('button', { name: 'Sign out everywhere else' })));
    await waitFor(() => expect(navigateAway).toHaveBeenCalledWith('/api/auth/signin'));
    expect(screen.queryByRole('link', { name: 'Sign in again' })).toBeNull();
  });
});

describe('Add a passkey (FR-MANAGE-7, SRV-AUTH FR-AUTH-6)', () => {
  const CREDENTIAL_JSON = { id: 'cred-new', rawId: 'cred-new', type: 'public-key', response: { clientDataJSON: 'e30', attestationObject: 'o2N' }, clientExtensionResults: {} };
  const stub = (create: () => Promise<unknown>) => {
    const parse = vi.fn((json: unknown) => ({ parsed: json }));
    vi.stubGlobal('PublicKeyCredential', Object.assign(function PublicKeyCredential() {}, { parseCreationOptionsFromJSON: parse }));
    const createFn = vi.fn(create);
    Object.defineProperty(navigator, 'credentials', { value: { create: createFn }, configurable: true });
    return { parse, create: createFn };
  };
  afterEach(() => {
    vi.unstubAllGlobals();
    delete (navigator as { credentials?: unknown }).credentials;
  });
  const passkeys = async (server: FakeServer, navigateAway?: (url: string) => void) => {
    await mount(server, '/settings', navigateAway);
    const panel = await tab('Account');
    return within(panel).findByRole('region', { name: 'Passkeys' });
  };
  const add = async (region: HTMLElement) => act(async () => fireEvent.click(await within(region).findByRole('button', { name: 'Add a passkey' })));

  it('runs options → create → verify with the credential’s JSON, then refetches the list', async () => {
    const webauthn = stub(async () => ({ toJSON: () => CREDENTIAL_JSON }));
    const server = fakeServer();
    const region = await passkeys(server);
    await waitFor(() => expect(within(region).getAllByRole('listitem')).toHaveLength(3));
    expect(within(region).queryByText(/sign in on the new device/)).toBeNull();
    const before = server.to('/api/auth/credentials').length;
    await add(region);
    expect(await within(region).findByText('Passkey added.')).toBeTruthy();
    expect(server.writes()).toEqual([
      { method: 'POST', path: '/api/auth/register/options', body: {} },
      { method: 'POST', path: '/api/auth/register/verify', body: CREDENTIAL_JSON },
    ]);
    expect(webauthn.parse).toHaveBeenCalledWith(fixture().register_options);
    expect(webauthn.create).toHaveBeenCalledWith({ publicKey: { parsed: fixture().register_options } });
    await waitFor(() => expect(server.to('/api/auth/credentials').length).toBeGreaterThan(before));
  });

  it('says a cancelled prompt calmly and sends no verify', async () => {
    stub(async () => {
      throw new DOMException('The operation either timed out or was not allowed.', 'NotAllowedError');
    });
    const server = fakeServer();
    const region = await passkeys(server);
    await add(region);
    const line = await within(region).findByText('The passkey request was cancelled or timed out. Nothing was added.');
    expect(line.getAttribute('role')).toBe('status');
    expect(within(region).queryByRole('alert')).toBeNull();
    expect(server.writes()).toEqual([{ method: 'POST', path: '/api/auth/register/options', body: {} }]);
    expect((within(region).getByRole('button', { name: 'Add a passkey' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('says a prompt that resolves without a credential calmly and sends no verify', async () => {
    const webauthn = stub(async () => null);
    const server = fakeServer();
    const region = await passkeys(server);
    await add(region);
    const line = await within(region).findByText('The passkey request was cancelled or timed out. Nothing was added.');
    expect(line.getAttribute('role')).toBe('status');
    expect(within(region).queryByRole('alert')).toBeNull();
    expect(webauthn.create).toHaveBeenCalledTimes(1);
    expect(server.writes()).toEqual([{ method: 'POST', path: '/api/auth/register/options', body: {} }]);
    expect((within(region).getByRole('button', { name: 'Add a passkey' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('takes the recent-sign-in path on reauth_required and never prompts', async () => {
    const webauthn = stub(async () => ({ toJSON: () => CREDENTIAL_JSON }));
    const navigateAway = vi.fn();
    const server = fakeServer({ overrides: { 'POST /api/auth/register/options': refusal('reauth_required') } });
    const region = await passkeys(server, navigateAway);
    await add(region);
    expect(await within(region).findByText('Sign in again to do this (required every 10 minutes for account actions).')).toBeTruthy();
    expect(within(region).getByRole('link', { name: 'Sign in again' }).getAttribute('href')).toBe('/api/auth/signin?next=/settings');
    expect(webauthn.create).not.toHaveBeenCalled();
    expect(navigateAway).not.toHaveBeenCalled();
  });

  it('shows a refused registration and a device that already holds a passkey', async () => {
    let attempt = 0;
    stub(async () => {
      attempt += 1;
      if (attempt === 2) throw new DOMException('already registered', 'InvalidStateError');
      return { toJSON: () => CREDENTIAL_JSON };
    });
    const server = fakeServer({ overrides: { 'POST /api/auth/register/verify': { status: 400, body: { error: 'register_failed' } } } });
    const region = await passkeys(server);
    await add(region);
    expect(await within(region).findByText('That passkey could not be registered. Try again.')).toBeTruthy();
    await add(region);
    expect(await within(region).findByText('This device already has a passkey for your account.')).toBeTruthy();
  });

  it('offers no button where the browser cannot parse creation options', async () => {
    const region = await passkeys(fakeServer());
    expect(await within(region).findByText('This browser cannot add passkeys here.')).toBeTruthy();
    expect(within(region).queryByRole('button', { name: 'Add a passkey' })).toBeNull();
  });
});

describe('People (FR-MANAGE-7)', () => {
  const person = async (panel: HTMLElement, handle: string) =>
    waitFor(() => {
      const li = within(panel).getAllByRole('listitem').find((x) => x.querySelector('.person-handle')?.textContent === handle);
      if (!li) throw new Error(`no ${handle}`);
      return li;
    });

  it('shows each person with source, state and passkeys, and the rules for actions', async () => {
    await mount(fakeServer());
    const panel = await tab('People');
    const ivan = await person(panel, 'ivan');
    expect(within(ivan).queryByRole('button')).toBeNull();
    const anna = await person(panel, 'anna');
    expect(within(anna).getByText('Managed in reader.yaml.')).toBeTruthy();
    expect(within(anna).queryByRole('button', { name: /Disable|Delete|Enable/ })).toBeNull();
    expect(within(await person(panel, 'max')).getByText('Locked')).toBeTruthy();
    expect(within(await person(panel, 'olga')).getByText('pending')).toBeTruthy();
    const petr = await person(panel, 'petr');
    expect(within(petr).getByText('disabled')).toBeTruthy();
    expect(within(petr).getByRole('button', { name: 'Enable petr' })).toBeTruthy();
    const lena = await person(panel, 'lena');
    expect(within(lena).getByText(/Laptop/)).toBeTruthy();
    expect(within(await person(panel, 'boris')).getByText('Disable instead: this account has its own data.')).toBeTruthy();
  });

  it('sends each person action', async () => {
    const server = fakeServer();
    await mount(server);
    const panel = await tab('People');
    await act(async () => fireEvent.click(within(await person(panel, 'petr')).getByRole('button', { name: 'Enable petr' })));
    await act(async () => fireEvent.click(within(await person(panel, 'lena')).getByRole('button', { name: 'Sign out everywhere: lena' })));
    let dialog = await screen.findByRole('dialog');
    await act(async () => fireEvent.click(within(dialog).getByRole('button', { name: 'Sign out everywhere' })));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    fireEvent.click(within(await person(panel, 'boris')).getByRole('button', { name: 'Remove passkey Phone of boris' }));
    dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/This account will no longer be able to sign in\./)).toBeTruthy();
    await act(async () => fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' })));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    fireEvent.click(within(await person(panel, 'lena')).getByRole('button', { name: 'Delete lena' }));
    dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/The handle becomes free for a new sign-up\./)).toBeTruthy();
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Type lena to confirm' }), { target: { value: 'lena' } });
    await act(async () => fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' })));
    await waitFor(() =>
      expect(server.writes()).toEqual([
        { method: 'POST', path: `/api/auth/signups/${ID.petr}/enable`, body: {} },
        { method: 'POST', path: `/api/auth/users/${ID.lena}/sessions/revoke`, body: {} },
        { method: 'DELETE', path: `/api/auth/users/${ID.boris}/credentials/p-boris-1`, body: {} },
        { method: 'DELETE', path: `/api/auth/users/${ID.lena}`, body: { confirm: 'lena' } },
      ]),
    );
  });
});

describe('Instance (FR-MANAGE-6, FR-MANAGE-11)', () => {
  it('shows each row with its value, key, source and when it applies, a hidden value and the file-only hint', async () => {
    await mount(fakeServer());
    const panel = await tab('Instance');
    const row = (name: string) =>
      waitFor(() => {
        const r = within(panel).getAllByRole('row').find((x) => x.querySelector('th')?.textContent === name);
        if (!r) throw new Error(`no ${name}`);
        return r;
      });
    expect(within(await row('tasks.(hidden).model')).getByText('(hidden)')).toBeTruthy();
    const model = await row('tasks.draft.model');
    expect(within(model).getByText('Change this in reader.yaml, then run `reader up`.')).toBeTruthy();
    expect(within(model).queryByRole('button', { name: /Edit/ })).toBeNull();
    expect(within(await row('pipeline.run_cap_usd')).getByText('fixed in reader-ai')).toBeTruthy();
    const retention = await row('observability.retention_days');
    expect(within(retention).getByText('Set here (reader.yaml says 30)')).toBeTruthy();
    expect(within(retention).getByText(/at the next run/)).toBeTruthy();
  });

  it('renders the route’s real body (VEC-MANAGE-2): keyed rows editable, model rows read-only, an ignored override with Reset', async () => {
    const body = readJson<{ body: unknown }>('spec/fixtures/WEB-MANAGE/settings-view.json').body;
    const server = fakeServer({ overrides: { 'GET /api/settings': { status: 200, body } } });
    await mount(server);
    const panel = await tab('Instance');
    const row = (name: string) =>
      waitFor(() => {
        const r = within(panel).getAllByRole('row').find((x) => x.querySelector('th')?.textContent === name);
        if (!r) throw new Error(`no ${name}`);
        return r;
      });
    const monthly = await row('budgets.monthly_usd');
    expect(within(monthly).getByText('Set here (reader.yaml says 200)')).toBeTruthy();
    expect(within(monthly).getByRole('button', { name: 'Edit budgets.monthly_usd' })).toBeTruthy();
    expect(within(await row('tasks.draft.model')).queryByRole('button', { name: /Edit/ })).toBeNull();
    expect(within(await row('observability.enabled')).queryByRole('button', { name: /Edit/ })).toBeNull();
    const gone = await row('tasks.gone.budget_usd_per_lesson');
    expect(within(gone).getByText('Ignored: the task is no longer in reader.yaml')).toBeTruthy();
    fireEvent.click(within(gone).getByRole('button', { name: 'Reset to reader.yaml' }));
    const dialog = await screen.findByRole('dialog');
    await act(async () => fireEvent.click(within(dialog).getByRole('button', { name: 'Reset' })));
    await waitFor(() => expect(server.writes()).toEqual([{ method: 'DELETE', path: '/api/settings/tasks.gone.budget_usd_per_lesson', body: {} }]));
  });

  it('edits a number within the schema bounds, resets to reader.yaml after a confirm, and disables Open when the file says off', async () => {
    const fx = fixture();
    const rows = (fx.settings as { rows: Array<Record<string, unknown>> }).rows.map((r) => (r.name === 'signup' ? fx.settings_signup_file_off : r));
    const server = fakeServer({ fx, overrides: { 'GET /api/settings': { status: 200, body: { rows, ignored: [] } } } });
    await mount(server);
    const panel = await tab('Instance');
    const retention = await waitFor(() => within(panel).getAllByRole('row').find((x) => x.querySelector('th')?.textContent === 'observability.retention_days')!);
    fireEvent.click(within(retention).getByRole('button', { name: 'Edit observability.retention_days' }));
    const field = within(retention).getByRole('spinbutton') as HTMLInputElement;
    expect(field.min).toBe('1');
    expect(field.max).toBe('365');
    fireEvent.change(field, { target: { value: '400' } });
    expect((within(retention).getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(field, { target: { value: '20' } });
    await act(async () => fireEvent.click(within(retention).getByRole('button', { name: 'Save' })));
    fireEvent.click(within(retention).getByRole('button', { name: 'Reset to reader.yaml' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/30/)).toBeTruthy();
    await act(async () => fireEvent.click(within(dialog).getByRole('button', { name: 'Reset' })));
    await waitFor(() =>
      expect(server.writes()).toEqual([
        { method: 'POST', path: '/api/settings/observability.retention_days', body: { value: 20, version: 2 } },
        { method: 'DELETE', path: '/api/settings/observability.retention_days', body: {} },
      ]),
    );
    const signup = within(panel).getAllByRole('row').find((x) => x.querySelector('th')?.textContent === 'signup')!;
    fireEvent.click(within(signup).getByRole('button', { name: 'Edit signup' }));
    expect(within(signup).getByText('reader.yaml turns sign-up off; only the file can open it.')).toBeTruthy();
    const open = within(signup).getByRole('option', { name: 'Open' }) as HTMLOptionElement;
    expect(open.disabled).toBe(true);
  });
});

describe('Data (FR-MANAGE-8, INV-MANAGE-4)', () => {
  it('lists recently deleted courses, lessons and sources with Restore, and blocks a course whose source is deleted', async () => {
    const server = fakeServer();
    await mount(server);
    const panel = await tab('Data');
    const deleted = await within(panel).findByRole('region', { name: 'Recently deleted' });
    const item = (text: string) =>
      waitFor(() => {
        const li = within(deleted).getAllByRole('listitem').find((x) => x.querySelector('.deleted-title')?.textContent === text);
        if (!li) throw new Error(`no ${text}`);
        return li;
      });
    expect(within(await item('Course of a deleted book')).getByText('Restore its source first.')).toBeTruthy();
    expect(within(await item('Course of a deleted book')).queryByRole('button', { name: 'Restore' })).toBeNull();
    await act(async () => fireEvent.click(within(await item('Deleted course')).getByRole('button', { name: 'Restore' })));
    await act(async () => fireEvent.click(within(await item('Fixture lesson W03: deleted')).getByRole('button', { name: 'Restore' })));
    await act(async () => fireEvent.click(within(await item('A deleted book')).getByRole('button', { name: 'Restore' })));
    expect(await screen.findByText(/1 course came back with it\./)).toBeTruthy();
    expect(server.writes()).toEqual([
      { method: 'POST', path: '/api/courses/c-deleted/restore', body: { at: 'first' } },
      { method: 'POST', path: '/api/lessons/w03/restore', body: {} },
      { method: 'POST', path: '/api/materials/up-cccccccccccc/restore', body: {} },
    ]);
    expect(within(panel).getByText('Download each lesson as .ipynb from its menu. A full export of your data is planned (ADR-0017).')).toBeTruthy();
  });

  it('pages the activity, newest first, with handles and current titles', async () => {
    const server = fakeServer();
    await mount(server);
    const panel = await tab('Data');
    const activity = await within(panel).findByRole('region', { name: 'Activity' });
    const rows = await waitFor(() => {
      const r = within(activity).getAllByRole('listitem');
      if (r.length < 2) throw new Error('not yet');
      return r;
    });
    expect(rows[0]!.textContent).toMatch(/ · ivan · .+ · Fixture course$/);
    expect(rows[1]!.textContent).toMatch(/ · ivan · .+ · course 9f8e7d6c$/);
    await act(async () => fireEvent.click(within(activity).getByRole('button', { name: 'Older' })));
    await waitFor(() => expect(within(activity).getAllByRole('listitem')).toHaveLength(3));
    expect(server.to('/api/audit')).toEqual([
      { method: 'GET', path: '/api/audit', body: undefined },
      { method: 'GET', path: '/api/audit?before=401', body: undefined },
    ]);
    expect(within(activity).queryByRole('button', { name: 'Older' })).toBeNull();
  });
});
