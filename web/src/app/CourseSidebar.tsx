// Course sidebar (WEB-UI FR-UI-17): links in disclosure lists, built from the shared lessons query. A static column at
// 75rem and wider; below, the header's Courses button opens the same component in a drawer (distinct id prefix).
import { Button } from '@acme/ui/button';
import { Skeleton } from '@acme/ui/skeleton';
import { useQuery } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { useState } from 'react';
import { ChevronIcon } from '../ui/icons';
import type { LessonSummary } from './api';
import { useServices } from './context';
import { groupLessons, type CourseGroup } from './lessons';
import { EXPANDED_MAX } from './LessonList';
import { queries } from './queries';

const locale = () => (typeof navigator !== 'undefined' && navigator.language) || 'en';
const published = (l: LessonSummary) => l.latest_sha256 !== null;

function Group({ group, current, prefix, onNavigate }: { group: CourseGroup; current: string | null; prefix: string; onNavigate?: () => void }) {
  const holdsCurrent = group.lessons.some((l) => l.lesson_id === current);
  const [open, setOpen] = useState(holdsCurrent);
  const [all, setAll] = useState(false);
  const listId = `${prefix}-course-${group.course_id}`;
  const done = group.lessons.filter(published).length;
  const large = group.lessons.length > EXPANDED_MAX;
  const rows = !large || all ? group.lessons : group.lessons.filter((l) => published(l) || l.lesson_id === current);
  return (
    <li className="sidebar-group">
      <button type="button" className="sidebar-course" aria-expanded={open} aria-controls={listId} onClick={() => setOpen((o) => !o)}>
        <ChevronIcon className="sidebar-chevron" />
        <span className="sidebar-course-title">{group.course_title}</span>
        <span className="sidebar-count">
          {`${done}/${group.lessons.length}`}
          <span className="visually-hidden"> published</span>
        </span>
      </button>
      <ul id={listId} role="list" hidden={!open}>
        {open
          ? rows.map((l) => (
              <li key={l.lesson_id} className={published(l) ? 'sidebar-row' : 'sidebar-row unpublished'}>
                {published(l) ? (
                  <Link
                    to="/lessons/$lessonId"
                    params={{ lessonId: l.lesson_id }}
                    aria-current={l.lesson_id === current ? 'page' : undefined}
                    onClick={onNavigate}
                  >
                    <span className="sidebar-ordinal" aria-hidden="true">
                      {l.ordinal}
                    </span>
                    {l.title}
                  </Link>
                ) : (
                  <span>
                    <span className="sidebar-ordinal" aria-hidden="true">
                      {l.ordinal}
                    </span>
                    {l.title}
                  </span>
                )}
              </li>
            ))
          : null}
      </ul>
      {open && large ? (
        <Button variant="ghost" color="neutral" size="sm" className="sidebar-more" onClick={() => setAll((a) => !a)}>
          {all ? 'Show fewer' : `Show all ${group.lessons.length}`}
        </Button>
      ) : null}
    </li>
  );
}

export function CourseSidebar({
  current = null,
  prefix = 'sidebar',
  onNavigate,
}: {
  /** The lesson on screen, if any: marked and its group open. */
  current?: string | null;
  /** Element id prefix: `sidebar` for the static column, `drawer` for the drawer's copy. */
  prefix?: string;
  onNavigate?: () => void;
}) {
  const { api } = useServices();
  const q = useQuery(queries(api).lessons());
  return (
    <nav className="course-sidebar" aria-label="Courses">
      {q.isPending ? (
        <div aria-hidden="true">
          <Skeleton.Root>
            <Skeleton.Item variant="text" width="70%" />
            <Skeleton.Item variant="text" width="55%" />
          </Skeleton.Root>
        </div>
      ) : q.isError ? (
        <div className="sidebar-error">
          <p>Courses could not be loaded.</p>
          <Button variant="ghost" color="neutral" size="sm" onClick={() => void q.refetch()}>
            Retry
          </Button>
        </div>
      ) : (
        <ul role="list" className="sidebar-groups">
          {groupLessons(q.data.lessons, locale()).map((g) => (
            <Group key={g.course_id} group={g} current={current} prefix={prefix} {...(onNavigate ? { onNavigate } : {})} />
          ))}
        </ul>
      )}
    </nav>
  );
}
