// WEB-MANAGE FR-MANAGE-6: per-browser text size and reading width, stored next to the theme and applied as `html`
// attributes that select token values (web/src/ui/app.css); web/src/public/theme.js applies the same rule before first
// paint. An unknown stored value means the default.
export const TEXT_SIZES = ['small', 'medium', 'large'] as const;
export const WIDTHS = ['narrow', 'normal', 'wide'] as const;
export type TextSize = (typeof TEXT_SIZES)[number];
export type Width = (typeof WIDTHS)[number];

export const PREFS = {
  size: { key: 'reader-ai.text-size', attr: 'data-text-size', values: TEXT_SIZES, fallback: 'medium' },
  width: { key: 'reader-ai.width', attr: 'data-width', values: WIDTHS, fallback: 'normal' },
} as const;

type Pref = (typeof PREFS)[keyof typeof PREFS];

export const readPref = <P extends Pref>(p: P): P['values'][number] => {
  try {
    const v = localStorage.getItem(p.key);
    return (p.values as readonly string[]).includes(v ?? '') ? (v as P['values'][number]) : p.fallback;
  } catch {
    return p.fallback;
  }
};

/** Stores and applies a choice; the default removes the attribute (the stylesheet's own values apply). */
export const choosePref = (p: Pref, value: string): void => {
  const root = document.documentElement;
  if (value === p.fallback) root.removeAttribute(p.attr);
  else root.setAttribute(p.attr, value);
  try {
    localStorage.setItem(p.key, value);
  } catch {
    // storage blocked: the attribute still changed for this page
  }
};
