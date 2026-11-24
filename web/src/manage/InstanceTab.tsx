// WEB-MANAGE FR-MANAGE-6 and -11, Instance (owner): SRV-CONFIG FR-CONFIG-25's effective settings, each row with its
// value (or "(hidden)"), key, source and when it applies; allow-listed rows are edited through FR-CONFIG-26's overlay
// (ADR-0036 accepted): numbers within the schema's bounds, sign-up as a select whose Open is disabled when reader.yaml
// turns sign-up off, and a reset to reader.yaml after a confirm. Model rows stay read-only.
import { Button } from '@acme/ui/button';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import type { IgnoredOverride, Setting } from './api';
import { KEYS, useManageApi, useRefresh } from './hooks';
import { boundOf, problem, validNumber, type Problem } from './model';
import { codeOf, ConfirmDialog, ProblemLine, useManageT } from './primitives';

const show = (v: Setting['value']): string => (v === null ? '—' : String(v));

export function InstanceTab() {
  const t = useManageT();
  const api = useManageApi();
  const q = useQuery({ queryKey: KEYS.settings, queryFn: () => api.settings() });
  return (
    <section className="settings-section" aria-labelledby="instance-h">
      <h2 id="instance-h">{t('instance.heading')}</h2>
      {q.isPending ? <p role="status">{t('instance.loading')}</p> : null}
      {q.isError ? <ProblemLine problem={problem(codeOf(q.error), 'setting', t)} /> : null}
      {q.data ? (
        <div className="manage-scroll">
          <table className="manage-table">
            <caption className="visually-hidden">{t('instance.caption')}</caption>
            <thead>
              <tr>
                <th scope="col">{t('instance.col.name')}</th>
                <th scope="col">{t('instance.col.value')}</th>
                <th scope="col">{t('instance.col.key')}</th>
                <th scope="col">{t('instance.col.source')}</th>
                <th scope="col">{t('instance.col.applies')}</th>
              </tr>
            </thead>
            <tbody>
              {q.data.rows.map((s, i) => (
                <Row key={`${s.name}-${i}`} s={s} />
              ))}
              {q.data.ignored.map((o) => (
                <IgnoredRow key={o.key} o={o} />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}

function Row({ s }: { s: Setting }) {
  const t = useManageT();
  const api = useManageApi();
  const refresh = useRefresh();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(show(s.value));
  const [issue, setIssue] = useState<Problem | null>(null);
  const [resetting, setResetting] = useState(false);
  const [saved, setSaved] = useState(false);
  // FR-CONFIG-25: a row carries a key exactly when FR-CONFIG-26's allow-list lets the overlay set it.
  const editable = s.key !== null;
  const isSignup = s.key === 'sharing.signup';
  const bound = s.key ? boundOf(s.key) : null;
  const fileOff = isSignup && s.file_value === 'off';
  const parsed = isSignup ? (draft === 'open' || draft === 'off' ? draft : null) : bound ? validNumber(draft, bound) : null;
  const valid = parsed !== null && !(isSignup && fileOff && parsed === 'open');
  const save = async () => {
    if (!valid || s.key === null || parsed === null) return;
    setIssue(null);
    try {
      await api.setSetting(s.key, parsed, s.version ?? 0);
      setSaved(true);
      setEditing(false);
      await refresh();
    } catch (e) {
      setIssue(problem(codeOf(e), 'setting', t));
    }
  };
  return (
    <tr>
      <th scope="row">{s.name}</th>
      <td>
        {editing ? (
          <span className="setting-edit">
            {isSignup ? (
              <>
                <select className="manage-input" aria-label={s.name} value={draft} onChange={(e) => setDraft(e.target.value)}>
                  <option value="open" disabled={fileOff}>
                    {t('enum.signup.open')}
                  </option>
                  <option value="off">{t('enum.signup.off')}</option>
                </select>
                {fileOff ? <span className="muted">{t('instance.signup_off')}</span> : null}
              </>
            ) : bound ? (
              <>
                <input
                  className="manage-input"
                  type="number"
                  aria-label={s.name}
                  min={bound.min}
                  max={bound.max}
                  step={bound.step}
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                />
                <span className="muted">{t('instance.range', { min: bound.min, max: bound.max })}</span>
              </>
            ) : null}
            <Button size="sm" disabled={!valid} onClick={() => void save()}>
              {t('save')}
            </Button>
            <Button variant="ghost" color="neutral" size="sm" onClick={() => setEditing(false)}>
              {t('cancel')}
            </Button>
          </span>
        ) : (
          <span>{isSignup ? t.enum('signup', show(s.value)) : show(s.value)}</span>
        )}
        {s.source === 'override' ? <span className="muted setting-override">{t('instance.override', { value: show(s.file_value ?? null) })}</span> : null}
        {s.ignored ? <span className="muted">{t('instance.ignored', { reason: t.enum('ignored', s.ignored) })}</span> : null}
        {saved ? <span role="status">{t('instance.saved')}</span> : null}
        <ProblemLine problem={issue} />
      </td>
      <td>
        {s.source === 'fixed' ? (
          <span className="muted">{t('instance.fixed')}</span>
        ) : s.key ? (
          <code>{s.key}</code>
        ) : null}
        {!editable && s.source !== 'fixed' ? <span className="muted setting-hint">{t('instance.file_only')}</span> : null}
      </td>
      <td>{t.enum('setting_source', s.source)}</td>
      <td>
        {t.enum('read_when', s.read_when)}
        {editable && !editing ? (
          <span className="row-buttons">
            <Button
              variant="ghost"
              color="neutral"
              size="sm"
              aria-label={t('instance.edit', { name: s.name })}
              onClick={() => {
                setDraft(show(s.value));
                setSaved(false);
                setEditing(true);
              }}
            >
              {t('notes.edit')}
            </Button>
          </span>
        ) : null}
        {editable && s.source === 'override' ? (
          <Button variant="ghost" color="neutral" size="sm" onClick={() => setResetting(true)}>
            {t('instance.reset')}
          </Button>
        ) : null}
        {resetting && s.key ? (
          <ConfirmDialog
            open
            title={t('instance.reset_title', { name: s.name })}
            confirmLabel={t('instance.reset_confirm')}
            danger={false}
            onClose={() => setResetting(false)}
            onConfirm={async () => {
              setResetting(false);
              setIssue(null);
              try {
                await api.resetSetting(s.key!);
                await refresh();
              } catch (e) {
                setIssue(problem(codeOf(e), 'setting', t));
              }
            }}
          >
            <p>{t('instance.reset_body', { value: show(s.value), file: show(s.file_value ?? null) })}</p>
          </ConfirmDialog>
        ) : null}
      </td>
    </tr>
  );
}

// FR-CONFIG-26's stale override with no row of its own (its task left reader.yaml): shown so the owner can reset it.
function IgnoredRow({ o }: { o: IgnoredOverride }) {
  const t = useManageT();
  const api = useManageApi();
  const refresh = useRefresh();
  const [resetting, setResetting] = useState(false);
  const [issue, setIssue] = useState<Problem | null>(null);
  const value = typeof o.value === 'object' && o.value !== null ? JSON.stringify(o.value) : show((o.value ?? null) as Setting['value']);
  return (
    <tr>
      <th scope="row">{o.key}</th>
      <td>
        <span>{value}</span>
        <span className="muted">{t('instance.ignored', { reason: t.enum('ignored', o.reason) })}</span>
        <ProblemLine problem={issue} />
      </td>
      <td>
        <code>{o.key}</code>
      </td>
      <td>{t.enum('setting_source', 'override')}</td>
      <td>
        <Button variant="ghost" color="neutral" size="sm" onClick={() => setResetting(true)}>
          {t('instance.reset')}
        </Button>
        {resetting ? (
          <ConfirmDialog
            open
            title={t('instance.reset_title', { name: o.key })}
            confirmLabel={t('instance.reset_confirm')}
            danger={false}
            onClose={() => setResetting(false)}
            onConfirm={async () => {
              setResetting(false);
              setIssue(null);
              try {
                await api.resetSetting(o.key);
                await refresh();
              } catch (e) {
                setIssue(problem(codeOf(e), 'setting', t));
              }
            }}
          >
            <p>{t('instance.reset_ignored', { value })}</p>
          </ConfirmDialog>
        ) : null}
      </td>
    </tr>
  );
}
