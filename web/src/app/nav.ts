// The header's main navigation as data (WEB-UI FR-UI-11): other branches append entries here (V/signup's sign-ups
// link with its count as `useBadge`) without changing the header's code.
import { useSignupsBadge } from './signupsBadge';

/** A nav entry's badge: the text it shows and the word the link's name adds after it ("Sign-ups, 3 waiting"). */
export interface NavBadge {
  shown: string;
  after: string;
}

export interface NavItem {
  label: string;
  to: string;
  /** Shown only to the owner (role `owner` from /api/me). */
  owner_only: boolean;
  /** A hook called once per render of the entry's link; `null` when there is nothing to show. The link then shows
   * `shown` and reads "<label>, <shown> <after>" (the separator and `after` are text at font size 0). */
  useBadge?: () => NavBadge | null;
}

export const NAV_ITEMS: NavItem[] = [
  { label: 'Lessons', to: '/', owner_only: false },
  { label: 'Import', to: '/import', owner_only: true },
  // FR-UI-14: WEB-ACCOUNTS's screen with the pending count.
  { label: 'Sign-ups', to: '/signups', owner_only: true, useBadge: useSignupsBadge },
  // FR-UI-18 (SRV-OBS FR-OBS-10): the owner's observability page.
  { label: 'Observability', to: '/observability', owner_only: true },
];

/** The item a path belongs to: "/" also for lesson pages. */
export const isCurrent = (item: NavItem, path: string): boolean =>
  item.to === '/' ? path === '/' || path.startsWith('/lessons/') : path === item.to || path.startsWith(`${item.to}/`);
