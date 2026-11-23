# Spike: codemirror-bundle — RESULT

**Question (V/design-pass, owner decision 2026-11-23: CodeMirror 6 for the lab editor, lazy-loaded):** How large is
the editor's lazy chunk, so that WEB-NOTEBOOK can pin a budget (NFR-NOTEBOOK-1) and the initial chunk budget
(WEB-UI NFR-UI-1, 121,153 B gzip) stays untouched?

**Answer:** 369,983 B raw, **125,244 B gzip** (level 9) for state, view, commands, language, lang-python and
lezer highlight with line numbers, history, bracket matching, a 4-space indent unit, `indentWithTab` and a
Shift+Enter binding. The budget is measured + 10 % = **137,768 B**; Green re-measures the Vite chunk and records it.

Date: 2026-11-23. Command: `bash spikes/codemirror-bundle/run.sh`. Node v22.22.1, esbuild 0.28.2 (`--bundle --minify
--format=esm`).

| Package | Version |
| --- | --- |
| @codemirror/state | 6.7.6 |
| @codemirror/view | 6.43.15 |
| @codemirror/commands | 6.11.1 |
| @codemirror/language | 6.13.1 |
| @codemirror/lang-python | 6.2.1 |
| @lezer/highlight | 1.2.5 |

**Limits:** esbuild, not the app's Vite build: Vite may split or dedupe differently (for example `@lezer/common`
shared with nothing else in the app today). The number is the order of magnitude for the budget, not the final one.

## CSP finding (decides the mount)

`style-mod` (CodeMirror's style injector, `src/style-mod.js` line 119) uses constructable stylesheets
(`adoptedStyleSheets`) only when the root has no `head`, that is a shadow root; mounted in the document it appends a
`<style>` element. Probe (`probe.mjs` in the scratchpad, Chromium from Playwright, page and bundle served with the
exact app CSP of `deploy/Caddyfile`, one editor mounted in a plain `div` and one in an open shadow root with
`EditorView({root: shadowRoot})`):

| Mount | CSP violations | `.cm-gutters` computed `display` | Sheets |
| --- | --- | --- | --- |
| document (`div`) | `style-src-elem inline` | `block` (base theme blocked: unstyled editor) | 1 `<style>` blocked |
| shadow root | none | `flex` (base theme applied) | 1 adopted sheet |

So the app mounts the editor inside a shadow root (`EditorView({parent: shadowRoot, root: shadowRoot})`); custom
properties (`--acme-*`) inherit through the shadow boundary, so the Calm theme applies with `EditorView.theme` over
`var(--acme-…)`. No CSP change (no nonce, no `'unsafe-inline'`).
