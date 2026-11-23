// The lesson view (WEB-NOTEBOOK FR-NOTEBOOK-1..3, 7, 9..11): cells by kind, grouped sources, open-question callouts,
// lab blocks with the CodeMirror editor (lazy; a plain text area until it loads), recall decks, the lesson contents
// with the current section, per-cell toolbars, the tutor panel's open state and the keyboard shortcuts. Kernel state
// comes from the controller (FR-NOTEBOOK-4..8).
import { Button } from '@acme/ui/button';
import { Dialog } from '@acme/ui/dialog';
import { Menu } from '@acme/ui/menu';
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
} from 'react';
import { Code } from '../render/code';
import { Markdown } from '../render/markdown';
import { Output, type NbOutput } from '../render/outputs';
import { AskIcon, CopyIcon, MoreIcon, PlayIcon, WarningIcon } from '../ui/icons';
import { collectCitations, lessonTitles, type Reference, type Titles } from './citations';
import { cellLabels, contentsEntries, isUnansweredOpen, type ContentsCell } from './contents';
import type { EditorHandle, EditorOptions } from './editor';
import type { CellRun, KernelController } from './kernel';
import { lessonLang } from './lang';

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

/** The lazy CodeMirror chunk (FR-NOTEBOOK-9). */
export interface EditorModule {
  mountEditor: (host: HTMLElement, opts: EditorOptions) => EditorHandle;
}
export type EditorLoader = () => Promise<EditorModule>;
const defaultLoadEditor: EditorLoader = () => import('./editor');

/** The tutor panel's state as the lesson view hands it to the `aside` (FR-NOTEBOOK-11, SRV-TUTOR FR-TUTOR-11). */
export interface PanelState {
  open: boolean;
  close: () => void;
}

/** Quiet labels (FR-NOTEBOOK-1): explanations, open questions, labs and recall cards are named otherwise. */
export const KIND_LABEL: Record<string, string> = {
  diagnostic: 'diagnostic',
  route: 'reading route',
  changed: 'what changed',
  erratum: 'erratum',
  tradeoff: 'trade-off',
  notes: 'notes',
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

const COPIED_MS = 2000; // structural: long enough to read "Copied"

const text = (s: string | string[]): string => (Array.isArray(s) ? s.join('') : s);
const reader = (c: NbCell): Record<string, unknown> => {
  const r = c.metadata?.reader;
  return r && typeof r === 'object' ? (r as Record<string, unknown>) : {};
};
const kindOf = (c: NbCell): string | undefined => {
  const k = reader(c).kind;
  return typeof k === 'string' ? k : undefined;
};
const cellId = (c: NbCell, i: number) => c.id ?? `cell-${i}`;
const nonEmpty = (v: unknown): v is string => typeof v === 'string' && v.trim().length > 0;
/** A status in sentence case: first letter capitalised, `_` as a space (FR-NOTEBOOK-3). */
export const sentenceCase = (s: string): string => {
  const t = s.replaceAll('_', ' ');
  return t.charAt(0).toUpperCase() + t.slice(1);
};

/** Focus an in-page target after its link was followed (tabIndex -1 targets; FR-NOTEBOOK-11, WEB-UI FR-UI-11). */
export const focusHashTarget = (e: MouseEvent<HTMLAnchorElement>) => {
  const id = e.currentTarget.getAttribute('href')?.slice(1);
  const target = id ? document.getElementById(id) : null;
  if (target) window.setTimeout(() => target.focus({ preventScroll: true }), 0);
};

/** FR-NOTEBOOK-11 (review B4): shortcuts never fire from text fields, editors, dialogs or menus. */
const GUARDED_ROLES = new Set(['dialog', 'menu', 'alertdialog']);
const guarded = (node: EventTarget): boolean => {
  if (!(node instanceof Element)) return false;
  const tag = node.tagName.toLowerCase();
  if (tag === 'textarea' || tag === 'input' || tag === 'select') return true;
  if ((node as HTMLElement).isContentEditable) return true;
  const ce = node.getAttribute('contenteditable');
  if (ce !== null && ce !== 'false') return true;
  return GUARDED_ROLES.has(node.getAttribute('role') ?? '');
};

/** Copies text and reports the outcome in a status line; "Copied" shows for COPIED_MS. */
const useCopy = () => {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const copy = async (value: string) => {
    window.clearTimeout(timer.current);
    try {
      await navigator.clipboard.writeText(value);
      setState('copied');
      timer.current = window.setTimeout(() => setState('idle'), COPIED_MS);
    } catch {
      setState('failed');
    }
  };
  return { state, copy };
};

function Sources({ refs }: { refs: Reference[] }) {
  if (refs.length === 0) return null;
  return (
    <div className="sources">
      <p className="sources-label" aria-hidden="true">
        Sources
      </p>
      {/* list-style none drops the list role in WebKit; role="list" restores it (FR-NOTEBOOK-2). */}
      <ol className="references" aria-label="Sources" role="list">
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

interface LabProps {
  cell: NbCell;
  id: string;
  label: string;
  run: CellRun | undefined;
  controller: KernelController;
  resetting: boolean;
  loadEditor: EditorLoader;
}

/** FR-NOTEBOOK-9: the lab block — header with the actions, the editor (or its fallback text area), the outputs. */
function LabBlock({ cell, id, label, run, controller, resetting, loadEditor }: LabProps) {
  const original = text(cell.source);
  const [code, setCode] = useState(original);
  const [editorModule, setEditorModule] = useState<EditorModule | null>(null);
  const host = useRef<HTMLDivElement>(null);
  const area = useRef<HTMLTextAreaElement>(null);
  const hadFocus = useRef(false);
  const handle = useRef<EditorHandle | null>(null);
  const latest = useRef({ resetting, run: (src: string) => controller.run(id, src) });
  latest.current = { resetting, run: (src: string) => controller.run(id, src) };
  const { state: copyState, copy } = useCopy();
  const codeLabel = `${label} code`;

  useEffect(() => {
    let live = true;
    loadEditor()
      .then((m) => {
        if (!live) return;
        // FR-NOTEBOOK-9 (code review): the user may be typing in the fallback; keep the text and the focus.
        hadFocus.current = document.activeElement !== null && document.activeElement === area.current;
        if (area.current) setCode(area.current.value);
        setEditorModule(m);
      })
      .catch(() => undefined); // the text area stays: a lab can always be edited and run
    return () => {
      live = false;
    };
  }, [loadEditor]);

  useEffect(() => {
    if (!editorModule || !host.current) return undefined;
    const h = editorModule.mountEditor(host.current, {
      doc: code,
      label: codeLabel,
      onChange: (t) => setCode(t),
      onRun: (t) => {
        if (!latest.current.resetting) latest.current.run(t);
      },
    });
    handle.current = h;
    if (hadFocus.current) h.focus();
    return () => {
      h.destroy();
      handle.current = null;
    };
    // The editor is mounted once per loaded module; its text then lives in the editor.
  }, [editorModule]);

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
  const revert = () => {
    setCode(original);
    handle.current?.setDoc(original);
  };
  const outputs = (run ? run.outputs : (cell.outputs ?? [])) as NbOutput[];
  const count = run ? run.executionCount : (cell.execution_count ?? null);
  const running = run?.state === 'running';
  const truncated = run?.notes.includes('output_truncated') ?? false;
  return (
    <div className="lab wide">
      <div className="lab-header">
        <span className="lab-title">{`Python · ${label}`}</span>
        <span className="muted exec-count">{count !== null ? `[${count}]` : ''}</span>
        <span role="status" className="muted run-state">
          {run && run.state !== 'idle' ? STATE_LABEL[run.state] : ''}
        </span>
        {run?.marker === 'timed_out' && run.state === 'running' && <span className="muted">timed out, interrupting…</span>}
        <span className="lab-actions">
          <Button size="sm" variant="ghost" color="neutral" onClick={() => void copy(code)}>
            <CopyIcon />
            {copyState === 'copied' ? 'Copied' : 'Copy'}
          </Button>
          <Button size="sm" variant="ghost" color="neutral" disabled={code === original} onClick={revert}>
            Revert
          </Button>
          {running && (
            <Button size="sm" variant="outline" color="neutral" onClick={() => controller.interrupt()}>
              Interrupt
            </Button>
          )}
          <Button size="sm" disabled={resetting} title="Run (Shift+Enter)" onClick={() => controller.run(id, code)}>
            <PlayIcon />
            Run
          </Button>
        </span>
      </div>
      {editorModule ? (
        <div ref={host} className="lab-editor" />
      ) : (
        <textarea
          ref={area}
          aria-label={codeLabel}
          spellCheck={false}
          wrap="off"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          onKeyDown={onKey}
          rows={Math.max(3, code.split('\n').length + 1)}
        />
      )}
      {(outputs.length > 0 || truncated) && (
        <div className="outputs">
          {outputs.map((o, i) => (
            <Output key={i} output={o} />
          ))}
          {truncated && <p className="muted">Output was cut at the size limit.</p>}
        </div>
      )}
      <span role="status" className="visually-hidden">
        {copyState === 'copied' ? 'Code copied' : copyState === 'failed' ? 'Copy failed' : ''}
      </span>
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

/** FR-NOTEBOOK-11: Copy (markdown cells) and Ask, in the tab order only while focus is inside the cell. */
function CellToolbar({ source, markdown, active, onAsk }: { source: string; markdown: boolean; active: boolean; onAsk?: (e: MouseEvent<HTMLButtonElement>) => void }) {
  const { state, copy } = useCopy();
  if (!markdown && !onAsk) return null;
  const tab = active ? 0 : -1;
  return (
    <div className="cell-toolbar">
      {markdown ? (
        <Button size="sm" variant="ghost" color="neutral" tabIndex={tab} aria-label="Copy cell text" onClick={() => void copy(source)}>
          <CopyIcon />
        </Button>
      ) : null}
      {onAsk ? (
        <Button size="sm" variant="ghost" color="neutral" tabIndex={tab} aria-label="Ask the tutor about this cell" onClick={onAsk}>
          <AskIcon />
        </Button>
      ) : null}
      <span role="status" className="visually-hidden">
        {state === 'copied' ? 'Cell text copied' : state === 'failed' ? 'Copy failed' : ''}
      </span>
    </div>
  );
}

interface CellProps {
  cell: NbCell;
  id: string;
  label: string | undefined;
  titles: Titles;
  run: CellRun | undefined;
  controller: KernelController;
  resetting: boolean;
  loadEditor: EditorLoader;
  active: boolean;
  onAsk?: ((id: string, e: MouseEvent<HTMLButtonElement>) => void) | undefined;
  className?: string;
  children?: ReactNode;
}

function CellView({ cell, id, label, titles, run, controller, resetting, loadEditor, active, onAsk, className, children }: CellProps) {
  const r = reader(cell);
  const kind = kindOf(cell);
  const source = text(cell.source);
  const kindLabel = kind ? KIND_LABEL[kind] : undefined;
  const refs = kind && ['explain', 'changed', 'erratum', 'route'].includes(kind) ? collectCitations(r, titles) : [];
  const finding = r.finding && typeof r.finding === 'object' ? (r.finding as Record<string, unknown>) : {};
  const status = typeof r.status === 'string' ? r.status : 'open';
  const unanswered = kind === 'open' && r.status !== 'answered';
  const ask = onAsk ? (e: MouseEvent<HTMLButtonElement>) => onAsk(id, e) : undefined;
  let body: ReactNode;
  if (kind === 'lab') {
    body = <LabBlock cell={cell} id={id} label={label ?? 'Lab'} run={run} controller={controller} resetting={resetting} loadEditor={loadEditor} />;
  } else if (cell.cell_type === 'code') {
    body = <Code code={source} language="python" />;
  } else if (kind === 'open') {
    body = (
      <div className="callout" data-tone="warning">
        <p className="callout-title">
          <WarningIcon />
          <span>Open question</span>
          {unanswered ? <span className="chip" data-tone="warning">{sentenceCase(status)}</span> : null}
        </p>
        {unanswered && finding.gate === 'G2' ? (
          <p className="muted callout-note">
            <span>Not yet confirmed by the source:</span> <span>{String(finding.reason ?? '')}</span>
          </p>
        ) : null}
        <Markdown source={source} />
      </div>
    );
  } else if (kind === 'recall') {
    body = children;
  } else if (kind === 'notes' && !nonEmpty(source)) {
    body = <p className="muted">Notes are not saved in this version.</p>;
  } else {
    body = (
      <>
        <Markdown source={source} />
        {kind === 'diagnostic' && <Questions r={r} source={source} />}
      </>
    );
  }
  return (
    <article className={className ?? 'cell'} data-cell-id={id} data-kind={kind} id={`cell-${id}`} tabIndex={-1}>
      {kind !== 'lab' ? <CellToolbar source={source} markdown={cell.cell_type === 'markdown'} active={active} {...(ask ? { onAsk: ask } : {})} /> : null}
      {kindLabel && <p className="cell-kind">{kindLabel}</p>}
      {body}
      {kind === 'erratum' && typeof r.correction === 'string' && (
        <p className="correction">
          Correction: <span>{r.correction}</span>
        </p>
      )}
      <Sources refs={refs} />
    </article>
  );
}

/** FR-NOTEBOOK-10: a run of adjacent recall cells as one deck with a show/hide-all toggle. */
function RecallDeck({ cells, render }: { cells: Array<{ cell: NbCell; id: string }>; render: (c: { cell: NbCell; id: string }, card: ReactNode) => ReactNode }) {
  const withBack = cells.filter(({ cell }) => nonEmpty(reader(cell).back)).map(({ id }) => id);
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const allOpen = withBack.length > 0 && withBack.every((id) => open.has(id));
  const headingId = useId();
  return (
    <section className="recall-deck" aria-labelledby={headingId}>
      <div className="recall-deck-header">
        <h2 id={headingId}>{`Recall cards (${cells.length})`}</h2>
        {withBack.length > 0 ? (
          <Button size="sm" variant="ghost" color="neutral" onClick={() => setOpen(allOpen ? new Set() : new Set(withBack))}>
            {allOpen ? 'Hide all answers' : 'Show all answers'}
          </Button>
        ) : null}
      </div>
      <div className="recall-grid">
        {cells.map((c) => {
          const r = reader(c.cell);
          const source = text(c.cell.source);
          const card = (
            <>
              {nonEmpty(source) ? <Markdown source={source} /> : nonEmpty(r.front) ? <p className="recall-front">{r.front}</p> : null}
              {nonEmpty(r.back) && (
                <details
                  className="recall-back"
                  open={open.has(c.id)}
                  onToggle={(e) => {
                    const isOpen = e.currentTarget.open;
                    setOpen((s) => {
                      if (s.has(c.id) === isOpen) return s;
                      const next = new Set(s);
                      if (isOpen) next.add(c.id);
                      else next.delete(c.id);
                      return next;
                    });
                  }}
                >
                  <summary>Show answer</summary>
                  <p>{r.back}</p>
                </details>
              )}
            </>
          );
          return render(c, card);
        })}
      </div>
    </section>
  );
}

type Block = { type: 'cell'; cell: NbCell; id: string } | { type: 'deck'; cells: Array<{ cell: NbCell; id: string }> };

const blocksOf = (cells: NbCell[]): Block[] => {
  const blocks: Block[] = [];
  cells.forEach((cell, i) => {
    const id = cellId(cell, i);
    const last = blocks[blocks.length - 1];
    if (kindOf(cell) === 'recall') {
      if (last?.type === 'deck') last.cells.push({ cell, id });
      else blocks.push({ type: 'deck', cells: [{ cell, id }] });
    } else blocks.push({ type: 'cell', cell, id });
  });
  return blocks;
};

/** FR-NOTEBOOK-11: the contents entry whose target last passed the top 30% of the viewport. */
const useCurrentSection = (hrefs: string[]) => {
  const [current, setCurrent] = useState<string | null>(hrefs[0] ?? null);
  const key = hrefs.join('|');
  useEffect(() => {
    setCurrent(hrefs[0] ?? null);
    if (typeof IntersectionObserver !== 'function') return undefined;
    const targets = hrefs.map((h) => document.getElementById(h.slice(1))).filter((t): t is HTMLElement => t !== null);
    const order = new Map(targets.map((t, i) => [t, i]));
    const io = new IntersectionObserver(
      (entries) => {
        const passed = entries.filter((e) => e.isIntersecting).map((e) => e.target as HTMLElement);
        if (passed.length === 0) return;
        const last = passed.sort((a, b) => (order.get(a) ?? 0) - (order.get(b) ?? 0)).at(-1)!;
        setCurrent(`#${last.id}`);
      },
      { rootMargin: '0px 0px -70% 0px' },
    );
    for (const t of targets) io.observe(t);
    return () => io.disconnect();
    // `key` stands for `hrefs`, whose identity changes every render.
  }, [key]);
  return current;
};

export function LessonView({
  notebook,
  controller,
  aside,
  crumbs,
  loadEditor = defaultLoadEditor,
}: {
  notebook: Notebook;
  controller: KernelController;
  /** The tutor panel, shown with the focused cell's id and contents label and the panel's state (FR-TUTOR-11). */
  aside?: ((focusedCellId: string | null, focusedLabel: string | null, panel: PanelState) => ReactNode) | undefined;
  /** The breadcrumbs above the title (FR-NOTEBOOK-11); the route passes the library Breadcrumbs. */
  crumbs?: ReactNode;
  /** The CodeMirror chunk loader (FR-NOTEBOOK-9); tests and stories hold, fail or stub it. */
  loadEditor?: EditorLoader;
}) {
  const state = useSyncExternalStore(controller.subscribe, controller.getState, controller.getState);
  const [confirm, setConfirm] = useState(false);
  const [shortcuts, setShortcuts] = useState(false);
  const [focused, setFocused] = useState<string | null>(null);
  const [contentsOpen, setContentsOpen] = useState(false);
  const [tutorOpen, setTutorOpen] = useState(false);
  const opener = useRef<HTMLElement | null>(null);
  const askButton = useRef<HTMLButtonElement>(null);
  const root = useRef<HTMLDivElement>(null);
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
  const lang = useMemo(() => lessonLang(notebook), [notebook]);
  const blocks = useMemo(() => blocksOf(notebook.cells), [notebook]);
  const entries = contentsEntries(contentsCells, labels, aside !== undefined);
  const current = useCurrentSection(entries.filter((e) => e.href !== '#tutor').map((e) => e.href));
  const currentIndex = entries.findIndex((e) => e.href === current);
  const labs = notebook.cells.map((c, i) => ({ c, id: cellId(c, i) })).filter(({ c }) => kindOf(c) === 'lab');
  const open = contentsCells.filter(isUnansweredOpen);
  const title = notebook.metadata?.reader && typeof notebook.metadata.reader === 'object' ? (notebook.metadata.reader as { title?: unknown }).title : undefined;

  const openTutor = useCallback((from: HTMLElement | null) => {
    opener.current = from;
    setTutorOpen(true);
  }, []);
  const closeTutor = useCallback(() => {
    setTutorOpen(false);
    window.setTimeout(() => {
      // SRV-TUTOR FR-TUTOR-11 (code review B2): back to the opener, unless it cannot take focus (removed, or hidden
      // like the contents entry below 60rem); then the toolbar's "Ask the tutor".
      const o = opener.current;
      if (o && o.isConnected) o.focus();
      if (!o || document.activeElement !== o) askButton.current?.focus();
    }, 0);
  }, []);
  const askAbout = (id: string, e: MouseEvent<HTMLButtonElement>) => {
    const from = e.currentTarget;
    setFocused(id);
    openTutor(from);
  };

  // FR-NOTEBOOK-11: keyboard shortcuts, guarded (review B4); Escape closes the tutor from the page (FR-TUTOR-11).
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.defaultPrevented || e.ctrlKey || e.altKey || e.metaKey) return;
      if (e.composedPath().some(guarded)) return;
      if (e.key === 'Escape') {
        if (tutorOpen) closeTutor();
        return;
      }
      const cells = root.current ? ([...root.current.querySelectorAll('[data-cell-id]')] as HTMLElement[]) : [];
      if (e.code === 'KeyJ' || e.code === 'KeyK') {
        if (cells.length === 0) return;
        const active = document.activeElement instanceof Element ? document.activeElement.closest('[data-cell-id]') : null;
        const at = active ? cells.indexOf(active as HTMLElement) : -1;
        const next = e.code === 'KeyJ' ? Math.min(cells.length - 1, at + 1) : Math.max(0, at - 1);
        const target = cells[next]!;
        e.preventDefault();
        target.focus({ preventScroll: true });
        target.scrollIntoView?.({ block: 'start' });
      } else if (e.key === '?' || (e.code === 'Slash' && e.shiftKey)) {
        // `?`, also by its physical key: Shift+Slash types "," on a Russian layout.
        e.preventDefault();
        setShortcuts(true);
      } else if ((e.key === '/' || e.code === 'Slash') && aside) {
        // `/`, also by its physical key: Slash types "." on a Russian layout.
        e.preventDefault();
        openTutor(document.activeElement instanceof HTMLElement ? document.activeElement : null);
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [aside, tutorOpen, openTutor, closeTutor]);

  const renderCell = (cell: NbCell, id: string, extra: { className?: string; children?: ReactNode } = {}) => (
    <CellView
      key={id}
      cell={cell}
      id={id}
      label={labels.get(id)}
      titles={titles}
      run={state.cells[id]}
      controller={controller}
      resetting={state.resetting}
      loadEditor={loadEditor}
      active={focused === id}
      onAsk={aside ? askAbout : undefined}
      {...extra}
    />
  );

  return (
    <div className="lesson" lang={lang} ref={root}>
      <header className="lesson-header">
        {crumbs}
        {typeof title === 'string' && <h1>{title}</h1>}
        <div className="lesson-toolbar">
          {open.length > 0 && (
            <a className="chip findings-link" data-tone="warning" href={`#cell-${open[0]!.id}`} onClick={focusHashTarget}>
              {open.length} open finding{open.length === 1 ? '' : 's'}
            </a>
          )}
          {aside && (
            <Button ref={askButton} size="sm" variant="ghost" color="neutral" onClick={(e) => openTutor(e.currentTarget)}>
              <AskIcon />
              Ask the tutor
            </Button>
          )}
          {entries.length > 0 && (
            <span className="contents-toggle">
              <Button
                size="sm"
                variant="ghost"
                color="neutral"
                aria-expanded={contentsOpen}
                aria-controls={navId}
                onClick={() => setContentsOpen((o) => !o)}
              >
                Contents
              </Button>
            </span>
          )}
          <div className="lesson-controls">
            {labs.length > 0 && (
              <Button
                size="sm"
                variant="ghost"
                color="neutral"
                disabled={state.resetting}
                onClick={() => controller.runAll(labs.map(({ c, id }) => ({ id, code: text(c.source) })))}
              >
                <PlayIcon />
                Run all
              </Button>
            )}
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
            <Menu.Root>
              <Menu.Trigger className="acme-button icon-button" data-variant="ghost" data-color="neutral" data-size="sm" aria-label="More lesson actions">
                <MoreIcon />
              </Menu.Trigger>
              <Menu.Portal>
                <Menu.Positioner align="end">
                  <Menu.Popup className="app-menu">
                    <Menu.Item className="menu-danger" disabled={state.resetting} onClick={() => setConfirm(true)}>
                      Reset kernel…
                    </Menu.Item>
                    <Menu.Item onClick={() => setShortcuts(true)}>Keyboard shortcuts</Menu.Item>
                  </Menu.Popup>
                </Menu.Positioner>
              </Menu.Portal>
            </Menu.Root>
          </div>
        </div>
      </header>
      {entries.length > 0 && (
        <nav id={navId} className="lesson-contents" aria-label="Lesson contents" data-open={contentsOpen}>
          <p className="contents-label" aria-hidden="true">
            Contents
          </p>
          <ol role="list">
            {entries.map((e, i) => (
              <li key={`${e.href}:${e.label}`}>
                <a
                  href={e.href}
                  aria-current={i === currentIndex ? 'location' : undefined}
                  onClick={(ev) => {
                    setContentsOpen(false);
                    if (e.href === '#tutor') {
                      ev.preventDefault();
                      openTutor(ev.currentTarget);
                    } else focusHashTarget(ev);
                  }}
                >
                  {e.label}
                </a>
              </li>
            ))}
          </ol>
        </nav>
      )}
      <div className="cells" onFocusCapture={(e) => focusFrom(e.target)} onClickCapture={(e) => focusFrom(e.target)}>
        {blocks.map((b) =>
          b.type === 'deck' ? (
            <RecallDeck
              key={`deck-${b.cells[0]!.id}`}
              cells={b.cells}
              render={({ cell, id }, card) => renderCell(cell, id, { className: 'cell recall-card', children: card })}
            />
          ) : (
            renderCell(b.cell, b.id)
          ),
        )}
      </div>
      {aside && (
        <div className="lesson-aside" data-open={tutorOpen}>
          {aside(focused, focused ? (labels.get(focused) ?? null) : null, { open: tutorOpen, close: closeTutor })}
        </div>
      )}
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
      <Dialog.Root open={shortcuts} onOpenChange={setShortcuts}>
        <Dialog.Portal>
          <Dialog.Backdrop />
          <Dialog.Popup>
            <Dialog.Title>Keyboard shortcuts</Dialog.Title>
            <dl className="shortcuts">
              <dt>
                <kbd>j</kbd> / <kbd>k</kbd>
              </dt>
              <dd>Next or previous cell</dd>
              <dt>
                <kbd>/</kbd>
              </dt>
              <dd>Ask the tutor</dd>
              <dt>
                <kbd>?</kbd>
              </dt>
              <dd>This list</dd>
              <dt>
                <kbd>Shift+Enter</kbd>
              </dt>
              <dd>Run a lab</dd>
              <dt>
                <kbd>Ctrl+Enter</kbd> or <kbd>⌘+Enter</kbd>
              </dt>
              <dd>Ask the tutor your question</dd>
              <dt>
                <kbd>Escape</kbd>
              </dt>
              <dd>Close a panel or dialog</dd>
            </dl>
            <div className="dialog-actions">
              <Dialog.Close>Close</Dialog.Close>
            </div>
          </Dialog.Popup>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}
