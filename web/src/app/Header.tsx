// The app header (WEB-UI FR-UI-11): Courses (below 75rem), the brand, the main nav from NAV_ITEMS and the user menu.
// Below 75rem, where the Courses button appears, the main nav moves into the Courses drawer (live run 2026-11-24: the
// owner's four entries overflowed a 390px header by 119px; code review: a 40rem breakpoint still overflowed up to
// about 847px with a long display name). The user menu and the course drawer are lazy chunks loaded on their triggers' first
// activation (review B1); until then the triggers are plain buttons styled like the library's ghost buttons.
import { Link } from '@tanstack/react-router';
import { Component, lazy, Suspense, useRef, useState, type ReactNode } from 'react';
import { MarkIcon, MenuIcon } from '../ui/icons';
import { isCurrent, NAV_ITEMS, type NavBadge, type NavItem } from './nav';

const UserMenu = lazy(() => import('./UserMenu'));
const CourseDrawer = lazy(() => import('./CourseDrawer'));

const noBadge = (): NavBadge | null => null;

function NavEntry({ item, current, onNavigate }: { item: NavItem; current: boolean; onNavigate?: (() => void) | undefined }) {
  // The badge hook is fixed per item (the entry's key), so the hook order of this component never changes.
  const badge = (item.useBadge ?? noBadge)();
  return (
    <Link
      to={item.to}
      className="nav-link"
      aria-current={current ? 'page' : undefined}
      onClick={onNavigate}
    >
      {/* FR-UI-11: "<label> <shown> <after>", e.g. "Sign-ups 1 waiting", with `after` visually hidden; no punctuation
          in hidden text, so no browser's spacing can misplace it (code review 2026-11-24). */}
      {item.label}
      {badge ? (
        <>
          {' '}
          <span className="nav-badge">{badge.shown}</span>
          <span className="visually-hidden"> {badge.after}</span>
        </>
      ) : null}
    </Link>
  );
}

/** The main nav from NAV_ITEMS (FR-UI-11): `main-nav` in the header (hidden below 75rem), `drawer-nav` in the Courses
 * drawer (shown only below 75rem), so exactly one "Main" navigation is rendered visibly at any width. */
export function MainNav({ owner, path, className, onNavigate }: { owner: boolean; path: string; className: string; onNavigate?: () => void }) {
  return (
    <nav className={className} aria-label="Main">
      {NAV_ITEMS.filter((item) => owner || !item.owner_only).map((item) => (
        <NavEntry key={item.to} item={item} current={isCurrent(item, path)} onNavigate={onNavigate} />
      ))}
    </nav>
  );
}

/** A lazy chunk that fails to load (FR-UI-11, code review) shows its fallback, the plain trigger, instead of
 * removing it. */
class LazyBoundary extends Component<{ fallback: ReactNode; children: ReactNode }, { failed: boolean }> {
  override state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  override render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}

const ghost = { className: 'acme-button', 'data-variant': 'ghost', 'data-color': 'neutral', 'data-size': 'sm' } as const;

function PlainUserTrigger({ displayName, onClick }: { displayName: string; onClick?: () => void }) {
  return (
    <button
      type="button"
      {...ghost}
      className="acme-button user-trigger"
      aria-haspopup="menu"
      aria-expanded={false}
      aria-label={`Account menu for ${displayName}`}
      onClick={onClick}
    >
      <span className="who">{displayName}</span>
    </button>
  );
}

export function Header({
  displayName,
  owner,
  path,
  currentLesson,
  onSignOut,
}: {
  displayName: string;
  owner: boolean;
  path: string;
  currentLesson: string | null;
  onSignOut: () => void;
}) {
  const [menuLoaded, setMenuLoaded] = useState(false);
  const [drawerLoaded, setDrawerLoaded] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const coursesButton = useRef<HTMLButtonElement>(null);
  const closeDrawer = () => {
    setDrawerOpen(false);
    window.setTimeout(() => coursesButton.current?.focus(), 0);
  };
  return (
    <header className="app-header">
      <button
        ref={coursesButton}
        type="button"
        {...ghost}
        className="acme-button courses-button"
        aria-expanded={drawerOpen}
        aria-controls={drawerOpen ? 'course-drawer' : undefined}
        onClick={() => {
          setDrawerLoaded(true);
          setDrawerOpen(true);
        }}
      >
        <MenuIcon />
        <span className="courses-label">Courses</span>
      </button>
      <Link className="brand" to="/">
        <MarkIcon className="brand-mark" />
        <span className="brand-text">reader-ai</span>
      </Link>
      <MainNav owner={owner} path={path} className="main-nav" />
      <span className="header-end">
        {menuLoaded ? (
          <LazyBoundary fallback={<PlainUserTrigger displayName={displayName} />}>
            <Suspense fallback={<PlainUserTrigger displayName={displayName} />}>
              <UserMenu displayName={displayName} defaultOpen onSignOut={onSignOut} />
            </Suspense>
          </LazyBoundary>
        ) : (
          <PlainUserTrigger displayName={displayName} onClick={() => setMenuLoaded(true)} />
        )}
      </span>
      {drawerLoaded ? (
        // FR-UI-11 (code review): if the drawer chunk fails, the main nav stays reachable as plain links (the header's
        // own nav is hidden below 75rem); the course list waits for a reload.
        <LazyBoundary fallback={<MainNav owner={owner} path={path} className="nav-fallback" />}>
          <Suspense fallback={null}>
            <CourseDrawer
              open={drawerOpen}
              onClose={closeDrawer}
              current={currentLesson}
              nav={<MainNav owner={owner} path={path} className="drawer-nav" onNavigate={closeDrawer} />}
            />
          </Suspense>
        </LazyBoundary>
      ) : null}
    </header>
  );
}
