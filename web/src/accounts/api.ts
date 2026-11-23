// WEB-ACCOUNTS: the client of SRV-AUTH FR-AUTH-29's owner routes. Unlike the app's client, a 401
// `reauth_required` is the screen's to show (FR-ACCOUNTS-2); only `unauthenticated` goes to the shell's sign-in
// redirect (FR-UI-5). Every response is validated before use.
import * as v from 'valibot';
import { ApiError, type ApiDeps } from '../app/api';

const Pending = v.object({ id: v.string(), handle: v.string(), display_name: v.string(), requested_at: v.string() });
const Account = v.object({ ...Pending.entries, disabled: v.boolean() });
const Signups = v.object({ pending: v.array(Pending), accounts: v.array(Account) });

export type PendingSignup = v.InferOutput<typeof Pending>;
export type SignupAccount = v.InferOutput<typeof Account>;
export type Signups = v.InferOutput<typeof Signups>;
export type SignupAction = 'approve' | 'reject' | 'disable';

export const SIGNUPS_PATH = '/api/auth/signups';

export const createSignupsApi = (deps: ApiDeps) => {
  const send = async (method: 'GET' | 'POST', path: string, body?: unknown): Promise<Response> => {
    const init: RequestInit = { method, credentials: 'same-origin' };
    if (body !== undefined) {
      init.body = JSON.stringify(body);
      init.headers = { 'Content-Type': 'application/json' };
    }
    let res: Response;
    try {
      res = await deps.fetch(path, init);
    } catch {
      throw new ApiError(0, 'network');
    }
    if (res.ok) return res;
    const err = (await res.json().catch(() => null)) as { error?: unknown } | null;
    const code = typeof err?.error === 'string' ? err.error : `http_${res.status}`;
    if (res.status === 401 && code !== 'reauth_required') deps.onUnauthorized();
    throw new ApiError(res.status, code);
  };

  return {
    list: async (): Promise<Signups> => {
      const res = await send('GET', SIGNUPS_PATH);
      const parsed = v.safeParse(Signups, await res.json().catch(() => undefined));
      if (!parsed.success) throw new ApiError(res.status, 'bad_response');
      return parsed.output;
    },
    act: async (id: string, action: SignupAction): Promise<void> => {
      await send('POST', `${SIGNUPS_PATH}/${encodeURIComponent(id)}/${action}`, {});
    },
  };
};

export type SignupsApi = ReturnType<typeof createSignupsApi>;

/** One query key for the header count and the screen, so a page load asks once (FR-ACCOUNTS-4). */
export const SIGNUPS_KEY = ['signups'] as const;
