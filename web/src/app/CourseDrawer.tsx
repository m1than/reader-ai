// The course sidebar in the library Drawer below 75rem (WEB-UI FR-UI-17). Loaded lazily on the Courses button's first
// activation, so the drawer module stays out of the initial chunk (review B1). Below 40rem it also holds the main nav
// (FR-UI-11), which the header then hides.
import { Drawer } from '@acme/ui/drawer';
import type { ReactNode } from 'react';
import { useT } from '../ui/i18n';
import { CourseSidebar } from './CourseSidebar';
import en from './messages.en';

export function CourseDrawer({ open, onClose, current, nav }: { open: boolean; onClose: () => void; current: string | null; nav?: ReactNode }) {
  const t = useT('app', en);
  return (
    <Drawer.Root open={open} onOpenChange={(o) => (o ? undefined : onClose())} side="inline-start" variant="temporary">
      <Drawer.Portal>
        <Drawer.Backdrop />
        <Drawer.Popup className="course-drawer" id="course-drawer">
          <Drawer.Title className="visually-hidden">{t('header.courses')}</Drawer.Title>
          {nav}
          <CourseSidebar current={current} prefix="drawer" onNavigate={onClose} />
        </Drawer.Popup>
      </Drawer.Portal>
    </Drawer.Root>
  );
}

export default CourseDrawer;
