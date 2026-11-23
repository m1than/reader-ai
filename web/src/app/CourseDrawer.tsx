// The course sidebar in the library Drawer below 75rem (WEB-UI FR-UI-17). Loaded lazily on the Courses button's first
// activation, so the drawer module stays out of the initial chunk (review B1).
import { Drawer } from '@acme/ui/drawer';
import { CourseSidebar } from './CourseSidebar';

export function CourseDrawer({ open, onClose, current }: { open: boolean; onClose: () => void; current: string | null }) {
  return (
    <Drawer.Root open={open} onOpenChange={(o) => (o ? undefined : onClose())} side="inline-start" variant="temporary">
      <Drawer.Portal>
        <Drawer.Backdrop />
        <Drawer.Popup className="course-drawer" id="course-drawer">
          <Drawer.Title className="visually-hidden">Courses</Drawer.Title>
          <CourseSidebar current={current} prefix="drawer" onNavigate={onClose} />
        </Drawer.Popup>
      </Drawer.Portal>
    </Drawer.Root>
  );
}

export default CourseDrawer;
