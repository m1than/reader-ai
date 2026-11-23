// Spike codemirror-bundle, CSP probe: mounts one CodeMirror editor in a plain div and one in an open shadow root on
// a page served with the app's exact CSP (deploy/Caddyfile), then reports CSP violations and whether the base theme
// applied (`.cm-gutters` display). Chromium only (Playwright from web/node_modules); Safari and Firefox unmeasured.
// Usage: bash spikes/codemirror-bundle/run.sh first (it installs the packages), then
//   node spikes/codemirror-bundle/probe.mjs <dir with node_modules holding @codemirror/* and esbuild>
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';

const here = new URL('.', import.meta.url).pathname;
const deps = process.argv[2];
if (!deps) throw new Error('usage: node probe.mjs <dir with @codemirror/* and esbuild installed>');
const repo = join(here, '..', '..');
const require = createRequire(join(repo, 'web/node_modules/'));
const { chromium } = require('@playwright/test');
const csp = /Content-Security-Policy "([^"]+)"/.exec(readFileSync(join(repo, 'deploy/Caddyfile'), 'utf8'))[1];

writeFileSync(join(deps, 'probe-entry.js'), `
import { EditorState } from '@codemirror/state';
import { EditorView, lineNumbers } from '@codemirror/view';
import { python } from '@codemirror/lang-python';
const mk = (shadow) => {
  const host = document.createElement('div'); host.id = shadow ? 'shadow' : 'plain'; document.body.append(host);
  const parent = shadow ? host.attachShadow({ mode: 'open' }) : host;
  new EditorView({ parent, root: shadow ? parent : undefined, state: EditorState.create({ doc: 'def f(x):\\n    return x\\n', extensions: [lineNumbers(), python()] }) });
};
mk(false); mk(true);
`);
execFileSync(join(deps, 'node_modules/.bin/esbuild'), ['probe-entry.js', '--bundle', '--format=esm', '--outfile=probe-bundle.js', '--log-level=warning'], { cwd: deps });
const bundle = readFileSync(join(deps, 'probe-bundle.js'));
const srv = createServer((q, r) => {
  if (q.url === '/bundle.js') {
    r.writeHead(200, { 'content-type': 'text/javascript', 'content-security-policy': csp });
    return r.end(bundle);
  }
  r.writeHead(200, { 'content-type': 'text/html', 'content-security-policy': csp });
  r.end('<!doctype html><html><head><title>probe</title></head><body><script type="module" src="/bundle.js"></script></body></html>');
}).listen(0);
const browser = await chromium.launch();
const page = await browser.newPage();
await page.addInitScript(() => {
  window.__v = [];
  document.addEventListener('securitypolicyviolation', (e) => window.__v.push(`${e.violatedDirective} ${e.blockedURI}`));
});
await page.goto(`http://localhost:${srv.address().port}/`);
await page.waitForTimeout(1000);
console.log(JSON.stringify(await page.evaluate(() => {
  const plain = document.querySelector('#plain .cm-gutters');
  const root = document.querySelector('#shadow').shadowRoot;
  const shadow = root.querySelector('.cm-gutters');
  return {
    violations: window.__v,
    plain_gutters_display: plain && getComputedStyle(plain).display,
    shadow_gutters_display: shadow && getComputedStyle(shadow).display,
    shadow_adopted_sheets: root.adoptedStyleSheets.length,
  };
})));
await browser.close();
srv.close();
