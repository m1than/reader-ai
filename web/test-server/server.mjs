#!/usr/bin/env node
// Test server for the [browser] tests (WEB-UI INV-UI-3, INV-UI-4; WEB-RENDER). Serves web/dist with the production
// Content-Security-Policy read from deploy/Caddyfile, web/storybook-static under /storybook/, and stubs the API with
// the spec fixtures (SRV-AUTH /api/me; SRV-LESSON list, lesson and revision; SRV-KERNEL session routes; no WebSocket).
//   node test-server/server.mjs [port]      (default 4173, bound to 127.0.0.1)
import { createServer } from 'node:http';
import { readFileSync, statSync } from 'node:fs';
import { extname, join, normalize, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const web = resolve(fileURLToPath(new URL('..', import.meta.url)));
const repo = resolve(web, '..');
const port = Number(process.argv[2] ?? 4173);

/** The CSP of the Caddyfile's site block, so the test runs under exactly the production policy. */
export const cspFromCaddyfile = (text) => {
  const m = /Content-Security-Policy\s+"([^"]+)"/.exec(text);
  if (!m) throw new Error('no Content-Security-Policy in deploy/Caddyfile');
  return m[1];
};

const CSP = cspFromCaddyfile(readFileSync(join(repo, 'deploy/Caddyfile'), 'utf8'));
const fixtures = JSON.parse(readFileSync(join(repo, 'spec/fixtures/WEB-UI/lessons.json'), 'utf8'));
const notebook = readFileSync(join(repo, 'spec/fixtures/WEB-NOTEBOOK/lesson-w01.ipynb'), 'utf8');
const me = { id: 1, handle: 'ivan', role: 'owner', display_name: 'Ivan', consent: { evaluation: true, tuning: false } };

const TYPES = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css',
  '.json': 'application/json', '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.ico': 'image/x-icon', '.map': 'application/json',
};

const api = (method, path) => {
  if (method === 'GET' && path === '/api/me') return [200, me];
  if (method === 'GET' && path === '/api/lessons') return [200, fixtures.list];
  const lesson = /^\/api\/lessons\/([^/]+)$/.exec(path);
  if (method === 'GET' && lesson) {
    const s = fixtures.list.lessons.find((l) => l.lesson_id === decodeURIComponent(lesson[1]));
    return s ? [200, { ...s, revisions: s.latest_sha256 ? [{ sha256: s.latest_sha256, created_at: s.updated_at }] : [] }] : [404, { error: 'not_found' }];
  }
  const rev = /^\/api\/lessons\/([^/]+)\/revisions\/([0-9a-f]{64})$/.exec(path);
  if (method === 'GET' && rev) return [200, JSON.parse(notebook)];
  if (method === 'POST' && path === '/api/kernel/sessions') return [201, { session_id: 'a'.repeat(32), lesson_id: 'w01' }];
  if (method === 'POST' && (/\/interrupt$/.test(path) || path === '/api/kernel/reset')) return [204, null];
  if (method === 'POST' && path === '/api/auth/signout') return [204, null];
  return [404, { error: 'not_found' }];
};

const file = (root, path) => {
  const target = normalize(join(root, decodeURIComponent(path)));
  if (!target.startsWith(root)) return null;
  try {
    return statSync(target).isFile() ? target : null;
  } catch {
    return null;
  }
};

export const apiRequests = [];

createServer((req, res) => {
  const url = new URL(req.url ?? '/', 'http://127.0.0.1');
  const path = url.pathname;
  if (path.startsWith('/api/')) {
    apiRequests.push(`${req.method} ${path}`);
    const [status, body] = api(req.method, path);
    res.writeHead(status, body === null ? {} : { 'Content-Type': 'application/json' });
    res.end(body === null ? undefined : JSON.stringify(body));
    return;
  }
  if (path.startsWith('/storybook/')) {
    const root = join(web, 'storybook-static');
    const f = file(root, path.slice('/storybook'.length) === '/' ? '/index.html' : path.slice('/storybook'.length));
    if (!f) {
      res.writeHead(404).end();
      return;
    }
    res.writeHead(200, { 'Content-Type': TYPES[extname(f)] ?? 'application/octet-stream' });
    res.end(readFileSync(f));
    return;
  }
  const root = join(web, 'dist');
  const f = file(root, path) ?? join(root, 'index.html');
  res.writeHead(200, { 'Content-Type': TYPES[extname(f)] ?? 'application/octet-stream', 'Content-Security-Policy': CSP });
  res.end(readFileSync(f));
}).listen(port, '127.0.0.1', () => console.log(`test server on http://127.0.0.1:${port} (CSP: ${CSP})`));
