// @trace FR-IMPORT-1 FR-IMPORT-2 FR-IMPORT-3 FR-IMPORT-7 FR-IMPORT-9 INV-IMPORT-1 INV-IMPORT-2 VEC-IMPORT-1
// The upload part of /import: the drop zone, the upload with its progress, the processing steps, the converter's
// state, Cancel, Retry and Discard. Synthetic bodies from spec/fixtures/WEB-IMPORT/responses.json; no book text.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { webRoot } from '../app/testing/fixtures';
import { load } from '../app/testing/load';
import type { UploadApi, UploadView } from './api';

const fixtures = JSON.parse(readFileSync(`${webRoot}../spec/fixtures/WEB-IMPORT/responses.json`, 'utf8')) as {
  views: Record<string, UploadView>;
  converter: Record<string, Record<string, unknown>>;
  upload_errors: Array<[number, string]>;
  declared: { material_id: string; upload: null };
};
const V = fixtures.views;

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const pdfFile = (name = 'Synthetic chapter 1.pdf', head = '%PDF-1.7\n', size?: number) => {
  const file = new File([head + 'x'.repeat(64) + '%%EOF'], name, { type: name.endsWith('.pdf') ? 'application/pdf' : 'text/plain' });
  if (size !== undefined) Object.defineProperty(file, 'size', { value: size });
  return file;
};

// ---- a fake XMLHttpRequest: the test answers each request by hand ----

class FakeXhr {
  static last: FakeXhr | null = null;
  method = '';
  url = '';
  headers: Record<string, string> = {};
  body: unknown = null;
  status = 0;
  responseText = '';
  aborted = false;
  withCredentials = false;
  upload = { onprogress: null as null | ((e: { loaded: number; total: number; lengthComputable: boolean }) => void) };
  onload: null | (() => void) = null;
  onerror: null | (() => void) = null;
  onabort: null | (() => void) = null;
  constructor() {
    FakeXhr.last = this;
  }
  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }
  setRequestHeader(k: string, v: string) {
    this.headers[k.toLowerCase()] = v;
  }
  send(body: unknown) {
    this.body = body;
  }
  abort() {
    this.aborted = true;
    this.onabort?.();
  }
  respond(status: number, body: unknown) {
    this.status = status;
    this.responseText = typeof body === 'string' ? body : JSON.stringify(body);
    this.onload?.();
  }
}

const json = (status: number, body: unknown) =>
  new Response(body === null ? null : JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

const makeApi = async (routes: (url: string, init?: RequestInit) => Response | Promise<Response>, onUnauthorized = vi.fn()) => {
  const { createUploadApi } = await load(() => import('./api'));
  expect(createUploadApi).toBeTypeOf('function');
  const fetchFn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => routes(String(input), init));
  const api = createUploadApi({ fetch: fetchFn as unknown as typeof fetch, onUnauthorized, xhr: () => new FakeXhr() as unknown as XMLHttpRequest });
  return { api, fetchFn, onUnauthorized };
};

const mount = async (api: UploadApi, onReady = vi.fn()) => {
  const { UploadSections } = await load(() => import('./UploadSections'));
  expect(UploadSections).toBeTypeOf('function');
  await act(async () => {
    render(<UploadSections api={api} onReady={onReady} />);
  });
  return onReady;
};

const choose = async (files: File[]) => {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  await act(async () => {
    fireEvent.change(input, { target: { files } });
  });
};

const pressUpload = async () => {
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Upload' }));
  });
};

// ---- FR-IMPORT-1: the drop zone and the client checks ----

describe('drop zone (FR-IMPORT-1)', () => {
  it('has one button, a hidden PDF input and a drop hint; the zone itself is not a control', async () => {
    const { api } = await makeApi(() => json(404, { error: 'not_found' }));
    await mount(api);
    const zone = screen.getByRole('region', { name: 'Upload a PDF' });
    expect(within(zone).getByRole('button', { name: 'Choose a PDF' })).toBeTruthy();
    expect(within(zone).getByText('or drop it here')).toBeTruthy();
    const input = zone.querySelector('input[type="file"]') as HTMLInputElement;
    expect(input.accept).toBe('application/pdf,.pdf');
    expect(input.hidden || input.className.includes('visually-hidden') || input.style.display === 'none').toBe(true);
    expect(zone.getAttribute('role')).not.toBe('button');
    expect(zone.getAttribute('tabindex')).toBeNull();
  });

  it.each([
    ['two files', () => [pdfFile(), pdfFile('b.pdf')], 'Choose one file at a time.'],
    ['a text file', () => [pdfFile('notes.txt')], 'This file is not a PDF.'],
    ['an empty file', () => [pdfFile('a.pdf', '%PDF-1.7', 0)], 'This file is empty.'],
    ['a file over 200 MiB', () => [pdfFile('a.pdf', '%PDF-1.7', 209715201)], 'This file is larger than 200 MiB.'],
    ['a .pdf that is not a PDF', () => [pdfFile('a.pdf', 'PK\u0003\u0004')], 'This file is not a PDF.'],
  ])('refuses %s before any request', async (_name, files, text) => {
    const { api, fetchFn } = await makeApi(() => json(500, {}));
    await mount(api);
    await choose(files());
    await waitFor(() => expect(screen.getByText(text)).toBeTruthy());
    expect((screen.queryByRole('button', { name: 'Upload' }) as HTMLButtonElement | null)?.disabled ?? true).toBe(true);
    expect(fetchFn).not.toHaveBeenCalled();
    expect(FakeXhr.last === null || FakeXhr.last.method === '').toBe(true);
  });

  it('accepts a file of exactly 200 MiB and reads only its first 5 bytes', async () => {
    const { checkFile } = await load(() => import('./model'));
    expect(checkFile).toBeTypeOf('function');
    const file = pdfFile('cap.pdf', '%PDF-1.7', 209715200);
    const slice = vi.spyOn(file, 'slice');
    expect(await checkFile([file])).toBeNull();
    expect(slice.mock.calls).toEqual([[0, 5]]);
  });

  it('drops a file onto the zone and announces the drag-over state', async () => {
    const { api } = await makeApi(() => json(500, {}));
    await mount(api);
    const zone = screen.getByRole('region', { name: 'Upload a PDF' });
    await act(async () => {
      fireEvent.dragOver(zone, { dataTransfer: { files: [], types: ['Files'] } });
    });
    expect(screen.getByText('Drop to upload')).toBeTruthy();
    await act(async () => {
      fireEvent.drop(zone, { dataTransfer: { files: [pdfFile()], types: ['Files'] } });
    });
    await waitFor(() => expect(screen.getByText('Synthetic chapter 1.pdf')).toBeTruthy());
  });

  it.each([
    ['ru-RU', 'ru'],
    ['en-GB', 'en'],
    ['de-DE', 'other'],
  ])('defaults the language from %s', async (nav, value) => {
    const { defaultLanguage } = await load(() => import('./model'));
    expect(defaultLanguage).toBeTypeOf('function');
    expect(defaultLanguage(nav)).toBe(value);
  });
});

// ---- FR-IMPORT-2: the upload request and its replies ----

describe('upload (FR-IMPORT-2)', () => {
  beforeEach(() => {
    FakeXhr.last = null;
  });

  it('posts the raw PDF with the name in a header and shows determinate progress', async () => {
    const { api } = await makeApi((url) => (url.includes('/converter') ? json(200, fixtures.converter.online) : json(200, V.queued)));
    await mount(api);
    await choose([pdfFile('Книга 1.pdf')]);
    await pressUpload();
    const xhr = FakeXhr.last!;
    expect(xhr.method).toBe('POST');
    expect(xhr.url).toBe('/api/materials/uploads?language=en'); // jsdom's navigator.language is en-US
    expect(xhr.url).not.toContain('Книга');
    expect(xhr.headers['x-reader-filename']).toBe(encodeURIComponent('Книга 1.pdf'));
    expect(xhr.headers['content-type']).toBe('application/pdf');
    expect(xhr.body).toBeInstanceOf(File);
    await act(async () => xhr.upload.onprogress?.({ loaded: 40, total: 100, lengthComputable: true }));
    const bar = screen.getByRole('progressbar', { name: 'Uploading' });
    expect(bar.getAttribute('aria-valuenow')).toBe('40');
    expect(screen.getByText('40%')).toBeTruthy();
    await act(async () => xhr.respond(201, V.queued));
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Processing' })).toBeTruthy());
  });

  it('cancels a running upload by aborting the request', async () => {
    const { api } = await makeApi(() => json(200, {}));
    await mount(api);
    await choose([pdfFile()]);
    await pressUpload();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Cancel upload' }));
    });
    expect(FakeXhr.last!.aborted).toBe(true);
    expect(screen.getByRole('button', { name: 'Upload' })).toBeTruthy();
  });

  it('shows "Already imported" for a declared material', async () => {
    const { api } = await makeApi(() => json(200, {}));
    await mount(api);
    await choose([pdfFile()]);
    await pressUpload();
    await act(async () => FakeXhr.last!.respond(200, fixtures.declared));
    expect(screen.getByText('Already imported')).toBeTruthy();
  });

  it.each(fixtures.upload_errors.filter(([status]) => status !== 401))('maps %i %s to its message', async (status, code) => {
    const { message } = await load(() => import('./messages'));
    const { api } = await makeApi(() => json(200, {}));
    await mount(api);
    await choose([pdfFile()]);
    await pressUpload();
    await act(async () => FakeXhr.last!.respond(status, { error: code }));
    expect(screen.getByText(message(code))).toBeTruthy();
  });

  it('names the remedies of inbox_full, config_unavailable and inbox_invalid', async () => {
    const { message } = await load(() => import('./messages'));
    expect(message('inbox_full')).toContain('Discard failed uploads, cancel queued ones, or wait');
    expect(message('config_unavailable')).toBe('The server cannot read its configuration; check reader.yaml');
    expect(message('inbox_invalid')).toBe("The server's upload folder is not usable; see the server log");
  });

  it('sends a 401 to sign-in, a network error offers Retry, a malformed body is bad_response', async () => {
    const { message } = await load(() => import('./messages'));
    const { api, onUnauthorized } = await makeApi(() => json(200, {}));
    await mount(api);
    await choose([pdfFile()]);
    await pressUpload();
    await act(async () => FakeXhr.last!.respond(401, { error: 'unauthenticated' }));
    expect(onUnauthorized).toHaveBeenCalled();
    await pressUpload();
    await act(async () => FakeXhr.last!.onerror?.());
    expect(screen.getByText(message('network'))).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
    await pressUpload();
    await act(async () => FakeXhr.last!.respond(201, { id: 3 }));
    expect(screen.getByText(message('bad_response'))).toBeTruthy();
  });
});

// ---- FR-IMPORT-3: processing ----

const processing = async (sequence: Array<[number, unknown]>, converter: Record<string, unknown> = fixtures.converter.online!) => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  let i = 0;
  const { api, fetchFn } = await makeApi((url) => {
    if (url === '/api/materials/converter') return json(200, converter);
    if (url.startsWith('/api/materials/uploads/')) {
      const [status, body] = sequence[Math.min(i++, sequence.length - 1)]!;
      return json(status, body);
    }
    return json(404, { error: 'not_found' });
  });
  const { UploadSections } = await load(() => import('./UploadSections'));
  const onReady = vi.fn();
  await act(async () => {
    render(<UploadSections api={api} onReady={onReady} initialUpload={(sequence[0]![1] as UploadView).id ?? V.queued!.id} />);
  });
  return { fetchFn, onReady };
};

const tick = async (ms: number) => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
};

describe('processing (FR-IMPORT-3)', () => {
  it('polls every 2 s and walks the step list to ready', async () => {
    const { fetchFn, onReady } = await processing([
      [200, V.queued],
      [200, V.converting],
      [200, V.importing],
      [200, V.indexing],
      [200, V.ready],
    ]);
    const list = screen.getByRole('list', { name: 'Steps' });
    expect(within(list).getAllByRole('listitem').map((li) => li.textContent?.trim())).toEqual([
      'Uploaded', 'Converting on your Mac', 'Importing', 'Indexing for search', 'Ready',
    ]);
    await tick(2000);
    expect(screen.getByText('Page 50 of 120')).toBeTruthy();
    expect(screen.getByRole('progressbar', { name: 'Converting' }).getAttribute('aria-valuenow')).toBe(String(Math.round((50 / 120) * 100)));
    expect(within(list).getByText('Converting on your Mac').closest('li')!.getAttribute('aria-current')).toBe('step');
    await tick(2000);
    await tick(2000);
    await tick(2000);
    expect(within(list).getByText('Ready').closest('li')!.getAttribute('aria-current')).toBe('step');
    expect(onReady).toHaveBeenCalledWith('up-0123456789ab');
    const polls = fetchFn.mock.calls.filter(([u]) => String(u).startsWith('/api/materials/uploads/')).length;
    await tick(10_000);
    expect(fetchFn.mock.calls.filter(([u]) => String(u).startsWith('/api/materials/uploads/')).length).toBe(polls);
  });

  it('announces step changes only, not every page count', async () => {
    await processing([
      [200, V.converting],
      [200, { ...V.converting, pages_done: 60 }],
      [200, V.sending],
    ]);
    const status = screen.getByRole('status', { name: 'Upload progress' });
    const first = status.textContent;
    await tick(2000);
    expect(status.textContent).toBe(first);
  });

  it.each([
    ['online', 'Waiting for the converter'],
    ['cli_running', 'Waiting: a command-line conversion is running on your Mac; this upload starts when it ends'],
    ['offline', 'The converter on your Mac is not running. Start it with reader up.'],
    ['setup_missing', 'The converter is not set up. Run reader-ingest --setup.'],
  ])('while queued, shows the converter state %s', async (key, text) => {
    await processing([[200, V.queued]], fixtures.converter[key]);
    await tick(10);
    await waitFor(() => expect(screen.getByText((_c, el) => el?.tagName === 'P' && el.textContent === text)).toBeTruthy());
  });

  it('warns about strays and stale code whatever the state', async () => {
    await processing([[200, V.queued]], { ...fixtures.converter.online, strays: 2, stale: true });
    await tick(10);
    await waitFor(() => expect(screen.getByText('Earlier conversions left processes running on your Mac; restarting your Mac clears them')).toBeTruthy());
    expect(screen.getByText((_c, el) => el?.tagName === 'P' && el.textContent === 'The converter on your Mac runs older code; reader up restarts it when it is idle')).toBeTruthy();
  });

  it('confirms Cancel, deletes the upload and stops on its 404', async () => {
    const cancel = vi.fn();
    const { api } = await makeApi((url, init) => {
      if (url.endsWith('/cancel') && init?.method === 'POST') {
        cancel();
        return json(200, { cancelled: true });
      }
      if (url === '/api/materials/converter') return json(200, fixtures.converter.online);
      return cancel.mock.calls.length ? json(404, { error: 'not_found' }) : json(200, V.queued);
    });
    const { UploadSections } = await load(() => import('./UploadSections'));
    await act(async () => {
      render(<UploadSections api={api} onReady={vi.fn()} initialUpload={V.queued!.id} />);
    });
    await waitFor(() => screen.getByRole('button', { name: 'Cancel' }));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    });
    const dialog = screen.getByRole('dialog');
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel the upload' }));
    });
    expect(cancel).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.getByText('Cancelled')).toBeTruthy());
  });

  it('says the upload no longer exists on a 404 it did not cause', async () => {
    await processing([[200, V.queued], [404, { error: 'not_found' }]]);
    await tick(2000);
    await waitFor(() => expect(screen.getByText('This upload no longer exists')).toBeTruthy());
  });

  it('adds the restart-your-Mac remedy to memory_limit', async () => {
    await processing([[200, V.failed_memory]]);
    await waitFor(() => expect(screen.getByText('If files that used to convert now fail with this, restart your Mac')).toBeTruthy());
  });

  it.each([
    [202, { retry: 'requeued' }, 'Restarted'],
    [202, { retry: 'already_queued' }, 'Restarted'],
    [409, { error: 'busy' }, 'Already restarting; try again in a moment'],
  ])('Retry reply %i %j', async (status, body, text) => {
    const { api } = await makeApi((url, init) => {
      if (url.endsWith('/retry') && init?.method === 'POST') return json(status, body);
      if (url === '/api/materials/converter') return json(200, fixtures.converter.online);
      return json(200, V.failed_import_dead);
    });
    const { UploadSections } = await load(() => import('./UploadSections'));
    await act(async () => {
      render(<UploadSections api={api} onReady={vi.fn()} initialUpload={V.queued!.id} />);
    });
    await waitFor(() => screen.getByRole('button', { name: 'Retry' }));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    });
    await waitFor(() => expect(screen.getByText(text)).toBeTruthy());
  });

  it('stops polling while the page is hidden', async () => {
    const { fetchFn } = await processing([[200, V.queued]]);
    const count = () => fetchFn.mock.calls.filter(([u]) => String(u).startsWith('/api/materials/uploads/')).length;
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    const before = count();
    await tick(10_000);
    expect(count()).toBe(before);
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await tick(2000);
    expect(count()).toBeGreaterThan(before);
  });
});

// ---- INV-IMPORT-2: one message and the allowed controls per state ----

const CONTROLS: Record<string, string[]> = {
  queued: ['Cancel'],
  claimed: ['Cancel'],
  converting: ['Cancel'],
  sending: [],
  importing: [],
  indexing: [],
  ready: [],
  failed_reconvert: ['Retry', 'Discard'],
  failed_memory: ['Retry', 'Discard'],
  failed_host_lost: ['Retry', 'Discard'],
  failed_import_dead: ['Retry'],
  failed_embed_dead: ['Retry'],
  failed_no_retry: [],
};

describe('controls per state (INV-IMPORT-2)', () => {
  it('maps every fixture view to exactly its controls', async () => {
    const { controls } = await load(() => import('./model'));
    expect(controls).toBeTypeOf('function');
    for (const [key, view] of Object.entries(V)) expect([key, controls(view)]).toEqual([key, CONTROLS[key]]);
  });

  it.each(Object.keys(CONTROLS))('renders %s with one message and only its buttons', async (key) => {
    await processing([[200, V[key]]]);
    await tick(10);
    const section = screen.getByRole('region', { name: 'Processing' });
    const buttons = within(section).queryAllByRole('button').map((b) => b.textContent?.trim());
    expect(buttons.sort()).toEqual([...CONTROLS[key]!].sort());
    expect(within(section).getAllByTestId('upload-message')).toHaveLength(1);
  });
});

// ---- FR-IMPORT-7: one catalogue; the filename as text ----

describe('messages (FR-IMPORT-7)', () => {
  it('has a message for every code the page can meet, and a fallback', async () => {
    const { message, CODES } = await load(() => import('./messages'));
    expect(message).toBeTypeOf('function');
    for (const [, code] of fixtures.upload_errors) expect(CODES).toContain(code);
    for (const code of ['one_file', 'not_pdf', 'empty', 'too_large', 'network', 'bad_response', 'unknown', 'host_lost', 'convert_failed', 'memory_limit', 'work_limit', 'timeout', 'fetch_mismatch', 'inbox_missing', 'receive_failed', 'bad_reply', 'busy', 'state'])
      expect(CODES).toContain(code);
    expect(message('made-up-code')).toBe(message('unknown'));
  });

  it('shows the filename as text only', async () => {
    await processing([[200, { ...V.queued, filename: '<img src=x onerror=alert(1)>.pdf' }]]);
    await waitFor(() => expect(screen.getByText('<img src=x onerror=alert(1)>.pdf')).toBeTruthy());
    expect(document.querySelector('img')).toBeNull();
  });
});

// ---- INV-IMPORT-1: the page never reads the PDF beyond 5 bytes, no object or data URLs, no HTML ----

describe('no reading, no URLs, no HTML (INV-IMPORT-1)', () => {
  const sources = readdirSync(join(webRoot, 'src/import'))
    .filter((f) => /\.(ts|tsx)$/.test(f) && !/\.(test|stories)\.tsx?$/.test(f))
    .map((f) => [f, readFileSync(join(webRoot, 'src/import', f), 'utf8')] as const);

  it('has upload sources to scan', () => {
    expect(sources.map(([f]) => f)).toEqual(expect.arrayContaining(['api.ts', 'model.ts', 'UploadSections.tsx', 'ImportPage.tsx']));
  });

  it.each([
    ['createObjectURL', /createObjectURL/],
    ['FileReader', /\bFileReader\b/],
    ['data URL', /data:/],
    ['innerHTML', /innerHTML|dangerouslySetInnerHTML/],
    ['whole-file reads', /\.(text|arrayBuffer|stream)\(\)/],
  ])('no %s', (_name, re) => {
    const offenders = sources.filter(([, s]) => re.test(s.replace(/\.slice\(0, 5\)\.arrayBuffer\(\)/g, ''))).map(([f]) => f);
    expect(offenders).toEqual([]);
  });

  it('reads exactly the first 5 bytes once', () => {
    const reads = sources.flatMap(([, s]) => s.match(/\.slice\(0, 5\)\.arrayBuffer\(\)/g) ?? []);
    expect(reads).toHaveLength(1);
  });
});

// ---- FR-IMPORT-9: the page's CSS follows the polish rules (tokens only) ----

describe('styles (FR-IMPORT-9)', () => {
  it('uses Calm tokens, not colour literals, in web/src/import/*.css', () => {
    const css = readdirSync(join(webRoot, 'src/import')).filter((f) => f.endsWith('.css'));
    expect(css.length).toBeGreaterThan(0);
    for (const f of css) {
      const text = readFileSync(join(webRoot, 'src/import', f), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
      expect([f, text.match(/#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|oklch\(/g)]).toEqual([f, null]);
      expect([f, text.match(/gradient|backdrop-filter/g)]).toEqual([f, null]);
    }
  });
});
