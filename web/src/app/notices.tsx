// Notices (WEB-UI FR-UI-7): plain text from a fixed catalogue keyed by code, each in sentence case ending with a
// full stop; at most 3 visible, older ones counted as "+n more".
import { Button } from '@acme/ui/button';
import { CloseIcon, InfoIcon } from '../ui/icons';

export const NOTICE_CATALOGUE: Readonly<Record<string, string>> = {
  rejected: 'The kernel refused that request.',
  busy: 'The kernel is busy; try again in a moment.',
  timeout: 'The cell ran longer than 60 seconds and was interrupted.',
  output_truncated: 'The output was too large and was cut.',
  kernel_restarted: 'The kernel restarted; variables from earlier cells are gone.',
  kernel_dead: 'The kernel stopped; the next Run starts a new one.',
  kernel_reset: 'The kernel was reset; the next Run starts a fresh one.',
  kernel_unavailable: 'The kernel is unavailable right now.',
  kernel_upstream: 'The kernel did not answer; try Run again.',
  too_many_kernels: 'Too many open lessons; close one or reset the kernel.',
  connect_failed: 'Could not connect to the kernel. The lesson may be open in other tabs.',
  sign_out_failed: 'Signing out failed; try again.',
  network: 'The network connection was lost.',
  bad_response: 'The server sent something unexpected.',
  unknown: 'Something went wrong.',
};

export const MAX_VISIBLE_NOTICES = 3;

// Own properties only: a code such as `constructor` or `__proto__` must not reach Object.prototype.
export const noticeText = (code: string): string =>
  Object.hasOwn(NOTICE_CATALOGUE, code) ? NOTICE_CATALOGUE[code]! : NOTICE_CATALOGUE.unknown!;

export interface Notice {
  id: number;
  code: string;
}

export function NoticeRegion({ notices, onDismiss }: { notices: Notice[]; onDismiss: (id: number) => void }) {
  const visible = notices.slice(-MAX_VISIBLE_NOTICES);
  const hidden = notices.length - visible.length;
  return (
    <aside className="notices" aria-label="Notices">
      {hidden > 0 && <p className="notices-more">{`+${hidden} more`}</p>}
      {visible.map((n) => (
        <div key={n.id} role="status" className="notice">
          <InfoIcon className="notice-icon" />
          <span>{noticeText(n.code)}</span>
          <Button variant="ghost" color="neutral" size="sm" onClick={() => onDismiss(n.id)} aria-label="Dismiss">
            <CloseIcon />
          </Button>
        </div>
      ))}
    </aside>
  );
}
