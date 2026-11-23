// The header's main navigation as data (WEB-UI FR-UI-11): other branches append entries here (V/signup's sign-ups
// link with its count as `badge`) without changing the header's code.
import type { ReactNode } from 'react';

export interface NavItem {
  label: string;
  to: string;
  /** Shown only to the owner (role `owner` from /api/me). */
  owner_only: boolean;
  /** Rendered inside the link after its label, e.g. a count. */
  badge?: () => ReactNode;
}

export const NAV_ITEMS: NavItem[] = [
  { label: 'Lessons', to: '/', owner_only: false },
  { label: 'Import', to: '/import', owner_only: true },
];

/** The item a path belongs to: "/" also for lesson pages. */
export const isCurrent = (item: NavItem, path: string): boolean =>
  item.to === '/' ? path === '/' || path.startsWith('/lessons/') : path === item.to || path.startsWith(`${item.to}/`);
