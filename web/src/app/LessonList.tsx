// Lesson list (WEB-UI FR-UI-6): a Continue block, then one group per course with its progress; a group of more than
// 12 lessons starts collapsed to its published rows. Every row may carry the compile slot (status and action).
import { Button } from '@acme/ui/button';
import { Link } from '@tanstack/react-router';
import { useContext, useEffect, useId, useRef, useState } from 'react';
import type { LessonSummary } from './api';
import { ServicesContext, type CompileSlot } from './context';
import { groupLessons, type CourseGroup } from './lessons';

/** Structural bounds (FR-UI-6): a short Continue block, one screen of rows before a group collapses. */
export const CONTINUE_MAX = 5;
export const EXPANDED_MAX = 12;

const date = (iso: string, locale: string) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString(locale, { year: 'numeric', month: 'short', day: 'numeric' });
};
const published = (l: LessonSummary) => l.latest_sha256 !== null;

function Row({ lesson, locale, slot }: { lesson: LessonSummary; locale: string; slot: CompileSlot | undefined }) {
  const parts = slot ? slot(lesson) : null;
  const status = parts?.status ?? (published(lesson) ? null : <span className="chip">not published yet</span>);
  const action = parts?.action ?? null;
  return (
    <li className={published(lesson) ? 'lesson-row published' : 'lesson-row'}>
      <span className="lesson-ordinal" aria-hidden="true">
        {lesson.ordinal}
      </span>
      <span className="lesson-title">
        {published(lesson) ? (
          <Link to="/lessons/$lessonId" params={{ lessonId: lesson.lesson_id }} className="row-link">
            {lesson.title}
          </Link>
        ) : (
          <span>{lesson.title}</span>
        )}
      </span>
      <span className="lesson-meta">
        {status ? <span className="lesson-status">{status}</span> : null}
        <time dateTime={lesson.updated_at}>{date(lesson.updated_at, locale)}</time>
      </span>
      {action ? <span className="row-action">{action}</span> : null}
    </li>
  );
}

function Course({ group, locale, slot, focused }: { group: CourseGroup; locale: string; slot: CompileSlot | undefined; focused: boolean }) {
  const id = useId();
  const total = group.lessons.length;
  const done = group.lessons.filter(published);
  const next = group.lessons.find((l) => !published(l));
  const [expanded, setExpanded] = useState(focused || total <= EXPANDED_MAX);
  const section = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!focused) return;
    setExpanded(true);
    section.current?.scrollIntoView({ block: 'start' });
  }, [focused]);
  const rows = expanded || !next ? group.lessons : done;
  return (
    <section ref={section} id={`course-${group.course_id}`} className="course-group" aria-labelledby={`${id}-h`}>
      <h2 id={`${id}-h`}>{group.course_title}</h2>
      <p className="course-summary">
        {`${done.length} of ${total} published`}
        {next ? ` · next: ${next.title}` : ''}
      </p>
      <ul id={`${id}-list`} role="list">
        {rows.map((l) => (
          <Row key={l.lesson_id} lesson={l} locale={locale} slot={slot} />
        ))}
      </ul>
      {next ? (
        <Button
          variant="ghost"
          color="neutral"
          size="sm"
          className="group-toggle"
          aria-expanded={expanded}
          aria-controls={`${id}-list`}
          onClick={() => setExpanded((e) => !e)}
        >
          {expanded ? 'Show published only' : `Show all ${total} lessons`}
        </Button>
      ) : null}
    </section>
  );
}

function Continue({ lessons, courses }: { lessons: LessonSummary[]; courses: Map<string, string> }) {
  const recent = lessons
    .filter(published)
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
    .slice(0, CONTINUE_MAX);
  if (recent.length === 0) return null;
  return (
    <section className="continue" aria-labelledby="continue-h">
      <h2 id="continue-h">Continue</h2>
      <ul role="list">
        {recent.map((l) => (
          <li key={l.lesson_id} className="lesson-row published">
            <span className="lesson-title">
              <Link to="/lessons/$lessonId" params={{ lessonId: l.lesson_id }} className="row-link">
                {l.title}
              </Link>
            </span>
            <span className="lesson-meta">
              <span>{courses.get(l.course_id) ?? l.course_title}</span>
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function LessonList({ lessons, locale, focusCourse = null }: { lessons: LessonSummary[]; locale: string; focusCourse?: string | null }) {
  const slot = useContext(ServicesContext)?.compileSlot;
  if (lessons.length === 0) return <p className="empty">No lessons yet</p>;
  const groups = groupLessons(lessons, locale);
  const titles = new Map(groups.map((g) => [g.course_id, g.course_title]));
  return (
    <div className="lesson-list">
      <Continue lessons={lessons} courses={titles} />
      {groups.map((g) => (
        <Course key={g.course_id} group={g} locale={locale} slot={slot} focused={focusCourse === g.course_id} />
      ))}
    </div>
  );
}
