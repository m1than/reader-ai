// The theme choice (WEB-UI FR-UI-16): System, Light or Dark, stored under one key. web/src/public/theme.js applies the
// stored value before first paint with the same rule; this module reads, applies and stores later changes.
export type Theme = 'system' | 'light' | 'dark';

export const THEME_KEY = 'reader-ai.theme';
export const THEMES: readonly Theme[] = ['system', 'light', 'dark'];

/** The stored choice; a missing, unknown or unreadable value is System. */
export const readTheme = (storage: Pick<Storage, 'getItem'> | null | undefined): Theme => {
  try {
    const v = storage?.getItem(THEME_KEY);
    return v === 'light' || v === 'dark' || v === 'system' ? v : 'system';
  } catch {
    return 'system';
  }
};

/** System removes `data-theme` (calm.css follows prefers-color-scheme); Light and Dark set it. */
export const applyTheme = (choice: Theme, root: HTMLElement): void => {
  if (choice === 'light' || choice === 'dark') root.setAttribute('data-theme', choice);
  else root.removeAttribute('data-theme');
};

/** Applies and stores a choice; a blocked storage keeps the choice for this page only. */
export const chooseTheme = (choice: Theme, root: HTMLElement, storage: Pick<Storage, 'setItem'> | null | undefined): void => {
  applyTheme(choice, root);
  try {
    storage?.setItem(THEME_KEY, choice);
  } catch {
    // storage blocked: the attribute still changed
  }
};

const browserStorage = (): Storage | null => {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
};

export const storedTheme = (): Theme => readTheme(browserStorage());
export const setTheme = (choice: Theme): void => chooseTheme(choice, document.documentElement, browserStorage());
