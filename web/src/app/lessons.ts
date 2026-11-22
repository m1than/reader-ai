// Lesson list grouping (WEB-UI FR-UI-6): courses sorted by title in the user's locale, lessons by ordinal.
import type { LessonSummary } from './api';

export interface CourseGroup {
  course_id: string;
  course_title: string;
  lessons: LessonSummary[];
}

export const groupLessons = (lessons: LessonSummary[], locale: string): CourseGroup[] => {
  const byCourse = new Map<string, CourseGroup>();
  for (const l of lessons) {
    const g = byCourse.get(l.course_id) ?? { course_id: l.course_id, course_title: l.course_title, lessons: [] };
    g.lessons.push(l);
    byCourse.set(l.course_id, g);
  }
  const collator = new Intl.Collator(locale);
  return [...byCourse.values()]
    .map((g) => ({ ...g, lessons: [...g.lessons].sort((a, b) => a.ordinal - b.ordinal) }))
    .sort((a, b) => collator.compare(a.course_title, b.course_title));
};
