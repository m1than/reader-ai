// Vitest setup (WEB-UI FR-UI-30 "Tests pin English"): no stored interface language leaks into a test.
import { beforeEach } from 'vitest';
// The app's entry registers the Russian catalogue loaders (main.tsx); tests register the same table.
import './ru-loaders';

beforeEach(() => {
  try {
    globalThis.localStorage?.removeItem('reader-ai.locale');
  } catch {
    // no storage in this environment
  }
  if (typeof document !== 'undefined') document.documentElement.lang = 'en';
});
