// The lab editor (WEB-NOTEBOOK FR-NOTEBOOK-9): CodeMirror 6 as a lazy chunk, mounted in an open shadow root because
// the app CSP blocks the style element CodeMirror injects into the document (spike codemirror-bundle); in a shadow root
// its styles are an adopted stylesheet. Theme and highlighting read the Calm custom properties, which cross the shadow
// boundary, so the editor follows the theme without re-mounting.
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands';
import { python } from '@codemirror/lang-python';
import { bracketMatching, HighlightStyle, indentUnit, syntaxHighlighting } from '@codemirror/language';
import { EditorSelection, EditorState } from '@codemirror/state';
import { drawSelection, EditorView, keymap, lineNumbers } from '@codemirror/view';
import { tags } from '@lezer/highlight';

export interface EditorOptions {
  doc: string;
  /** The content element's accessible name ("Lab <n> code"). */
  label: string;
  onChange: (text: string) => void;
  onRun: (text: string) => void;
}
export interface EditorHandle {
  setDoc: (text: string) => void;
  focus: () => void;
  destroy: () => void;
}

export const ESCAPE_HINT = 'Press Escape, then Tab, to leave the editor.';

// Calm colours for tokens, the same roles as the Shiki themes (WEB-RENDER FR-RENDER-4): muted text, success, warning,
// accent and info, each a Calm token that follows the scheme.
const highlight = HighlightStyle.define([
  { tag: [tags.comment, tags.lineComment, tags.blockComment], color: 'var(--acme-text-muted)', fontStyle: 'italic' },
  { tag: [tags.string, tags.special(tags.string)], color: 'var(--acme-success)' },
  { tag: [tags.number, tags.bool, tags.null], color: 'var(--acme-warning)' },
  { tag: [tags.keyword, tags.controlKeyword, tags.definitionKeyword, tags.operatorKeyword, tags.modifier], color: 'var(--acme-accent)' },
  { tag: [tags.function(tags.variableName), tags.function(tags.definition(tags.variableName))], color: 'var(--acme-info)' },
]);

const theme = EditorView.theme({
  '&': {
    color: 'var(--acme-text)',
    backgroundColor: 'var(--acme-surface-raised)',
    fontSize: '0.875rem',
    borderRadius: '0 0 8px 8px',
  },
  '&.cm-focused': { outline: '2px solid var(--acme-focus)', outlineOffset: '2px' },
  '.cm-scroller': { fontFamily: 'var(--acme-font-mono)', lineHeight: '1.55', overflowX: 'auto' },
  '.cm-content': { caretColor: 'var(--acme-text)', padding: '8px 0' },
  '.cm-gutters': { backgroundColor: 'transparent', color: 'var(--acme-text-muted)', border: 'none' },
  '.cm-activeLine': { backgroundColor: 'transparent' },
  '&.cm-focused .cm-selectionBackground, .cm-selectionBackground': { backgroundColor: 'var(--acme-holo-soft)' },
});

// The app CSP has no 'unsafe-inline' for style attributes. When typed or inserted text replaces a selection that spans
// highlighted spans, Chromium's contenteditable engine inserts markup with a `style` attribute (to keep the replaced
// text's look), which the CSP blocks and reports (live run 2026-11-24, 7 reports per learner session). Only that case
// is taken over (code review): an `insertText` outside composition while some selection range is non-empty goes
// through a CodeMirror transaction, applied to the event's target ranges when it has any (so a platform edit whose
// target is not the selection keeps its meaning) and to the selection otherwise. Caret-only typing, composition (IME)
// and `insertReplacementText` (macOS text replacement, spelling) stay with CodeMirror and the browser.
export const INTERCEPTED_INPUT = 'insertText';
const targetChanges = (view: EditorView, event: InputEvent, text: string) => {
  const ranges = event.getTargetRanges();
  if (ranges.length === 0) return null;
  try {
    return ranges.map((r) => {
      const a = view.posAtDOM(r.startContainer, r.startOffset);
      const b = view.posAtDOM(r.endContainer, r.endOffset);
      return { from: Math.min(a, b), to: Math.max(a, b), insert: text };
    });
  } catch {
    return null; // a target outside the editor's content: fall back to the selection
  }
};
const insertThroughState = EditorView.domEventHandlers({
  beforeinput: (event, view) => {
    if (event.inputType !== INTERCEPTED_INPUT || event.isComposing || view.composing) return false;
    if (view.state.selection.ranges.every((r) => r.empty)) return false;
    const text = event.data;
    if (text === null) return false;
    event.preventDefault();
    const changes = targetChanges(view, event, text);
    if (changes) {
      const set = view.state.changes(changes);
      const end = set.mapPos(Math.max(...changes.map((c) => c.to)), 1);
      view.dispatch({ changes: set, selection: EditorSelection.cursor(end), scrollIntoView: true, userEvent: 'input.type' });
    } else {
      view.dispatch(view.state.replaceSelection(text), { scrollIntoView: true, userEvent: 'input.type' });
    }
    return true;
  },
});

let seq = 0;

export const mountEditor = (host: HTMLElement, { doc, label, onChange, onRun }: EditorOptions): EditorHandle => {
  const root = host.shadowRoot ?? host.attachShadow({ mode: 'open' });
  seq += 1;
  const hintId = `cm-hint-${seq}`;
  // The hint lives in the same shadow root as the content: aria-describedby does not cross a shadow boundary.
  const hint = document.createElement('span');
  hint.id = hintId;
  hint.className = 'cm-hint';
  hint.textContent = ESCAPE_HINT;
  Object.assign(hint.style, { position: 'absolute', width: '1px', height: '1px', overflow: 'hidden', clipPath: 'inset(50%)', whiteSpace: 'nowrap' });
  root.append(hint);
  const parent = document.createElement('div');
  root.append(parent);
  const run = (view: EditorView) => {
    onRun(view.state.doc.toString());
    return true;
  };
  const view = new EditorView({
    parent,
    root,
    state: EditorState.create({
      doc,
      extensions: [
        lineNumbers(),
        history(),
        drawSelection(),
        bracketMatching(),
        indentUnit.of('    '),
        python(),
        syntaxHighlighting(highlight),
        theme,
        insertThroughState,
        // tabindex 0: the content already is a tab stop (contenteditable); stated so axe sees the scroller's focusable
        // content (scrollable-region-focusable).
        EditorView.contentAttributes.of({ 'aria-label': label, 'aria-describedby': hintId, 'aria-multiline': 'true', tabindex: '0' }),
        keymap.of([{ key: 'Shift-Enter', run }, { key: 'Mod-Enter', run }, indentWithTab, ...defaultKeymap, ...historyKeymap]),
        EditorView.updateListener.of((u) => {
          if (u.docChanged) onChange(u.state.doc.toString());
        }),
      ],
    }),
  });
  return {
    setDoc: (text) => view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } }),
    focus: () => view.focus(),
    destroy: () => {
      view.destroy();
      parent.remove();
      hint.remove();
    },
  };
};
