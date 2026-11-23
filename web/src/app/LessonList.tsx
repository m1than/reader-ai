// Lesson list (WEB-UI FR-UI-6): grouped by course; published lessons link to their view.
import { Link } from '@tanstack/react-router';
import type { LessonSummary } from './api';
import { groupLessons } from './lessons';

const date = (iso: string, locale: string) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString(locale, { year: 'numeric', month: 'short', day: 'numeric' });
};

/** `status`: FR-UI-13's compile label per unpublished lesson id; "not published yet" otherwise (FR-UI-6). */
export function LessonList({ lessons, locale, status = {} }: { lessons: LessonSummary[]; locale: string; status?: Record<string, string> }) {
  if (lessons.length === 0) return <p className="empty">No lessons yet</p>;
  return (
    <div className="lesson-list">
      {groupLessons(lessons, locale).map((course) => (
        <section key={course.course_id} aria-labelledby={`course-${course.course_id}`}>
          <h2 id={`course-${course.course_id}`}>{course.course_title}</h2>
          <ul role="list">
            {/* FR-UI-6: one row — ordinal, title, then status and date as separate muted elements. */}
            {course.lessons.map((l) => (
              <li key={l.lesson_id} className={l.latest_sha256 ? 'lesson-row published' : 'lesson-row'}>
                <span className="lesson-ordinal" aria-hidden="true">
                  {l.ordinal}
                </span>
                <span className="lesson-title">
                  {l.latest_sha256 ? (
                    <Link to="/lessons/$lessonId" params={{ lessonId: l.lesson_id }}>
                      {l.title}
                    </Link>
                  ) : (
                    <span>{l.title}</span>
                  )}
                </span>
                <span className="lesson-meta">
                  {!l.latest_sha256 && <span className="lesson-status">{status[l.lesson_id] ?? 'not published yet'}</span>}
                  <time dateTime={l.updated_at}>{date(l.updated_at, locale)}</time>
                </span>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
