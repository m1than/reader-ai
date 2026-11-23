// Spike codemirror-bundle: the lab editor's lazy chunk as WEB-NOTEBOOK FR-NOTEBOOK-9 (amended) would use it.
// Bundled with esbuild (minify, esm) by run.sh; never imported by shipped code.
import { EditorState } from '@codemirror/state';
import { EditorView, keymap, lineNumbers, highlightActiveLine, drawSelection } from '@codemirror/view';
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands';
import { syntaxHighlighting, HighlightStyle, indentUnit, bracketMatching } from '@codemirror/language';
import { python } from '@codemirror/lang-python';
import { tags } from '@lezer/highlight';

const style = HighlightStyle.define([
  { tag: tags.keyword, color: 'var(--k)' },
  { tag: tags.string, color: 'var(--s)' },
  { tag: tags.comment, color: 'var(--c)' },
]);

export const mount = (parent, doc, run) =>
  new EditorView({
    parent,
    state: EditorState.create({
      doc,
      extensions: [
        lineNumbers(), history(), drawSelection(), highlightActiveLine(), bracketMatching(), indentUnit.of('    '),
        python(), syntaxHighlighting(style),
        keymap.of([{ key: 'Shift-Enter', run }, indentWithTab, ...defaultKeymap, ...historyKeymap]),
      ],
    }),
  });
