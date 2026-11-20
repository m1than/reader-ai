#!/usr/bin/env node
// Opens the built gallery (vite preview) in headless Chromium and reports page errors and what rendered.
//   CHROMIUM_PATH=<chromium binary> node render-check.mjs <path to a playwright package> [url]
// Uses the Playwright and Chromium the library already has (its CI pins Chromium 1194 the same way);
// this spike installs no browser.
import { createRequire } from 'node:module';

const [playwrightDir, url = 'http://127.0.0.1:4173/'] = process.argv.slice(2);
const { chromium } = createRequire(import.meta.url)(playwrightDir);

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const errors = [];
const results = {};
for (const theme of ['light', 'dark']) {
  const page = await browser.newPage();
  page.on('pageerror', (e) => errors.push(`${theme}: ${e.message}`));
  page.on('console', (m) => m.type() === 'error' && errors.push(`${theme} console: ${m.text()} (${m.location().url})`));
  await page.goto(url);
  await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
  await page.waitForSelector('main');
  results[theme] = await page.evaluate(() => {
    const css = getComputedStyle(document.documentElement);
    const count = (selector) => document.querySelectorAll(selector).length;
    return {
      canvas: css.getPropertyValue('--acme-canvas').trim(),
      accent: css.getPropertyValue('--acme-accent').trim(),
      bodyBackground: getComputedStyle(document.body).backgroundColor,
      mainChildren: document.querySelector('main')?.children.length ?? 0,
      tabs: count('[role="tab"]'),
      grid: count('[role="grid"]'),
      gridcells: count('[role="gridcell"]'),
      tree: count('[role="tree"]'),
      progressbar: count('[role="progressbar"]'),
      switch: count('[role="switch"]'),
      slider: count('[role="slider"], input[type="range"]'),
      svg: count('svg'),
      toast: count('[role="status"], [role="alert"], [data-acme-toast], li[data-type]'),
      buttons: count('button'),
    };
  });
  await page.close();
}
await browser.close();
console.log(JSON.stringify({ errors, results }, null, 2));
// The gallery page has no favicon; the browser's request for /favicon.ico is not a library error.
process.exit(errors.filter((e) => !e.includes('/favicon.ico')).length ? 1 : 0);
