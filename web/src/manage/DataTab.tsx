// WEB-MANAGE FR-MANAGE-8, Data (owner): "Recently deleted" courses, lessons and sources with Restore (SRV-LESSON
// FR-LESSON-21/-25/-28, SRV-INGEST FR-INGEST-31), "Activity" from SRV-EVENTS FR-EVENTS-31 (200 rows a page, Older),
// and the export note. Deletion is soft only: there is no purge control (owner decision D1).
import { Button } from '@acme/ui/button';
import { useQueries, useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { textLang, useLocale } from '../ui/i18n';
import type { AuditPage } from './api';
import { KEYS, useCourses, useManageApi, useRefresh } from './hooks';
import { problem, type Problem } from './model';
import { codeOf, ProblemLine, useManageT } from './primitives';

export function DataTab() {
  const t = useManageT();
  return (
    <div className="settings-section">
      <RecentlyDeleted />
      <Activity />
      <section className="settings-block" aria-labelledby="export-h">
        <h2 id="export-h">{t('export.heading')}</h2>
        <p>{t('export.text')}</p>
      </section>
    </div>
  );
}

function RecentlyDeleted() {
  const t = useManageT();
  const api = useManageApi();
  const refresh = useRefresh();
  const { formatLocale } = useLocale();
  const deletedCourses = useCourses('deleted');
  const active = useCourses('active');
  const archived = useCourses('archived');
  const materials = useQuery({ queryKey: KEYS.materials(true), queryFn: () => api.materials(true) });
  const live = [...(active.data?.courses ?? []), ...(archived.data?.courses ?? [])];
  const lessons = useQueries({
    queries: live.map((c) => ({ queryKey: KEYS.deletedLessons(c.course_id), queryFn: () => api.deletedLessons(c.course_id) })),
  });
  const [issue, setIssue] = useState<Problem | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const when = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleDateString(formatLocale, { dateStyle: 'medium' }) : '');
  const deletedMaterialIds = new Set((materials.data?.materials ?? []).map((m) => m.id));
  const restore = async (f: () => Promise<unknown>, object: 'course' | 'lesson' | 'source') => {
    setIssue(null);
    setNote(null);
    try {
      const r = await f();
      const back = (r as { restored_courses?: unknown[] } | undefined)?.restored_courses;
      setNote(back && back.length > 0 ? t('deleted.restored_with', { count: back.length }) : t('deleted.restored'));
      await refresh();
    } catch (e) {
      setIssue(problem(codeOf(e), object, t));
    }
  };
  const rows = [
    ...(deletedCourses.data?.courses ?? []).map((c) => ({
      key: `c-${c.course_id}`,
      title: c.title,
      meta: t('deleted.course', { date: when(c.deleted_at) }),
      blocked: c.material_id !== null && deletedMaterialIds.has(c.material_id ?? ''),
      onRestore: () => restore(() => api.restoreCourse(c.course_id, 'first'), 'course'),
    })),
    ...live.flatMap((c, i) =>
      (lessons[i]?.data?.lessons ?? []).map((l) => ({
        key: `l-${l.lesson_id}`,
        title: l.title,
        meta: t('deleted.lesson', { course: c.title, date: when(l.deleted_at) }),
        blocked: false,
        onRestore: () => restore(() => api.restoreLesson(l.lesson_id), 'lesson'),
      })),
    ),
    ...(materials.data?.materials ?? []).map((m) => ({
      key: `m-${m.id}`,
      title: m.display_title ?? m.title ?? m.id,
      meta: t('deleted.source', { date: when(m.deleted_at) }),
      blocked: false,
      onRestore: () => restore(() => api.restoreMaterial(m.id), 'source'),
    })),
  ];
  const loading = deletedCourses.isPending || materials.isPending;
  return (
    <section className="settings-block" aria-labelledby="deleted-h">
      <h2 id="deleted-h">{t('deleted.heading')}</h2>
      {loading ? <p role="status">{t('deleted.loading')}</p> : null}
      {!loading && rows.length === 0 ? <p className="muted">{t('deleted.none')}</p> : null}
      <ul role="list" className="manage-list">
        {rows.map((r) => (
          <li key={r.key} className="manage-row">
            <span className="row-main">
              <span className="deleted-title row-title" lang={textLang(r.title)}>
                {r.title}
              </span>
              <span className="muted">{r.meta}</span>
            </span>
            {r.blocked ? (
              <span className="muted">{t('deleted.blocked')}</span>
            ) : (
              <Button variant="outline" color="neutral" size="sm" onClick={() => void r.onRestore()}>
                {t('restore')}
              </Button>
            )}
          </li>
        ))}
      </ul>
      {note ? <p role="status">{note}</p> : null}
      <ProblemLine problem={issue} />
    </section>
  );
}

function Activity() {
  const t = useManageT();
  const api = useManageApi();
  const { formatLocale } = useLocale();
  const people = useQuery({ queryKey: KEYS.people, queryFn: () => api.people() });
  const active = useCourses('active');
  const archived = useCourses('archived');
  const deleted = useCourses('deleted');
  const materials = useQuery({ queryKey: KEYS.materials(false), queryFn: () => api.materials(false) });
  const [pages, setPages] = useState<AuditPage[]>([]);
  const [issue, setIssue] = useState<Problem | null>(null);
  const load = async (before: number | null) => {
    try {
      const page = await api.audit(before);
      setPages((ps) => (before === null ? [page] : [...ps, page]));
    } catch (e) {
      setIssue(problem(codeOf(e), 'setting', t));
    }
  };
  useEffect(() => {
    void load(null);
    // one first page per mount
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const handles = new Map((people.data?.users ?? []).map((u) => [u.id, u.handle]));
  const titles = new Map<string, string>([
    ...[...(active.data?.courses ?? []), ...(archived.data?.courses ?? []), ...(deleted.data?.courses ?? [])].map((c) => [c.course_id, c.title] as [string, string]),
    ...(materials.data?.materials ?? []).map((m) => [m.id, m.display_title ?? m.title ?? m.id] as [string, string]),
    ...(people.data?.users ?? []).map((u) => [u.id, u.handle] as [string, string]),
  ]);
  const rows = pages.flatMap((p) => p.rows);
  const next = pages.at(-1)?.next_before ?? null;
  return (
    <section className="settings-block" aria-labelledby="activity-h">
      <h2 id="activity-h">{t('activity.heading')}</h2>
      {pages.length > 0 && rows.length === 0 ? <p className="muted">{t('activity.none')}</p> : null}
      <ul role="list" className="manage-list">
        {rows.map((r) => {
          const who = r.actor_id ? (handles.get(r.actor_id) ?? r.actor_id.slice(0, 8)) : '—';
          const object = titles.get(r.object_id) ?? t('activity.deleted_object', { kind: t.enum('object_kind', r.object_kind), id: r.object_id.slice(0, 8) });
          const at = new Date(r.at).toLocaleString(formatLocale, { dateStyle: 'medium', timeStyle: 'short' });
          return <li key={r.id} className="activity-row">{`${at} · ${who} · ${t.enum('audit', r.action)} · ${object}`}</li>;
        })}
      </ul>
      {next !== null ? (
        <Button variant="ghost" color="neutral" size="sm" onClick={() => void load(next)}>
          {t('activity.older')}
        </Button>
      ) : null}
      <ProblemLine problem={issue} />
    </section>
  );
}
