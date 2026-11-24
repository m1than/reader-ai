// WEB-MANAGE FR-MANAGE-7, People (owner): everyone with source, state, last sign-in, passkeys and sessions, and the
// per-person actions of SRV-AUTH FR-AUTH-41..43, FR-AUTH-29's Disable and FR-AUTH-48's Enable (ADR-0037 accepted).
// The owner's own row has no actions; a reader.yaml person has no Disable, Enable or Delete.
import { Button } from '@acme/ui/button';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { useServices } from '../app/context';
import { queries } from '../app/queries';
import { textLang, useLocale } from '../ui/i18n';
import type { Person } from './api';
import { KEYS, useManageApi, useRefresh } from './hooks';
import { problem, type Problem } from './model';
import { codeOf, ConfirmDialog, ProblemLine, useManageT } from './primitives';

type Ask =
  | { kind: 'signout'; p: Person }
  | { kind: 'passkey'; p: Person; pid: string; label: string }
  | { kind: 'disable'; p: Person }
  | { kind: 'delete'; p: Person };

export function PeopleTab() {
  const t = useManageT();
  const api = useManageApi();
  const { api: app } = useServices();
  const refresh = useRefresh();
  const { formatLocale } = useLocale();
  const me = useQuery(queries(app).me());
  const q = useQuery({ queryKey: KEYS.people, queryFn: () => api.people() });
  const [ask, setAsk] = useState<Ask | null>(null);
  const [issue, setIssue] = useState<Problem | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const when = (iso: string) => new Date(iso).toLocaleDateString(formatLocale, { dateStyle: 'medium' });
  const act = async (f: () => Promise<unknown>, ok?: string) => {
    setIssue(null);
    setNote(null);
    try {
      await f();
      if (ok) setNote(ok);
      await refresh();
    } catch (e) {
      const p = problem(codeOf(e), 'person', t);
      setIssue(p);
    }
  };
  const label = (l: string | null) => l ?? t('passkey.unnamed');
  return (
    <section className="settings-section" aria-labelledby="people-h">
      <h2 id="people-h">{t('people.heading')}</h2>
      {q.isPending ? <p role="status">{t('people.loading')}</p> : null}
      {q.isError ? <ProblemLine problem={problem(codeOf(q.error), 'person', t)} /> : null}
      {note ? <p role="status">{note}</p> : null}
      <ProblemLine problem={issue} />
      <ul role="list" className="manage-list people-list">
        {(q.data?.users ?? []).map((p) => {
          const self = p.id === me.data?.id;
          const signup = p.source === 'signup';
          const disabled = p.disabled_at !== null;
          const state = disabled ? 'disabled' : p.state;
          return (
            <li key={p.id} className="manage-row person-row">
              <span className="row-main">
                <span className="row-title" lang={textLang(p.display_name)}>
                  {p.display_name}
                </span>
                <span className="person-handle">{p.handle}</span>
                <span className="muted">
                  {t.enum('role', p.role)} · {t.enum('person_source', p.source)} · <span>{t.enum('person_state', state)}</span>
                </span>
                {p.locked ? <span className="chip">{t('person.locked')}</span> : null}
                <span className="muted">
                  {p.last_signin_at ? t('person.last', { date: when(p.last_signin_at) }) : t('person.never')} · {t('person.sessions', { count: p.sessions_active })}
                </span>
                <span className="muted">
                  {p.passkeys.length > 0 ? `${t('person.passkeys')} ${p.passkeys.map((k) => label(k.label)).join(', ')}` : t('person.no_passkeys')}
                </span>
                {self ? <span className="muted">{t('person.you')}</span> : null}
                {!self && !signup ? <span className="muted">{t('person.managed')}</span> : null}
                {!self && signup && p.role === 'learner' && p.owns_data ? <span className="muted">{t('person.has_data')}</span> : null}
              </span>
              {self ? null : (
                <span className="row-buttons">
                  {p.sessions_active > 0 ? (
                    <Button variant="ghost" color="neutral" size="sm" onClick={() => setAsk({ kind: 'signout', p })}>
                      {t('person.sign_out', { handle: p.handle })}
                    </Button>
                  ) : null}
                  {p.passkeys.map((k) => (
                    <Button
                      key={k.pid}
                      variant="ghost"
                      color="neutral"
                      size="sm"
                      aria-label={t('person.remove_passkey', { name: label(k.label), handle: p.handle })}
                      onClick={() => setAsk({ kind: 'passkey', p, pid: k.pid, label: label(k.label) })}
                    >
                      {t('person.remove_passkey_short')}
                    </Button>
                  ))}
                  {signup && !disabled && p.state === 'active' ? (
                    <Button variant="ghost" color="neutral" size="sm" onClick={() => setAsk({ kind: 'disable', p })}>
                      {t('person.disable', { handle: p.handle })}
                    </Button>
                  ) : null}
                  {signup && disabled ? (
                    <Button
                      variant="outline"
                      color="neutral"
                      size="sm"
                      onClick={() => void act(() => api.enablePerson(p.id), p.locked ? t('person.enabled_locked') : t('person.enabled', { handle: p.handle }))}
                    >
                      {t('person.enable', { handle: p.handle })}
                    </Button>
                  ) : null}
                  {signup && p.role === 'learner' && !p.owns_data ? (
                    <Button variant="ghost" color="neutral" size="sm" onClick={() => setAsk({ kind: 'delete', p })}>
                      {t('person.delete', { handle: p.handle })}
                    </Button>
                  ) : null}
                </span>
              )}
            </li>
          );
        })}
      </ul>
      {ask?.kind === 'signout' ? (
        <ConfirmDialog
          open
          title={t('person.sign_out_title', { handle: ask.p.handle })}
          confirmLabel={t('person.sign_out_confirm')}
          onClose={() => setAsk(null)}
          onConfirm={async () => {
            setAsk(null);
            await act(() => api.revokePerson(ask.p.id));
          }}
        >
          <p>{t('person.sign_out_body')}</p>
        </ConfirmDialog>
      ) : null}
      {ask?.kind === 'passkey' ? (
        <ConfirmDialog
          open
          title={t('person.remove_passkey_title', { name: ask.label, handle: ask.p.handle })}
          confirmLabel={t('remove')}
          onClose={() => setAsk(null)}
          onConfirm={async () => {
            setAsk(null);
            await act(() => api.removePersonPasskey(ask.p.id, ask.pid));
          }}
        >
          <p>
            {t('person.remove_passkey_body')} {ask.p.source === 'signup' && ask.p.passkeys.length === 1 ? `${t('person.remove_last')} ` : ''}
            {t('cannot_undo')}
          </p>
        </ConfirmDialog>
      ) : null}
      {ask?.kind === 'disable' ? (
        <ConfirmDialog
          open
          title={t('person.disable_title', { handle: ask.p.handle })}
          confirmLabel={t('person.disable_confirm')}
          onClose={() => setAsk(null)}
          onConfirm={async () => {
            setAsk(null);
            await act(() => api.disablePerson(ask.p.id));
          }}
        >
          <p>{t('person.disable_body')}</p>
        </ConfirmDialog>
      ) : null}
      {ask?.kind === 'delete' ? (
        <ConfirmDialog
          open
          title={t('person.delete_title', { handle: ask.p.handle })}
          confirmLabel={t('delete')}
          typed={ask.p.handle}
          onClose={() => setAsk(null)}
          onConfirm={async (confirm) => {
            setAsk(null);
            await act(() => api.deletePerson(ask.p.id, confirm ?? ''));
          }}
        >
          <p>
            {t('person.delete_body')} {t('cannot_undo')}
          </p>
        </ConfirmDialog>
      ) : null}
    </section>
  );
}
