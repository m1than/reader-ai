// [browser] tests (WEB-UI, WEB-RENDER, SRV-TUTOR's Ask panel, WEB-PLAN's /import): run on request, not in
// tools/ci-local.sh.
//   npm run build && npm run build-storybook && npm run e2e
// (`npm run e2e` sets NODE_PATH so specs under e2e/web resolve web/node_modules, e.g. @axe-core/playwright.)
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: '../e2e/web',
  testMatch: ['ui/**/*.spec.ts', 'render/**/*.spec.ts', 'tutor/**/*.spec.ts', 'import/**/*.spec.ts'],
  fullyParallel: false,
  reporter: 'list',
  use: { baseURL: 'http://127.0.0.1:4173' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: { command: 'node test-server/server.mjs', url: 'http://127.0.0.1:4173/', reuseExistingServer: false },
});
