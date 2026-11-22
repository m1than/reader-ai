// The lesson view (WEB-NOTEBOOK FR-NOTEBOOK-1..3, 7, 9): cells by kind, references, open findings, lab cells with
// an in-memory editor and the kernel controls. Kernel state comes from the controller (FR-NOTEBOOK-4..8).
import { Button } from '@acme/ui/button';
import { Chip } from '@acme/ui/chip';
import { Dialog } from '@acme/ui/dialog';
import { useState, useSyncExternalStore, type KeyboardEvent } from 'react';
import { Code } from '../render/code';
import { Markdown } from '../render/markdown';
import { Output, type NbOutput } from '../render/outputs';
import { collectCitations, type Reference } from './citations';
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
const isOpenFinding = (c: NbCell) => reader(c).kind === 'open' && reader(c).status !== 'answered';

function References({ refs }: { refs: Reference[] }) {
  if (refs.length === 0) return null;
  return (
    <ol className="references" aria-label="References">
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
  return (
    <div className="lab">
      <textarea aria-label="Lab code" spellCheck={false} value={code} onChange={(e) => setCode(e.target.value)} onKeyDown={onKey} rows={Math.min(24, code.split('\n').length + 1)} />
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
        {run && run.state !== 'idle' && <span className="muted">{STATE_LABEL[run.state]}</span>}
        {run?.marker === 'timed_out' && run.state === 'running' && <span className="muted">timed out, interrupting…</span>}
      </div>
      <div className="outputs">
        {outputs.map((o, i) => (
          <Output key={i} output={o} />
        ))}
        {run?.notes.includes('output_truncated') && <p className="muted">Output was cut at the size limit.</p>}
      </div>
    </div>
  );
}

function CellView({ cell, index, run, controller, resetting }: CellProps & { index: number }) {
  const id = cellId(cell, index);
  const r = reader(cell);
  const kind = typeof r.kind === 'string' ? r.kind : undefined;
  const label = kind ? KIND_LABEL[kind] : undefined;
  const refs = kind && ['explain', 'changed', 'erratum', 'route'].includes(kind) ? collectCitations(r) : [];
  const finding = r.finding && typeof r.finding === 'object' ? (r.finding as Record<string, unknown>) : {};
  return (
    <article className="cell" data-cell-id={id} id={`cell-${id}`}>
      {label && <p className="cell-kind">{label}</p>}
      {isOpenFinding(cell) && (
        <div className="open-finding">
          <Chip.Root color="warning" variant="soft">
            <Chip.Label>open</Chip.Label>
          </Chip.Root>{' '}
          <span>{String(r.status ?? 'open')}</span>
          {finding.gate === 'G2' && (
            <>
              {' '}
              <strong>not yet confirmed</strong> <span>{String(finding.reason ?? '')}</span>
            </>
          )}
        </div>
      )}
      {kind === 'lab' || (cell.cell_type === 'code' && kind === undefined) ? (
        kind === 'lab' ? (
          <LabCell cell={cell} id={id} run={run} controller={controller} resetting={resetting} />
        ) : (
          <Code code={text(cell.source)} language="python" />
        )
      ) : cell.cell_type === 'code' ? (
        <Code code={text(cell.source)} language="python" />
      ) : (
        <Markdown source={text(cell.source)} />
      )}
      {kind === 'erratum' && typeof r.correction === 'string' && (
        <p className="correction">
          Correction: <span>{r.correction}</span>
        </p>
      )}
      <References refs={refs} />
    </article>
  );
}

export function LessonView({ notebook, controller }: { notebook: Notebook; controller: KernelController }) {
  const state = useSyncExternalStore(controller.subscribe, controller.getState, controller.getState);
  const [confirm, setConfirm] = useState(false);
  const labs = notebook.cells.map((c, i) => ({ c, id: cellId(c, i) })).filter(({ c }) => reader(c).kind === 'lab');
  const open = notebook.cells.map((c, i) => ({ c, id: cellId(c, i) })).filter(({ c }) => isOpenFinding(c));
  const title = notebook.metadata?.reader && typeof notebook.metadata.reader === 'object' ? (notebook.metadata.reader as { title?: unknown }).title : undefined;
  return (
    <main className="page lesson">
      <header className="lesson-header">
        {typeof title === 'string' && <h1>{title}</h1>}
        {open.length > 0 && (
          <a href={`#cell-${open[0]!.id}`}>
            {open.length} open finding{open.length === 1 ? '' : 's'}
          </a>
        )}
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
      </header>
      {notebook.cells.map((c, i) => (
        <CellView key={cellId(c, i)} cell={c} index={i} run={state.cells[cellId(c, i)]} controller={controller} resetting={state.resetting} />
      ))}
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
    </main>
  );
}
