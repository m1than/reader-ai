/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react()],
  // WEB-UI FR-UI-9: fonts and images are files, never inlined as data: URLs (the app CSP allows none).
  build: { assetsInlineLimit: 0 },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
    testTimeout: 20_000,
  },
});
