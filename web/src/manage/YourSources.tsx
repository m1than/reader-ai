// WEB-MANAGE FR-MANAGE-5: the owner's sources on /import (WEB-IMPORT paired change), below the upload sections:
// each with pages, origin and the courses that use it; Rename and Delete… for imported sources only (SRV-INGEST
// FR-INGEST-30/-31). Its heading is "Manage your sources", so it is not a second "Your sources" heading beside
// WEB-PLAN's list on the same page.
import { useQuery } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { textLang } from '../ui/i18n';
import type { Material } from './api';
import { KEYS, useManageApi, useRefresh } from './hooks';
import { problem, type Problem } from './model';
import { ActionsMenu, codeOf, ConfirmDialog, ProblemLine, RenameField, useManageT } from './primitives';
import { showUndo } from './toast';

const titleOf = (m: Material) => m.display_title ?? m.title ?? m.id;

export function YourSources() {
  const t = useManageT();
  const api = useManageApi();
  const q = useQuery({ queryKey: KEYS.materials(false), queryFn: () => api.materials(false) });
  return (
    <section className="page manage-sources" aria-labelledby="manage-sources-h">
      <h2 id="manage-sources-h">{t('sources.heading')}</h2>
      {q.isPending ? <p role="status">{t('sources.loading')}</p> : null}
      {q.isError ? <ProblemLine problem={problem(codeOf(q.error), 'source', t)} /> : null}
      {q.isSuccess && q.data.materials.length === 0 ? <p className="muted">{t('sources.none')}</p> : null}
      <ul role="list" className="manage-list">
        {(q.data?.materials ?? []).map((m) => (
          <SourceRow key={m.id} m={m} />
        ))}
      </ul>
    </section>
  );
}

function SourceRow({ m }: { m: Material }) {
  const t = useManageT();
  const api = useManageApi();
  const refresh = useRefresh();
  const [mode, setMode] = useState<null | 'rename' | 'delete'>(null);
  const [issue, setIssue] = useState<Problem | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const title = titleOf(m);
  // Imported sources only: a reader.yaml source never imported has no row to rename or delete (FR-INGEST-30).
  const imported = m.status === 'imported' && m.version !== null;
  return (
    <li className="manage-row source-row">
      <span className="row-main">
        <span className="source-title row-title" lang={textLang(title)}>
          {title}
        </span>
        <span className="muted">
          {m.page_count !== null ? `${t('sources.pages', { count: m.page_count })} · ` : ''}
          <span>{t.enum('source', m.source)}</span>
        </span>
        {m.courses.length > 0 ? (
          <span className="muted">
            {t('sources.used_by')}{' '}
            {m.courses.map((c, i) => (
              <span key={c.course_id}>
                {i > 0 ? ', ' : ''}
                <a href={`/#course-${encodeURIComponent(c.course_id)}`} lang={textLang(c.title)}>
                  {c.title}
                </a>
              </span>
            ))}
          </span>
        ) : imported ? (
          <span className="muted">{t('sources.unused')}</span>
        ) : null}
      </span>
      {imported ? (
        <ActionsMenu
          label={title}
          triggerRef={trigger}
          items={[
            { label: t('rename'), onSelect: () => setMode('rename') },
            { label: t('delete_ellipsis'), onSelect: () => setMode('delete'), danger: true },
          ]}
        />
      ) : null}
      {mode === 'rename' ? (
        <RenameField
          value={title}
          label={t('sources.title_label')}
          object="source"
          returnFocus={trigger}
          {...(m.source === 'config' ? { hint: t('sources.config_hint') } : {})}
          onCancel={() => setMode(null)}
          onSave={async (next) => {
            await api.renameMaterial(m.id, m.version ?? 1, next);
            setMode(null);
            await refresh();
          }}
        />
      ) : null}
      <ProblemLine problem={issue} />
      {mode === 'delete' ? <DeleteSource m={m} title={title} onClose={() => setMode(null)} onProblem={setIssue} /> : null}
    </li>
  );
}

function DeleteSource({ m, title, onClose, onProblem }: { m: Material; title: string; onClose: () => void; onProblem: (p: Problem) => void }) {
  const t = useManageT();
  const api = useManageApi();
  const refresh = useRefresh();
  const impact = useQuery({ queryKey: ['manage', 'material-impact', m.id], queryFn: () => api.materialImpact(m.id), retry: false });
  const [alert, setAlert] = useState<Problem | null>(null);
  const courses = impact.data?.courses ?? m.courses;
  const cascade = courses.length > 0;
  return (
    <ConfirmDialog
      open
      title={t('sources.delete_title', { title })}
      confirmLabel={cascade ? t('sources.cascade', { count: courses.length }) : t('delete')}
      typed={title}
      alert={alert}
      onClose={onClose}
      onConfirm={async (confirm) => {
        setAlert(null);
        try {
          await api.deleteMaterial(m.id, confirm ?? '', cascade);
          onClose();
          await refresh();
          showUndo({
            text: t('toast.source.deleted'),
            restoredText: t('toast.source.restored'),
            onUndo: async () => {
              await api.restoreMaterial(m.id);
              await refresh();
            },
          });
        } catch (e) {
          const p = problem(codeOf(e), 'source', t);
          if (p.refetch) {
            onClose();
            onProblem(p);
          } else setAlert(p);
        }
      }}
    >
      <p>{t('sources.delete_body')}</p>
      {cascade ? (
        <>
          <p>{t('sources.in_use')}</p>
          <ul role="list" className="manage-list">
            {courses.map((c) => (
              <li key={c.course_id} lang={textLang(c.title)}>
                {c.title}
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </ConfirmDialog>
  );
}
