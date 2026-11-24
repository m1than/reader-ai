// WEB-MANAGE's slot stubs (WEB-UI FR-UI-6 manage slot; FR-UI-30 [build]: the only web/src/manage module allowed in
// an initial chunk). Each part is a lazy chunk that loads its Russian catalogue with it (FR-UI-31); course controls
// and the archive render for the owner only (INV-MANAGE-2).
import { lazy, Suspense, type ComponentType } from 'react';
import type { LessonSummary } from '../app/api';
import type { ManageSlot } from '../app/context';
import { withAreas } from '../ui/i18n';

const CourseActions = lazy(withAreas(() => import('./CourseActions').then((m) => ({ default: m.CourseActions })), ['manage']));
const ArchivedCourses = lazy(withAreas(() => import('./CourseActions').then((m) => ({ default: m.ArchivedCourses })), ['manage']));
const LessonActions = lazy(withAreas(() => import('./LessonActions').then((m) => ({ default: m.LessonActions })), ['manage']));
const ToastHost = lazy(withAreas(() => import('./toast'), ['manage']));

const suspended =
  <P extends object>(C: ComponentType<P>) =>
  (props: P) => (
    <Suspense fallback={null}>
      <C {...props} />
    </Suspense>
  );

export const manageSlot: ManageSlot = {
  CourseActions: suspended<{ courseId: string; title: string }>(CourseActions),
  LessonActions: suspended<{ lesson: LessonSummary }>(LessonActions),
  ListFooter: suspended<object>(ArchivedCourses),
  Host: suspended<object>(ToastHost),
};
