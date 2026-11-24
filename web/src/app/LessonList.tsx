// Lesson list (WEB-UI FR-UI-6): a Continue block, then one group per course with its progress; a group of more than
// 12 lessons starts collapsed to its published rows. Every row may carry the compile slot (status and action).
// FR-UI-30: copy from the app catalogue; FR-UI-32: course and lesson titles carry their own `lang`.
import { Button } from '@acme/ui/button';
import { Link } from '@tanstack/react-router';
import { useContext, useEffect, useId, useRef, useState } from 'react';
import { textLang, useT, type T } from '../ui/i18n';
import type { LessonSummary } from './api';
import { ServicesContext, type CompileSlot } from './context';
import { CheckIcon } from '../ui/icons';
import { groupLessons, visiblePositions, type CourseGroup } from './lessons';
import en from './messages.en';

/** Structural bounds (FR-UI-6): a short Continue block, one screen of rows before a group collapses. */
export const CONTINUE_MAX = 5;
export const EXPANDED_MAX = 12;

type Tr = T<typeof en>;

const date = (iso: string, locale: string) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString(locale, { year: 'numeric', month: 'short', day: 'numeric' });
};
const published = (l: LessonSummary) => l.latest_sha256 !== null;

/** FR-UI-6: the slot's Status, then the default status; app.css shows the default only while `.slot-status` is empty. */
export function RowStatus({ lesson, slot }: { lesson: LessonSummary; slot: CompileSlot | undefined }) {
  const t = useT('app', en);
  const fallback = published(lesson) ? null : <span className="chip">{t('list.not_published')}</span>;
  if (!slot?.Status) return fallback ? <span className="lesson-status">{fallback}</span> : null;
  const { Status } = slot;
  return (
    <span className="lesson-status">
      <span className="slot-status">
        <Status lesson={lesson} place="list" />
      </span>
      {fallback ? <span className="status-default">{fallback}</span> : null}
    </span>
  );
}

function Row({ lesson, locale, slot, position }: { lesson: LessonSummary; locale: string; slot: CompileSlot | undefined; position: number }) {
  const Action = slot?.Action;
  const manage = useContext(ServicesContext)?.manageSlot;
  const Manage = manage?.LessonActions;
  const t = useT('app', en);
  const cls = ['lesson-row', published(lesson) ? 'published' : '', lesson.skipped ? 'skipped' : ''].filter(Boolean).join(' ');
  return (
    <li className={cls}>
      {/* WEB-MANAGE FR-MANAGE-4: the visible position 1..n, never the stored ordinal (gaps after a delete). */}
      <span className="lesson-ordinal" aria-hidden="true">
        {position}
      </span>
      <span className="lesson-title" lang={textLang(lesson.title)}>
        {published(lesson) ? (
          <Link to="/lessons/$lessonId" params={{ lessonId: lesson.lesson_id }} className="row-link">
            {lesson.title}
          </Link>
        ) : (
          <span>{lesson.title}</span>
        )}
      </span>
      <span className="lesson-meta">
        {lesson.done ? (
          <span className="lesson-done">
            <CheckIcon />
            <span className="visually-hidden">{t('list.done')}</span>
          </span>
        ) : null}
        {lesson.skipped ? <span className="chip">{t('list.skipped')}</span> : null}
        <RowStatus lesson={lesson} slot={slot} />
        <time dateTime={lesson.updated_at}>{date(lesson.updated_at, locale)}</time>
      </span>
      {Action || Manage ? (
        <span className="row-action">
          {Action ? <Action lesson={lesson} place="list" /> : null}
          {Manage ? <Manage lesson={lesson} /> : null}
        </span>
      ) : null}
    </li>
  );
}

function Course({
  group,
  locale,
  slot,
  focused,
  t,
  positions,
}: {
  group: CourseGroup;
  locale: string;
  slot: CompileSlot | undefined;
  focused: boolean;
  t: Tr;
  positions: Map<string, number>;
}) {
  const CourseActions = useContext(ServicesContext)?.manageSlot?.CourseActions;
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
      <div className="course-head">
        <h2 id={`${id}-h`} lang={textLang(group.course_title)}>
          {group.course_title}
        </h2>
        {CourseActions ? <CourseActions courseId={group.course_id} title={group.course_title} /> : null}
      </div>
      <p className="course-summary">
        {next ? t('list.summary_next', { done: done.length, total, title: next.title }) : t('list.summary', { done: done.length, total })}
      </p>
      <ul id={`${id}-list`} role="list">
        {rows.map((l) => (
          <Row key={l.lesson_id} lesson={l} locale={locale} slot={slot} position={positions.get(l.lesson_id) ?? l.ordinal} />
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
          {expanded ? t('list.show_published') : t('list.show_all', { count: total })}
        </Button>
      ) : null}
    </section>
  );
}

function Continue({ lessons, courses, t }: { lessons: LessonSummary[]; courses: Map<string, string>; t: Tr }) {
  // WEB-MANAGE FR-MANAGE-4: a skipped lesson is never the Continue block's target.
  const recent = lessons
    .filter((l) => published(l) && !l.skipped)
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
    .slice(0, CONTINUE_MAX);
  if (recent.length === 0) return null;
  return (
    <section className="continue" aria-labelledby="continue-h">
      <h2 id="continue-h">{t('list.continue')}</h2>
      <ul role="list">
        {recent.map((l) => {
          const course = courses.get(l.course_id) ?? l.course_title;
          return (
            <li key={l.lesson_id} className="lesson-row published">
              <span className="lesson-title" lang={textLang(l.title)}>
                <Link to="/lessons/$lessonId" params={{ lessonId: l.lesson_id }} className="row-link">
                  {l.title}
                </Link>
              </span>
              <span className="lesson-meta">
                <span lang={textLang(course)}>{course}</span>
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export function LessonList({ lessons, locale, focusCourse = null }: { lessons: LessonSummary[]; locale: string; focusCourse?: string | null }) {
  const services = useContext(ServicesContext);
  const slot = services?.compileSlot;
  const Footer = services?.manageSlot?.ListFooter;
  const t = useT('app', en);
  if (lessons.length === 0)
    return (
      <>
        <p className="empty">{t('list.empty')}</p>
        {Footer ? <Footer /> : null}
      </>
    );
  const groups = groupLessons(lessons, locale);
  const titles = new Map(groups.map((g) => [g.course_id, g.course_title]));
  const positions = visiblePositions(groups);
  return (
    <div className="lesson-list">
      <Continue lessons={lessons} courses={titles} t={t} />
      {groups.map((g) => (
        <Course key={g.course_id} group={g} locale={locale} slot={slot} focused={focusCourse === g.course_id} t={t} positions={positions} />
      ))}
      {Footer ? <Footer /> : null}
    </div>
  );
}
