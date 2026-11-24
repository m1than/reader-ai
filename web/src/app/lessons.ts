// Lesson list grouping (WEB-UI FR-UI-6): courses sorted by title in the user's locale, lessons by ordinal. With
// SRV-LESSON's course positions (WEB-MANAGE FR-MANAGE-3, Move up / Move down), courses follow their position first.
import type { LessonSummary } from './api';

export interface CourseGroup {
  course_id: string;
  course_title: string;
  lessons: LessonSummary[];
  /** The course's position among the owner's active courses, when the server sends it. */
  position?: number | null | undefined;
}

const pos = (g: CourseGroup) => (typeof g.position === 'number' ? g.position : Number.POSITIVE_INFINITY);

export const groupLessons = (lessons: LessonSummary[], locale: string): CourseGroup[] => {
  const byCourse = new Map<string, CourseGroup>();
  for (const l of lessons) {
    const g = byCourse.get(l.course_id) ?? { course_id: l.course_id, course_title: l.course_title, lessons: [], position: l.course_position };
    g.lessons.push(l);
    byCourse.set(l.course_id, g);
  }
  const collator = new Intl.Collator(locale);
  return [...byCourse.values()]
    .map((g) => ({ ...g, lessons: [...g.lessons].sort((a, b) => a.ordinal - b.ordinal) }))
    .sort((a, b) => pos(a) - pos(b) || collator.compare(a.course_title, b.course_title));
};

/** The visible number of each lesson in its course, 1..n: ordinals keep gaps after a delete (SRV-LESSON FR-LESSON-28). */
export const visiblePositions = (groups: readonly CourseGroup[]): Map<string, number> =>
  new Map(groups.flatMap((g) => g.lessons.map((l, i) => [l.lesson_id, i + 1] as const)));
