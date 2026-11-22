// [browser] tests (WEB-UI, WEB-RENDER): run on request, not in tools/ci-local.sh.
//   npm run build && npm run build-storybook && npx playwright test
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: '../e2e/web',
  testMatch: ['ui/**/*.spec.ts', 'render/**/*.spec.ts'],
  fullyParallel: false,
  reporter: 'list',
  use: { baseURL: 'http://127.0.0.1:4173' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: { command: 'node test-server/server.mjs', url: 'http://127.0.0.1:4173/', reuseExistingServer: false },
});
