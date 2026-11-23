// [browser] tests (WEB-UI, WEB-RENDER, SRV-TUTOR's Ask panel, SRV-AUTH's pages, WEB-PLAN's /import, WEB-ACCOUNTS):
// run on request, not in tools/ci-local.sh.
//   npm run build && npm run build-storybook && npm run e2e
// (`npm run e2e` sets NODE_PATH so specs under e2e/web resolve web/node_modules, e.g. @axe-core/playwright.)
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: '../e2e/web',
  testMatch: [
    'ui/**/*.spec.ts',
    'render/**/*.spec.ts',
    'tutor/**/*.spec.ts',
    'auth/**/*.spec.ts',
    'import/**/*.spec.ts',
    'accounts/**/*.spec.ts',
    'notebook/**/*.spec.ts',
  ],
  fullyParallel: false,
  // WEB-UI FR-UI-15: the test server's compile stub keeps global state, so one worker.
  workers: 1,
  reporter: 'list',
  use: { baseURL: 'http://127.0.0.1:4173' },
  projects: [
    {
      name: 'chromium',
      testMatch: ['ui/**/*.spec.ts', 'render/**/*.spec.ts', 'tutor/**/*.spec.ts', 'import/**/*.spec.ts', 'notebook/**/*.spec.ts'],
      use: { ...devices['Desktop Chrome'] },
    },
    // SRV-AUTH FR-AUTH-21 and FR-AUTH-32, WEB-ACCOUNTS FR-ACCOUNTS-5: every state in dark and in light.
    { name: 'auth-dark', testMatch: ['auth/**/*.spec.ts', 'accounts/**/*.spec.ts'], use: { ...devices['Desktop Chrome'], colorScheme: 'dark' } },
    { name: 'auth-light', testMatch: ['auth/**/*.spec.ts', 'accounts/**/*.spec.ts'], use: { ...devices['Desktop Chrome'], colorScheme: 'light' } },
  ],
  webServer: { command: 'node test-server/server.mjs', url: 'http://127.0.0.1:4173/', reuseExistingServer: false },
});
