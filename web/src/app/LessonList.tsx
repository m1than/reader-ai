// Lesson list (WEB-UI FR-UI-6): grouped by course; published lessons link to their view.
import { Link } from '@tanstack/react-router';
import type { LessonSummary } from './api';
import { groupLessons } from './lessons';

const date = (iso: string, locale: string) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString(locale, { year: 'numeric', month: 'short', day: 'numeric' });
};

export function LessonList({ lessons, locale }: { lessons: LessonSummary[]; locale: string }) {
  if (lessons.length === 0) return <p className="empty">No lessons yet</p>;
  return (
    <div className="lesson-list">
      {groupLessons(lessons, locale).map((course) => (
        <section key={course.course_id} aria-labelledby={`course-${course.course_id}`}>
          <h2 id={`course-${course.course_id}`}>{course.course_title}</h2>
          <ul>
            {course.lessons.map((l) => (
              <li key={l.lesson_id}>
                {l.latest_sha256 ? (
                  <Link to="/lessons/$lessonId" params={{ lessonId: l.lesson_id }}>
                    {l.title}
                  </Link>
                ) : (
                  <>
                    <span>{l.title}</span> <span className="muted">not published yet</span>
                  </>
                )}{' '}
                <time className="muted" dateTime={l.updated_at}>
                  {date(l.updated_at, locale)}
                </time>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
