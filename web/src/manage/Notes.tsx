// WEB-MANAGE FR-MANAGE-9 (every reader; P2): a bookmark toggle per cell, the lesson's bookmarks in its contents, and
// the reader's notes for the lesson (SRV-LESSON FR-LESSON-31). Notes are plain text rendered as text nodes
// (INV-UI-2), never Markdown; a deleted note is gone for good, so its Undo posts the same body again.
import { Button } from '@acme/ui/button';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useId, useState } from 'react';
import { textLang } from '../ui/i18n';
import type { Note } from './api';
import { KEYS, useManageApi } from './hooks';
import { problem, type Problem } from './model';
import { codeOf, ProblemLine, useManageT } from './primitives';
import { showUndo } from './toast';

const useBookmarks = (lessonId: string) => {
  const api = useManageApi();
  const q = useQuery({ queryKey: KEYS.bookmarks, queryFn: () => api.bookmarks() });
  return (q.data?.bookmarks ?? []).filter((b) => b.lesson_id === lessonId);
};

export function BookmarkToggle({ lessonId, cellId }: { lessonId: string; cellId: string }) {
  const t = useManageT();
  const api = useManageApi();
  const client = useQueryClient();
  const marks = useBookmarks(lessonId);
  const [local, setLocal] = useState<boolean | null>(null);
  const [issue, setIssue] = useState<Problem | null>(null);
  const on = local ?? marks.some((b) => b.cell_id === cellId);
  const toggle = async () => {
    setIssue(null);
    try {
      await api.bookmark(lessonId, cellId, !on);
      setLocal(!on);
      void client.invalidateQueries({ queryKey: KEYS.bookmarks });
    } catch (e) {
      setIssue(problem(codeOf(e), 'lesson', t));
    }
  };
  return (
    <>
      <Button size="sm" variant="ghost" color="neutral" aria-pressed={on} onClick={() => void toggle()}>
        {on ? t('bookmark.remove') : t('bookmark.add')}
      </Button>
      <ProblemLine problem={issue} />
    </>
  );
}

export function BookmarksList({ lessonId, labels }: { lessonId: string; labels: ReadonlyMap<string, string> }) {
  const t = useManageT();
  const api = useManageApi();
  const client = useQueryClient();
  const marks = useBookmarks(lessonId);
  const id = useId();
  if (marks.length === 0) return null;
  return (
    <div className="contents-bookmarks">
      <p className="contents-label" id={id}>
        {t('bookmarks.heading')}
      </p>
      <ul role="list" aria-labelledby={id}>
        {marks.map((b) => (
          <li key={b.cell_id}>
            {b.stale ? (
              <>
                <span className="muted">{t('bookmark.stale')}</span>{' '}
                <Button
                  size="sm"
                  variant="ghost"
                  color="neutral"
                  onClick={() => void api.bookmark(lessonId, b.cell_id, false).then(() => client.invalidateQueries({ queryKey: KEYS.bookmarks }))}
                >
                  {t('remove')}
                </Button>
              </>
            ) : (
              <a href={`#cell-${b.cell_id}`}>{labels.get(b.cell_id) ?? b.cell_id}</a>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function NotesToggle({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  const t = useManageT();
  return (
    <Button size="sm" variant="ghost" color="neutral" aria-expanded={open} onClick={onToggle}>
      {t('notes.toggle')}
    </Button>
  );
}

export function NotesPanel({ lessonId, labels, onClose }: { lessonId: string; labels: ReadonlyMap<string, string>; onClose: () => void }) {
  const t = useManageT();
  const api = useManageApi();
  const client = useQueryClient();
  const q = useQuery({ queryKey: KEYS.notes(lessonId), queryFn: () => api.notes(lessonId) });
  const [draft, setDraft] = useState('');
  const [editing, setEditing] = useState<{ id: string; body: string } | null>(null);
  const [issue, setIssue] = useState<Problem | null>(null);
  const newId = useId();
  const refresh = () => client.invalidateQueries({ queryKey: KEYS.notes(lessonId) });
  const run = async (f: () => Promise<unknown>) => {
    setIssue(null);
    try {
      await f();
      await refresh();
      return true;
    } catch (e) {
      setIssue(problem(codeOf(e), 'note', t));
      return false;
    }
  };
  const valid = (s: string) => s.trim().length >= 1 && [...s.trim()].length <= 10_000;
  const notes = [...(q.data?.notes ?? [])].sort((a, b) => b.created_at.localeCompare(a.created_at));
  const remove = async (n: Note) => {
    if (
      await run(() => api.deleteNote(lessonId, n.note_id))
    )
      showUndo({
        text: t('toast.note.deleted'),
        restoredText: t('toast.note.restored'),
        onUndo: async () => {
          await api.addNote(lessonId, n.body, n.cell_id);
          await refresh();
        },
      });
  };
  return (
    <section className="notes-panel" aria-labelledby={`${newId}-h`}>
      <div className="notes-head">
        <h2 id={`${newId}-h`}>{t('notes.heading')}</h2>
        <Button size="sm" variant="ghost" color="neutral" onClick={onClose}>
          {t('notes.close')}
        </Button>
      </div>
      <p className="manage-field">
        <label htmlFor={newId}>{t('notes.new')}</label>
        <textarea id={newId} className="manage-input" rows={3} maxLength={10_000} lang={textLang(draft)} value={draft} onChange={(e) => setDraft(e.target.value)} />
      </p>
      <Button
        size="sm"
        disabled={!valid(draft)}
        onClick={() =>
          void run(() => api.addNote(lessonId, draft.trim(), null)).then((ok) => {
            if (ok) setDraft('');
          })
        }
      >
        {t('notes.add')}
      </Button>
      <ProblemLine problem={issue} />
      {q.isSuccess && notes.length === 0 ? <p className="muted">{t('notes.none')}</p> : null}
      <ul role="list" className="manage-list">
        {notes.map((n) => (
          <li key={n.note_id} className="note-row">
            {editing?.id === n.note_id ? (
              <>
                <textarea
                  className="manage-input"
                  aria-label={t('notes.edit_label')}
                  rows={3}
                  maxLength={10_000}
                  value={editing.body}
                  onChange={(e) => setEditing({ id: n.note_id, body: e.target.value })}
                />
                <span className="row-buttons">
                  <Button
                    size="sm"
                    disabled={!valid(editing.body)}
                    onClick={() =>
                      void run(() => api.editNote(lessonId, n.note_id, editing.body.trim())).then((ok) => {
                        if (ok) setEditing(null);
                      })
                    }
                  >
                    {t('save')}
                  </Button>
                  <Button size="sm" variant="ghost" color="neutral" onClick={() => setEditing(null)}>
                    {t('cancel')}
                  </Button>
                </span>
              </>
            ) : (
              <>
                <p className="note-body" lang={textLang(n.body)}>
                  {n.body}
                </p>
                {n.cell_id ? <p className="muted">{t('notes.on', { cell: labels.get(n.cell_id) ?? n.cell_id })}</p> : null}
                <span className="row-buttons">
                  <Button size="sm" variant="ghost" color="neutral" onClick={() => setEditing({ id: n.note_id, body: n.body })}>
                    {t('notes.edit')}
                  </Button>
                  <Button size="sm" variant="ghost" color="neutral" onClick={() => void remove(n)}>
                    {t('delete')}
                  </Button>
                </span>
              </>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
