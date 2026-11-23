// The app header (WEB-UI FR-UI-11): Courses (below 75rem), the brand, the main nav from NAV_ITEMS and the user menu.
// The user menu and the course drawer are lazy chunks loaded on their triggers' first activation (review B1); until
// then the triggers are plain buttons styled like the library's ghost buttons.
import { Link } from '@tanstack/react-router';
import { Component, lazy, Suspense, useRef, useState, type ReactNode } from 'react';
import { MarkIcon, MenuIcon } from '../ui/icons';
import { isCurrent, NAV_ITEMS } from './nav';

const UserMenu = lazy(() => import('./UserMenu'));
const CourseDrawer = lazy(() => import('./CourseDrawer'));

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
      <nav className="main-nav" aria-label="Main">
        {NAV_ITEMS.filter((item) => owner || !item.owner_only).map((item) => {
          const current = isCurrent(item, path);
          return (
            <Link key={item.to} to={item.to} className="nav-link" aria-current={current ? 'page' : undefined}>
              {item.label}
              {item.badge ? (
                <>
                  {/* FR-UI-11: the link reads "<label>, <badge's words>"; the badge shows its number. */}
                  <span className="visually-hidden">, </span>
                  {item.badge()}
                </>
              ) : null}
            </Link>
          );
        })}
      </nav>
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
        <LazyBoundary fallback={null}>
          <Suspense fallback={null}>
            <CourseDrawer open={drawerOpen} onClose={closeDrawer} current={currentLesson} />
          </Suspense>
        </LazyBoundary>
      ) : null}
    </header>
  );
}
