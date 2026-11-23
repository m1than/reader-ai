// Shiki with the JavaScript regex engine (no WebAssembly), python only, one Calm theme per colour scheme
// (WEB-RENDER FR-RENDER-4). Loaded lazily by Code.
import { createHighlighterCore, type HighlighterCore, type ThemeRegistration } from '@shikijs/core';
import { createJavaScriptRegexEngine } from '@shikijs/engine-javascript';
import type { Root } from 'hast';

type Palette = { comment: string; string: string; number: string; keyword: string; fn: string };

const theme = (name: string, bg: string, fg: string, c: Palette): ThemeRegistration => ({
  name,
  type: name.endsWith('dark') ? 'dark' : 'light',
  colors: { 'editor.background': bg, 'editor.foreground': fg },
  tokenColors: [
    { scope: ['comment', 'punctuation.definition.comment'], settings: { foreground: c.comment, fontStyle: 'italic' } },
    { scope: ['string', 'string.quoted'], settings: { foreground: c.string } },
    { scope: ['constant.numeric', 'constant.language'], settings: { foreground: c.number } },
    { scope: ['keyword', 'storage', 'keyword.control'], settings: { foreground: c.keyword } },
    { scope: ['entity.name.function', 'support.function'], settings: { foreground: c.fn } },
  ],
});

// Colours from the Calm tokens (accent, info, warning, success, muted text), each ≥ 4.5:1 on the surface.
const CALM_LIGHT = theme('calm-light', '#fbfaf7', '#22211e', { comment: '#5e5a52', string: '#2f6b43', number: '#85560f', keyword: '#315c5f', fn: '#2f5f86' });
const CALM_DARK = theme('calm-dark', '#222120', '#ece9e2', { comment: '#bdb8ad', string: '#8fc49d', number: '#d9b26a', keyword: '#a1c6c4', fn: '#93b6d6' });

let highlighter: Promise<HighlighterCore> | undefined;
const load = () =>
  (highlighter ??= createHighlighterCore({
    themes: [CALM_LIGHT, CALM_DARK],
    langs: [import('@shikijs/langs/python')],
    engine: createJavaScriptRegexEngine(),
  }));

// Design pass (FR-RENDER-4): both themes at once. With `defaultColor: false` every token carries `--shiki-light` and
// `--shiki-dark` (the `pre` also the `-bg` pair) as custom properties; app.css picks one by scheme, so a theme change
// recolours code without a re-render and nothing reads matchMedia here.
export const highlight = async (code: string, language: string): Promise<Root | null> => {
  if (language !== 'python') return null;
  const h = await load();
  return h.codeToHast(code, { lang: 'python', themes: { light: 'calm-light', dark: 'calm-dark' }, defaultColor: false }) as Root;
};
