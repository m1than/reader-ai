// Notices (WEB-UI FR-UI-7): plain text from a fixed catalogue keyed by code, each in sentence case ending with a
// full stop; at most 3 visible, older ones counted as "+n more". The catalogue is the app area's `notice.*` messages
// (FR-UI-30); NOTICE_CATALOGUE is its English view.
import { Button } from '@acme/ui/button';
import { useT } from '../ui/i18n';
import { CloseIcon, InfoIcon } from '../ui/icons';
import en from './messages.en';

const PREFIX = 'notice.';

export const NOTICE_CATALOGUE: Readonly<Record<string, string>> = Object.fromEntries(
  Object.entries(en)
    .filter(([k]) => k.startsWith(PREFIX))
    .map(([k, v]) => [k.slice(PREFIX.length), v as string]),
);

export const MAX_VISIBLE_NOTICES = 3;

// Own properties only: a code such as `constructor` or `__proto__` must not reach Object.prototype.
export const noticeText = (code: string): string =>
  Object.hasOwn(NOTICE_CATALOGUE, code) ? NOTICE_CATALOGUE[code]! : NOTICE_CATALOGUE.unknown!;

export interface Notice {
  id: number;
  code: string;
}

export function NoticeRegion({ notices, onDismiss }: { notices: Notice[]; onDismiss: (id: number) => void }) {
  const t = useT('app', en);
  const visible = notices.slice(-MAX_VISIBLE_NOTICES);
  const hidden = notices.length - visible.length;
  const text = (code: string) => (Object.hasOwn(NOTICE_CATALOGUE, code) ? t.maybe(`${PREFIX}${code}`) : t('notice.unknown'));
  return (
    <aside className="notices" aria-label={t('notices.label')}>
      {hidden > 0 && <p className="notices-more">{t('notices.more', { count: hidden })}</p>}
      {visible.map((n) => (
        <div key={n.id} role="status" className="notice">
          <InfoIcon className="notice-icon" />
          <span>{text(n.code)}</span>
          <Button variant="ghost" color="neutral" size="sm" onClick={() => onDismiss(n.id)} aria-label={t('notices.dismiss')}>
            <CloseIcon />
          </Button>
        </div>
      ))}
    </aside>
  );
}
