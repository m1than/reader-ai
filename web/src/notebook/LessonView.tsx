// The lesson view (WEB-NOTEBOOK FR-NOTEBOOK-1..3, 7, 9..11): cells by kind, grouped sources, open findings, lab cells
// with an in-memory editor and the kernel controls, and the lesson contents. Kernel state comes from the controller
// (FR-NOTEBOOK-4..8).
import { Button } from '@acme/ui/button';
import { Chip } from '@acme/ui/chip';
import { Dialog } from '@acme/ui/dialog';
import { useId, useMemo, useState, useSyncExternalStore, type KeyboardEvent, type MouseEvent, type ReactNode } from 'react';
import { Code } from '../render/code';
import { Markdown } from '../render/markdown';
import { Output, type NbOutput } from '../render/outputs';
import { collectCitations, lessonTitles, type Reference, type Titles } from './citations';
import { cellLabels, contentsEntries, isUnansweredOpen, type ContentsCell } from './contents';
import type { CellRun, KernelController } from './kernel';

export interface NbCell {
  cell_type: string;
  id?: string;
  source: string | string[];
  metadata?: Record<string, unknown>;
  outputs?: Array<Record<string, unknown>>;
  execution_count?: number | null;
}
export interface Notebook {
  cells: NbCell[];
  metadata?: Record<string, unknown>;
}

export const KIND_LABEL: Record<string, string> = {
  diagnostic: 'diagnostic',
  route: 'reading route',
  explain: 'explanation',
  changed: 'what changed',
  erratum: 'erratum',
  tradeoff: 'trade-off',
  lab: 'lab',
  recall: 'recall',
  notes: 'notes',
  open: 'open question',
};

const STATE_LABEL: Record<string, string> = {
  queued: 'queued',
  running: 'running',
  done: 'done',
  error: 'error',
  interrupted: 'interrupted',
  timed_out: 'timed out',
  not_run: 'not run',
  lost: 'lost (kernel restarted)',
};

const text = (s: string | string[]): string => (Array.isArray(s) ? s.join('') : s);
const reader = (c: NbCell): Record<string, unknown> => {
  const r = c.metadata?.reader;
  return r && typeof r === 'object' ? (r as Record<string, unknown>) : {};
};
const cellId = (c: NbCell, i: number) => c.id ?? `cell-${i}`;
const nonEmpty = (v: unknown): v is string => typeof v === 'string' && v.trim().length > 0;

/** Focus an in-page target after its link was followed (tabIndex -1 targets; FR-NOTEBOOK-11, WEB-UI FR-UI-11). */
export const focusHashTarget = (e: MouseEvent<HTMLAnchorElement>) => {
  const id = e.currentTarget.getAttribute('href')?.slice(1);
  const target = id ? document.getElementById(id) : null;
  if (target) window.setTimeout(() => target.focus({ preventScroll: true }), 0);
};

function Sources({ refs }: { refs: Reference[] }) {
  if (refs.length === 0) return null;
  return (
    <div className="sources">
      <p className="sources-label" aria-hidden="true">
        Sources
      </p>
      <ol className="references" aria-label="Sources">
        {refs.map((r, i) => (
          <li key={i}>
            {r.kind === 'link' ? (
              <a href={r.href} rel="noopener noreferrer" target="_blank">
                {r.text}
              </a>
            ) : (
              <span>{r.text}</span>
            )}
            {r.retrieved ? <span className="muted"> (retrieved {r.retrieved})</span> : null}
          </li>
        ))}
      </ol>
    </div>
  );
}

interface CellProps {
  cell: NbCell;
  run: CellRun | undefined;
  controller: KernelController;
  /** A kernel reset is running: Run is disabled (FR-NOTEBOOK-7). */
  resetting: boolean;
}

function LabCell({ cell, id, run, controller, resetting }: CellProps & { id: string }) {
  const original = text(cell.source);
  const [code, setCode] = useState(original);
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Tab') {
      e.preventDefault();
      const el = e.currentTarget;
      const { selectionStart: s, selectionEnd: t } = el;
      const next = `${code.slice(0, s)}    ${code.slice(t)}`;
      setCode(next);
      el.value = next;
      el.setSelectionRange(s + 4, s + 4);
    } else if (e.key === 'Enter' && e.shiftKey) {
      e.preventDefault();
      if (!resetting) controller.run(id, e.currentTarget.value);
    }
  };
  const outputs = (run ? run.outputs : (cell.outputs ?? [])) as NbOutput[];
  const count = run ? run.executionCount : (cell.execution_count ?? null);
  const running = run?.state === 'running';
  const truncated = run?.notes.includes('output_truncated') ?? false;
  return (
    <div className="lab">
      <textarea
        aria-label="Lab code"
        spellCheck={false}
        wrap="off"
        value={code}
        onChange={(e) => setCode(e.target.value)}
        onKeyDown={onKey}
        rows={Math.min(24, code.split('\n').length + 1)}
      />
      <div className="lab-controls">
        <Button size="sm" disabled={resetting} onClick={() => controller.run(id, code)}>
          Run
        </Button>
        {running && (
          <Button size="sm" variant="outline" color="neutral" onClick={() => controller.interrupt()}>
            Interrupt
          </Button>
        )}
        <Button size="sm" variant="ghost" color="neutral" onClick={() => setCode(original)}>
          Revert
        </Button>
        <span className="muted exec-count">{count !== null ? `[${count}]` : ''}</span>
        <span role="status" className="muted run-state">
          {run && run.state !== 'idle' ? STATE_LABEL[run.state] : ''}
        </span>
        {run?.marker === 'timed_out' && run.state === 'running' && <span className="muted">timed out, interrupting…</span>}
      </div>
      {(outputs.length > 0 || truncated) && (
        <div className="outputs">
          {outputs.map((o, i) => (
            <Output key={i} output={o} />
          ))}
          {truncated && <p className="muted">Output was cut at the size limit.</p>}
        </div>
      )}
    </div>
  );
}

const questionText = (q: unknown): string | null => {
  if (typeof q === 'string') return q;
  if (q && typeof q === 'object' && typeof (q as { text?: unknown }).text === 'string') return (q as { text: string }).text;
  return null;
};

/** FR-NOTEBOOK-10: diagnostic questions as a list, unless the source already holds every one. */
function Questions({ r, source }: { r: Record<string, unknown>; source: string }) {
  const qs = (Array.isArray(r.questions) ? r.questions : []).map(questionText).filter((q): q is string => !!q);
  if (qs.length === 0 || qs.every((q) => source.includes(q))) return null;
  return (
    <ol className="questions">
      {qs.map((q, i) => (
        <li key={i}>{q}</li>
      ))}
    </ol>
  );
}

function CellBody({ cell, kind, r, lab }: { cell: NbCell; kind: string | undefined; r: Record<string, unknown>; lab: ReactNode }) {
  const source = text(cell.source);
  if (kind === 'lab') return <>{lab}</>;
  if (cell.cell_type === 'code') return <Code code={source} language="python" />;
  if (kind === 'recall') {
    return (
      <>
        {nonEmpty(source) ? <Markdown source={source} /> : nonEmpty(r.front) ? <p className="recall-front">{r.front}</p> : null}
        {nonEmpty(r.back) && (
          <details className="recall-back">
            <summary>Show answer</summary>
            <p>{r.back}</p>
          </details>
        )}
      </>
    );
  }
  if (kind === 'notes' && !nonEmpty(source)) return <p className="muted">Notes are not saved in this version.</p>;
  return (
    <>
      <Markdown source={source} />
      {kind === 'diagnostic' && <Questions r={r} source={source} />}
    </>
  );
}

function CellView({ cell, index, run, controller, resetting, titles }: CellProps & { index: number; titles: Titles }) {
  const id = cellId(cell, index);
  const r = reader(cell);
  const kind = typeof r.kind === 'string' ? r.kind : undefined;
  const label = kind ? KIND_LABEL[kind] : undefined;
  const refs = kind && ['explain', 'changed', 'erratum', 'route'].includes(kind) ? collectCitations(r, titles) : [];
  const finding = r.finding && typeof r.finding === 'object' ? (r.finding as Record<string, unknown>) : {};
  const open = kind === 'open' && r.status !== 'answered';
  return (
    <article className="cell" data-cell-id={id} data-kind={kind} id={`cell-${id}`} tabIndex={-1}>
      {label && <p className="cell-kind">{label}</p>}
      {open && (
        <div className="open-finding">
          <Chip.Root color="warning" variant="soft">
            <Chip.Label>{String(r.status ?? 'open')}</Chip.Label>
          </Chip.Root>
          {finding.gate === 'G2' && (
            <>
              {' '}
              <strong>not yet confirmed</strong> <span>{String(finding.reason ?? '')}</span>
            </>
          )}
        </div>
      )}
      <CellBody cell={cell} kind={kind} r={r} lab={<LabCell cell={cell} id={id} run={run} controller={controller} resetting={resetting} />} />
      {kind === 'erratum' && typeof r.correction === 'string' && (
        <p className="correction">
          Correction: <span>{r.correction}</span>
        </p>
      )}
      <Sources refs={refs} />
    </article>
  );
}

export function LessonView({
  notebook,
  controller,
  aside,
  back,
}: {
  notebook: Notebook;
  controller: KernelController;
  /** Shown after the cells with the focused cell's id and contents label (SRV-TUTOR's Ask panel, FR-TUTOR-11). */
  aside?: ((focusedCellId: string | null, focusedLabel: string | null) => ReactNode) | undefined;
  /** The link back to the lesson list (FR-NOTEBOOK-11); the route passes a router link. */
  back?: ReactNode;
}) {
  const state = useSyncExternalStore(controller.subscribe, controller.getState, controller.getState);
  const [confirm, setConfirm] = useState(false);
  const [focused, setFocused] = useState<string | null>(null);
  const [contentsOpen, setContentsOpen] = useState(false);
  const navId = useId();
  const focusFrom = (target: EventTarget) => {
    const id = target instanceof Element ? target.closest('[data-cell-id]')?.getAttribute('data-cell-id') : null;
    if (id) setFocused(id);
  };
  const contentsCells = useMemo<ContentsCell[]>(
    () =>
      notebook.cells.map((c, i) => {
        const r = reader(c);
        return { cell_type: c.cell_type, id: cellId(c, i), source: text(c.source), kind: typeof r.kind === 'string' ? r.kind : undefined, status: r.status };
      }),
    [notebook],
  );
  const labels = useMemo(() => cellLabels(contentsCells), [contentsCells]);
  const titles = useMemo(() => lessonTitles(notebook.cells), [notebook]);
  const entries = contentsEntries(contentsCells, labels, aside !== undefined);
  const labs = notebook.cells.map((c, i) => ({ c, id: cellId(c, i) })).filter(({ c }) => reader(c).kind === 'lab');
  const open = contentsCells.filter(isUnansweredOpen);
  const title = notebook.metadata?.reader && typeof notebook.metadata.reader === 'object' ? (notebook.metadata.reader as { title?: unknown }).title : undefined;
  return (
    <div className="lesson">
      <header className="lesson-header">
        {back}
        {typeof title === 'string' && <h1>{title}</h1>}
        <div className="lesson-toolbar">
          {open.length > 0 && (
            <a href={`#cell-${open[0]!.id}`} onClick={focusHashTarget}>
              {open.length} open finding{open.length === 1 ? '' : 's'}
            </a>
          )}
          <span className="contents-toggle">
            <Button
              size="sm"
              variant="outline"
              color="neutral"
              aria-expanded={contentsOpen}
              aria-controls={navId}
              onClick={() => setContentsOpen((o) => !o)}
            >
              Contents
            </Button>
          </span>
          <div className="lesson-controls">
            {labs.length > 0 && (
              <Button size="sm" variant="outline" disabled={state.resetting} onClick={() => controller.runAll(labs.map(({ c, id }) => ({ id, code: text(c.source) })))}>
                Run all
              </Button>
            )}
            <Button size="sm" variant="ghost" color="danger" disabled={state.resetting} onClick={() => setConfirm(true)}>
              Reset kernel
            </Button>
            {(state.connection === 'disconnected' || state.connection === 'unavailable') && (
              <Button size="sm" variant="outline" color="neutral" onClick={() => controller.retry()}>
                Retry
              </Button>
            )}
            {state.resetFailed && !state.resetting && (
              <Button size="sm" variant="outline" color="danger" onClick={() => controller.reset()}>
                Retry reset
              </Button>
            )}
          </div>
        </div>
      </header>
      <nav id={navId} className="lesson-contents" aria-label="Lesson contents" data-open={contentsOpen}>
        <p className="contents-label" aria-hidden="true">
          Contents
        </p>
        <ol>
          {entries.map((e) => (
            <li key={`${e.href}:${e.label}`}>
              <a
                href={e.href}
                onClick={(ev) => {
                  setContentsOpen(false);
                  focusHashTarget(ev);
                }}
              >
                {e.label}
              </a>
            </li>
          ))}
        </ol>
      </nav>
      <div className="cells" onFocusCapture={(e) => focusFrom(e.target)} onClickCapture={(e) => focusFrom(e.target)}>
        {notebook.cells.map((c, i) => (
          <CellView key={cellId(c, i)} cell={c} index={i} run={state.cells[cellId(c, i)]} controller={controller} resetting={state.resetting} titles={titles} />
        ))}
      </div>
      {aside && <div className="lesson-aside">{aside(focused, focused ? (labels.get(focused) ?? null) : null)}</div>}
      <Dialog.Root open={confirm} onOpenChange={setConfirm}>
        <Dialog.Portal>
          <Dialog.Backdrop />
          <Dialog.Popup>
            <Dialog.Title>Reset the kernel?</Dialog.Title>
            <Dialog.Description>This clears the Python state of every open lesson, not only this one.</Dialog.Description>
            <div className="dialog-actions">
              <Dialog.Close>Cancel</Dialog.Close>
              <Button
                color="danger"
                onClick={() => {
                  setConfirm(false);
                  controller.reset();
                }}
              >
                Reset
              </Button>
            </div>
          </Dialog.Popup>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}
