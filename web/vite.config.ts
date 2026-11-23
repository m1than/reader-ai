/// <reference types="vitest/config" />
import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: [
      // rehype-katex parses KaTeX's HTML through hast-util-from-html-isomorphic, whose browser build parses with the
      // DOM (a <template> innerHTML), which the app CSP reports as inline style attributes (style-src-attr) and which
      // puts an HTML string into the document. Its default build parses in JavaScript (parse5): use it everywhere
      // (WEB-RENDER design; WEB-UI INV-UI-2, INV-UI-4).
      {
        find: /^hast-util-from-html-isomorphic$/,
        replacement: fileURLToPath(new URL('./node_modules/hast-util-from-html-isomorphic/index.js', import.meta.url)),
      },
      // micromark decodes named character references; its browser build sets `innerHTML` on a detached element.
      // Its default build uses a lookup table: no HTML string reaches the DOM (WEB-UI INV-UI-2).
      {
        find: /^decode-named-character-reference$/,
        replacement: fileURLToPath(new URL('./node_modules/decode-named-character-reference/index.js', import.meta.url)),
      },
    ],
  },
  // WEB-UI FR-UI-16 (code review B1): files copied to the build's root (theme.js) live under src/public, because the
  // production Caddy image copies src, not web/public.
  publicDir: 'src/public',
  // WEB-UI FR-UI-9: fonts and images are files, never inlined as data: URLs (the app CSP allows none).
  build: { assetsInlineLimit: 0 },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
    testTimeout: 20_000,
  },
});
