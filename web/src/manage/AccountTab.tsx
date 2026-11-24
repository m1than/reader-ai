// WEB-MANAGE FR-MANAGE-7, Account (every user): the caller's passkeys (SRV-AUTH FR-AUTH-45, -46) and sessions
// (FR-AUTH-44). Removing a passkey or ending sessions needs a recent sign-in (FR-MANAGE-1's reauth path).
import { Button } from '@acme/ui/button';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { SIGN_IN_URL } from '../app/api';
import { useServices } from '../app/context';
import { textLang, useLocale } from '../ui/i18n';
import type { Credential } from './api';
import { KEYS, useManageApi, useRefresh } from './hooks';
import { problem, type Problem } from './model';
import { codeOf, ConfirmDialog, ProblemLine, RenameField, useManageT } from './primitives';

export function AccountTab() {
  return (
    <div className="settings-section">
      <Passkeys />
      <Sessions />
    </div>
  );
}

const useWhen = () => {
  const { formatLocale } = useLocale();
  return (iso: string) => new Date(iso).toLocaleDateString(formatLocale, { dateStyle: 'medium' });
};

function Passkeys() {
  const t = useManageT();
  const api = useManageApi();
  const refresh = useRefresh();
  const when = useWhen();
  const q = useQuery({ queryKey: KEYS.credentials, queryFn: () => api.credentials() });
  const [renaming, setRenaming] = useState<string | null>(null);
  const [removing, setRemoving] = useState<Credential | null>(null);
  const [issue, setIssue] = useState<Problem | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const list = q.data?.credentials ?? [];
  const name = (c: Credential) => c.label ?? t('passkey.unnamed');
  return (
    <section className="settings-block" aria-labelledby="passkeys-h">
      <h2 id="passkeys-h">{t('passkeys.heading')}</h2>
      {q.isPending ? <p role="status">{t('loading')}</p> : null}
      {q.isError ? <ProblemLine problem={problem(codeOf(q.error), 'passkey', t)} /> : null}
      <ul role="list" className="manage-list">
        {list.map((c) => (
          <li key={c.pid} className="manage-row">
            <span className="row-main">
              <span className="row-title" lang={textLang(name(c))}>
                {name(c)}
              </span>
              <span className="muted">
                {t('passkey.meta', { created: when(c.created_at), used: c.last_used_at ? t('passkey.used', { date: when(c.last_used_at) }) : t('passkey.never') })}
              </span>
              {c.backed_up ? <span className="chip">{t('passkey.synced')}</span> : null}
            </span>
            <span className="row-buttons">
              <Button variant="ghost" color="neutral" size="sm" onClick={() => setRenaming(c.pid)}>
                {t('passkey.rename', { name: name(c) })}
              </Button>
              {list.length > 1 ? (
                <Button variant="ghost" color="neutral" size="sm" onClick={() => setRemoving(c)}>
                  {t('passkey.remove', { name: name(c) })}
                </Button>
              ) : null}
            </span>
            {renaming === c.pid ? (
              <RenameField
                value={c.label ?? ''}
                label={t('passkey.label')}
                max={64}
                object="passkey"
                onCancel={() => setRenaming(null)}
                onSave={async (label) => {
                  await api.labelCredential(c.pid, label);
                  setRenaming(null);
                  await refresh();
                }}
              />
            ) : null}
          </li>
        ))}
      </ul>
      {list.length === 1 ? <p className="muted">{t('passkey.last')}</p> : null}
      <AddPasskey />
      {note ? <p role="status">{note}</p> : null}
      <ProblemLine problem={issue} />
      {removing ? (
        <ConfirmDialog
          open
          title={t('passkey.remove_title', { name: name(removing) })}
          confirmLabel={t('remove')}
          onClose={() => setRemoving(null)}
          onConfirm={async () => {
            const c = removing;
            setRemoving(null);
            setIssue(null);
            try {
              const r = await api.removeCredential(c.pid);
              setNote(t('passkey.removed', { count: r.revoked_sessions ?? 0 }));
              await refresh();
            } catch (e) {
              setIssue(problem(codeOf(e), 'passkey', t));
            }
          }}
        >
          <p>
            {t('passkey.remove_body')} {t('cannot_undo')}
          </p>
        </ConfirmDialog>
      ) : null}
    </section>
  );
}

/** FR-MANAGE-7 "Add a passkey": SRV-AUTH FR-AUTH-6's register ceremony in the page, the shape of the shared passkey
 * script (options → navigator.credentials.create → verify with the credential's JSON). */
const canRegister = (): boolean =>
  typeof PublicKeyCredential === 'function' &&
  typeof PublicKeyCredential.parseCreationOptionsFromJSON === 'function' &&
  typeof navigator.credentials?.create === 'function';

function AddPasskey() {
  const t = useManageT();
  const api = useManageApi();
  const refresh = useRefresh();
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [calm, setCalm] = useState<string | null>(null);
  const [issue, setIssue] = useState<Problem | null>(null);
  if (!canRegister()) return <p className="muted">{t('passkey.add_unsupported')}</p>;
  const add = async () => {
    if (busy) return;
    setBusy(true);
    setDone(null);
    setCalm(null);
    setIssue(null);
    try {
      const options = await api.registerOptions();
      const credential = (await navigator.credentials.create({ publicKey: PublicKeyCredential.parseCreationOptionsFromJSON(options) })) as PublicKeyCredential | null;
      if (credential === null) {
        setCalm(t('passkey.add_cancelled'));
        return;
      }
      await api.registerVerify(credential.toJSON());
      setDone(t('passkey.added'));
      await refresh();
    } catch (e) {
      const name = e instanceof DOMException ? e.name : '';
      if (name === 'NotAllowedError' || name === 'AbortError') setCalm(t('passkey.add_cancelled'));
      else if (name === 'InvalidStateError') setIssue({ text: t('passkey.add_exists') });
      else setIssue(problem(codeOf(e), 'passkey', t));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="manage-add-passkey">
      <Button variant="outline" color="neutral" size="sm" disabled={busy} onClick={() => void add()}>
        {t('passkey.add')}
      </Button>
      <p role="status" className="muted">
        {busy ? t('passkey.adding') : (done ?? calm ?? '')}
      </p>
      <ProblemLine problem={issue} />
    </div>
  );
}

function Sessions() {
  const t = useManageT();
  const api = useManageApi();
  const refresh = useRefresh();
  const { navigateAway } = useServices();
  const when = useWhen();
  const q = useQuery({ queryKey: KEYS.sessions, queryFn: () => api.sessions() });
  const [issue, setIssue] = useState<Problem | null>(null);
  const list = q.data?.sessions ?? [];
  const act = async (f: () => Promise<unknown>, after?: () => void) => {
    setIssue(null);
    try {
      await f();
      if (after) after();
      else await refresh();
    } catch (e) {
      setIssue(problem(codeOf(e), 'session', t));
    }
  };
  return (
    <section className="settings-block" aria-labelledby="sessions-h">
      <h2 id="sessions-h">{t('sessions.heading')}</h2>
      {q.isPending ? <p role="status">{t('loading')}</p> : null}
      <ul role="list" className="manage-list">
        {list.map((s) => (
          <li key={s.sid} className="manage-row">
            <span className="row-main">
              {s.current ? <span className="chip">{t('session.this')}</span> : null}
              <span className="muted">{t('session.meta', { created: when(s.created_at), expires: when(s.expires_at) })}</span>
            </span>
            <Button
              variant="ghost"
              color="neutral"
              size="sm"
              onClick={() => void act(() => api.revokeSession(s.sid), s.current ? () => navigateAway(SIGN_IN_URL) : undefined)}
            >
              {t('session.sign_out')}
            </Button>
          </li>
        ))}
      </ul>
      {list.some((s) => !s.current) ? (
        <Button variant="outline" color="neutral" size="sm" onClick={() => void act(() => api.revokeOthers())}>
          {t('session.others')}
        </Button>
      ) : q.isSuccess ? (
        <p className="muted">{t('session.none_other')}</p>
      ) : null}
      <ProblemLine problem={issue} />
    </section>
  );
}
