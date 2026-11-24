// WEB-ACCOUNTS FR-ACCOUNTS-1..3: the owner's Sign-ups screen. Pending requests with Approve and Reject (Reject asks
// first), approved sign-up accounts with Disable (asks first; permanent in the product). Every change is one click on
// the row's own button (INV-ACCOUNTS-1); names reach the DOM as text only (INV-ACCOUNTS-2). Messages come from this
// area's fixed catalogue (messages.en.ts, messages.ru.ts; WEB-UI FR-UI-30), never from a server response.
import { Button } from '@acme/ui/button';
import { Skeleton } from '@acme/ui/skeleton';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { ApiError } from '../app/api';
import { useServices } from '../app/context';
import { interleave, textLang, useLocale, useT, type T } from '../ui/i18n';
import { SIGNUPS_KEY, type PendingSignup, type SignupAccount, type SignupAction, type SignupsApi } from './api';
import en from './messages.en';

type Problem = 'reauth_required' | 'not_found' | 'unreachable' | 'failed';
type AccountsT = T<typeof en>;

const REAUTH_URL = '/api/auth/signin?next=/signups';

const problemOf = (e: unknown): Problem | null => {
  if (!(e instanceof ApiError)) return 'unreachable';
  if (e.status === 401 && e.code === 'reauth_required') return 'reauth_required';
  if (e.status === 401) return null; // the shell's sign-in redirect is already under way (FR-UI-5)
  if (e.status === 404) return 'not_found';
  if (e.status === 0 || e.status >= 500) return 'unreachable';
  return 'failed';
};

const navigatorLocale = () => (typeof navigator !== 'undefined' && navigator.language) || 'en';

/** "3 hours ago" from an ISO time, in the formatting locale (WEB-UI FR-UI-30); the exact time stays in the
 * `<time datetime>` attribute. */
export const relativeTime = (iso: string, now: number = Date.now(), locale: string = navigatorLocale()): string => {
  const seconds = Math.round((Date.parse(iso) - now) / 1000);
  const units: Array<[Intl.RelativeTimeFormatUnit, number]> = [
    ['day', 86400],
    ['hour', 3600],
    ['minute', 60],
  ];
  const fmt = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' });
  for (const [unit, size] of units) if (Math.abs(seconds) >= size) return fmt.format(Math.round(seconds / size), unit);
  return fmt.format(0, 'minute');
};

type RowProps = {
  row: PendingSignup | SignupAccount;
  kind: 'pending' | 'account';
  signups: SignupsApi;
  onDone: (row: PendingSignup | SignupAccount, action: SignupAction) => Promise<void>;
  t: AccountsT;
  locale: string;
};

function Row({ row, kind, signups, onDone, t, locale }: RowProps) {
  const [confirming, setConfirming] = useState<SignupAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const handle = `@${row.handle}`;

  useEffect(() => {
    if (confirming) cancelRef.current?.focus();
  }, [confirming]);

  const run = async (action: SignupAction) => {
    setConfirming(null);
    setBusy(true);
    setProblem(null);
    try {
      await signups.act(row.id, action);
    } catch (e) {
      setBusy(false);
      setProblem(problemOf(e));
      return;
    }
    setBusy(false);
    await onDone(row, action);
  };

  const disabled = 'disabled' in row && row.disabled;
  const confirmText = confirming === 'reject' ? t('confirm.reject', { handle }) : t('confirm.disable', { handle });
  const time = <time dateTime={row.requested_at}>{relativeTime(row.requested_at, Date.now(), locale)}</time>;

  return (
    <li className="signup-row" data-row={row.id}>
      <div className="signup-who">
        <span className="signup-handle">{handle}</span>
        <span className="signup-name" lang={textLang(row.display_name)}>
          {row.display_name}
        </span>
        <span className="signup-time muted">{kind === 'account' ? interleave(t('requested'), { when: time }) : time}</span>
      </div>
      {confirming ? (
        <div className="signup-confirm">
          <p>{confirmText}</p>
          <div className="state-actions">
            <Button variant="outline" color="danger" size="sm" onClick={() => void run(confirming)}>
              {confirming === 'reject' ? t('reject') : t('disable')}
            </Button>
            <Button ref={cancelRef} variant="ghost" color="neutral" size="sm" onClick={() => setConfirming(null)}>
              {t('cancel')}
            </Button>
          </div>
        </div>
      ) : kind === 'pending' ? (
        <div className="signup-actions state-actions">
          <Button data-first="pending" size="sm" aria-label={t('approve.label', { handle })} disabled={busy} onClick={() => void run('approve')}>
            {t('approve')}
          </Button>
          <Button
            variant="outline"
            color="danger"
            size="sm"
            aria-label={t('reject.label', { handle })}
            disabled={busy}
            onClick={() => setConfirming('reject')}
          >
            {t('reject')}
          </Button>
        </div>
      ) : disabled ? (
        <div className="signup-actions state-actions">
          <span className="signup-disabled muted">{t('disabled')}</span>
          {/* WEB-MANAGE paired change (ADR-0037): Enable restores access; Disable undoes it. */}
          <Button variant="outline" color="neutral" size="sm" aria-label={t('enable.label', { handle })} disabled={busy} onClick={() => void run('enable')}>
            {t('enable')}
          </Button>
        </div>
      ) : (
        <div className="signup-actions state-actions">
          <Button
            data-first="account"
            variant="outline"
            color="danger"
            size="sm"
            aria-label={t('disable.label', { handle })}
            disabled={busy}
            onClick={() => setConfirming('disable')}
          >
            {t('disable')}
          </Button>
        </div>
      )}
      {problem ? (
        <div className="signup-problem error">
          <p>{t(`problem.${problem}`)}</p>
          {problem === 'reauth_required' ? <a href={REAUTH_URL}>{t('sign_in_again')}</a> : null}
        </div>
      ) : null}
    </li>
  );
}

function Loading({ t }: { t: AccountsT }) {
  return (
    <div>
      {/* FR-UI-11: a status with hidden text, no aria-busy. */}
      <p role="status" className="visually-hidden">
        {t('loading')}
      </p>
      <div aria-hidden="true">
        <Skeleton.Root>
          <Skeleton.Item variant="text" width="60%" />
          <Skeleton.Item variant="text" width="80%" />
        </Skeleton.Root>
      </div>
    </div>
  );
}

export function SignupsPage() {
  const { signups } = useServices();
  if (!signups) throw new Error('signups client missing');
  const t = useT('accounts', en);
  const { formatLocale } = useLocale();
  const client = useQueryClient();
  const q = useQuery({ queryKey: SIGNUPS_KEY, queryFn: signups.list, staleTime: Infinity });
  const [announcement, setAnnouncement] = useState('');
  const headingRef = useRef<HTMLHeadingElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // After an action: focus the next row's first button in the same list, or the heading when that list is empty.
  const onDone = async (row: PendingSignup | SignupAccount, action: SignupAction) => {
    const selector = `[data-first="${action === 'disable' ? 'account' : 'pending'}"]`;
    const firsts = () => [...(listRef.current?.querySelectorAll<HTMLElement>(selector) ?? [])];
    const index = firsts().findIndex((el) => el.closest('[data-row]')?.getAttribute('data-row') === row.id);
    setAnnouncement(t(`done.${action}`, { handle: `@${row.handle}` }));
    await client.invalidateQueries({ queryKey: SIGNUPS_KEY });
    window.setTimeout(() => {
      const now = firsts();
      const next = now[Math.min(Math.max(0, index), now.length - 1)];
      if (next) next.focus();
      else headingRef.current?.focus();
    }, 0);
  };

  const sortedPending = q.data ? [...q.data.pending].sort((a, b) => a.requested_at.localeCompare(b.requested_at)) : [];

  return (
    <section className="page signups" aria-labelledby="signups-title">
      <h1 id="signups-title" ref={headingRef} tabIndex={-1}>
        {t('title')}
      </h1>
      <p className="lead">{t('lead')}</p>
      {q.isPending ? (
        <Loading t={t} />
      ) : q.isError ? (
        <div className="error state-actions">
          <p>{t('load_failed')}</p>
          <Button variant="outline" color="neutral" onClick={() => void q.refetch()}>
            {t('retry')}
          </Button>
        </div>
      ) : (
        <div ref={listRef}>
          <p role="status" className="signups-announce">
            {announcement}
          </p>
          <h2 id="signups-waiting">{t('waiting')}</h2>
          {sortedPending.length === 0 ? (
            <p className="muted">{t('waiting.none')}</p>
          ) : (
            <ul role="list" aria-labelledby="signups-waiting" className="signup-list">
              {sortedPending.map((row) => (
                <Row key={row.id} row={row} kind="pending" signups={signups} onDone={onDone} t={t} locale={formatLocale} />
              ))}
            </ul>
          )}
          <h2 id="signups-accounts">{t('accounts')}</h2>
          {q.data.accounts.length === 0 ? (
            <p className="muted">{t('accounts.none')}</p>
          ) : (
            <ul role="list" aria-labelledby="signups-accounts" className="signup-list">
              {q.data.accounts.map((row) => (
                <Row key={row.id} row={row} kind="account" signups={signups} onDone={onDone} t={t} locale={formatLocale} />
              ))}
            </ul>
          )}
          <p className="muted signups-help">{t('help')}</p>
        </div>
      )}
    </section>
  );
}
