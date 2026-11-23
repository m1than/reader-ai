// The slice run as the owner (e2e/live/verify-all.sh --slice → web_live.py --owner): pilot course import, search, L02
// compile, the compiled lesson in the browser, the tutor. Same TLS and passkey setup as playwright.live.config.ts;
// one serial session, no retries, long per-test budgets set inside owner.slice.ts.
import { defineConfig, devices } from '@playwright/test';
import { resolve } from 'node:path';

const out = resolve(process.env.READER_LIVE_OUT ?? 'out');
const spki = process.env.READER_CA_SPKI ?? '';

export default defineConfig({
  testDir: '.',
  testMatch: ['*.slice.ts'],
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  outputDir: resolve(out, 'playwright'),
  reporter: [['list'], ['json', { outputFile: resolve(out, 'playwright.json') }]],
  use: {
    baseURL: process.env.READER_URL ?? 'https://localhost',
    trace: 'off',
    screenshot: 'off',
    video: 'off',
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        launchOptions: spki ? { args: [`--ignore-certificate-errors-spki-list=${spki}`] } : {},
      },
    },
  ],
});
