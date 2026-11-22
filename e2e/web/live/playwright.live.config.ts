// Live browser checks (e2e/live/verify-all.sh). Outside the spec gate like e2e/live: the files are `*.live.ts`, not
// `*.spec.ts`, because they check the running stack rather than one module's spec.
//
// Two modes:
//   live (default)      — https://localhost behind Caddy, driven by e2e/live/lib/web_live.py, which passes
//                         READER_BOOTSTRAP_TOKEN, READER_CA_SPKI, READER_LIVE_OUT in the test process environment only.
//   READER_LIVE_OFFLINE=1 — the SPA against web/test-server (stubbed API, fixture W01) to catch selector and flow bugs
//                         without a stack; the stack-only checks are skipped.
//
// TLS: Chromium trusts Caddy's local root through --ignore-certificate-errors-spki-list with the root's SPKI hash
// (any certificate in the chain may match), so the page is a secure context without certificate errors and WebAuthn
// runs. The TLS chain itself is checked separately against the root with Node's verifier (NODE_EXTRA_CA_CERTS).
import { defineConfig, devices } from '@playwright/test';
import { resolve } from 'node:path';

const offline = process.env.READER_LIVE_OFFLINE === '1';
const out = resolve(process.env.READER_LIVE_OUT ?? 'out');
const spki = process.env.READER_CA_SPKI ?? '';
const webDir = process.env.READER_LIVE_WEB_DIR ?? resolve(import.meta.dirname, '../../../web');

export default defineConfig({
  testDir: '.',
  testMatch: ['*.live.ts'],
  fullyParallel: false,
  workers: 1,
  retries: 0, // never retry an assertion; readiness waits live inside the tests, each bounded
  timeout: 120_000,
  expect: { timeout: 15_000 },
  outputDir: resolve(out, 'playwright'),
  reporter: [['list'], ['json', { outputFile: resolve(out, 'playwright.json') }]],
  use: {
    baseURL: offline ? 'http://127.0.0.1:4173' : (process.env.READER_URL ?? 'https://localhost'),
    // Traces are taken by the suite itself (context.tracing) after the bootstrap step, so the bootstrap token never
    // enters a trace; screenshots are taken explicitly per check.
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
  webServer: offline
    ? { command: 'node test-server/server.mjs', cwd: webDir, url: 'http://127.0.0.1:4173/', reuseExistingServer: false }
    : undefined,
});
