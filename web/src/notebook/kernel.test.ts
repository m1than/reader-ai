// @trace FR-NOTEBOOK-4 FR-NOTEBOOK-5 FR-NOTEBOOK-6 FR-NOTEBOOK-7 FR-NOTEBOOK-8 INV-NOTEBOOK-1 INV-NOTEBOOK-3 VEC-NOTEBOOK-2
import fc from 'fast-check';
import { describe, expect, it, vi } from 'vitest';
import { readYaml } from '../app/testing/fixtures';
import { load } from '../app/testing/load';

// ---- fakes -------------------------------------------------------------------------------------------------------

class FakeSocket {
  sent: Array<Record<string, unknown>> = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;
  closedBy: number | null = null;
  constructor(readonly sessionId: string) {}
  send(data: string) {
    this.sent.push(JSON.parse(data) as Record<string, unknown>);
  }
  close(code?: number) {
    this.closedBy = code ?? 1000;
  }
}

type Reply = { status: number; body?: Record<string, unknown> };
const apiError = (r: Reply) => Object.assign(new Error(String(r.body?.error ?? r.status)), { status: r.status, code: String(r.body?.error ?? r.status) });

const harness = async () => {
  const mod = await load(() => import('./kernel'));
  expect(mod.createKernelController).toBeTypeOf('function');
  const sockets: FakeSocket[] = [];
  const timers: Array<{ fn: () => void; ms: number }> = [];
  const queues: Record<string, Reply[]> = { create: [], interrupt: [], reset: [] };
  const defaults: Record<string, Reply> = { create: { status: 201, body: { session_id: 's0' } }, interrupt: { status: 204 }, reset: { status: 204 } };
  const calls: string[] = [];
  const answer = async (kind: string) => {
    calls.push(kind);
    const r = queues[kind]!.shift() ?? defaults[kind]!;
    if (r.status >= 400) throw apiError(r);
    return r.body as never;
  };
  const navigate = vi.fn();
  const notify = vi.fn();
  let n = 0;
  const controller = mod.createKernelController({
    lessonId: 'w01',
    api: {
      createSession: () => answer('create'),
      interrupt: () => answer('interrupt'),
      resetKernel: () => answer('reset'),
    },
    openSocket: (sid: string) => {
      const s = new FakeSocket(sid);
      sockets.push(s);
      return s;
    },
    schedule: (fn: () => void, ms: number) => {
      timers.push({ fn, ms });
    },
    navigateToSignIn: navigate,
    notify,
    newMsgId: () => `m${++n}`,
    now: () => '2026-11-22T00:00:00.000Z',
  });
  const flush = async () => {
    for (let i = 0; i < 10; i++) await Promise.resolve();
  };
  const current = () => sockets.at(-1);
  const openCurrent = async () => {
    await flush();
    const s = current();
    if (s && s.onopen) s.onopen();
    await flush();
  };
  const executes = () => sockets.flatMap((s) => s.sent).filter((m) => (m.header as { msg_type: string }).msg_type === 'execute_request');
  return { mod, controller, sockets, timers, queues, calls, navigate, notify, flush, current, openCurrent, executes };
};

// ---- VEC-NOTEBOOK-2 replay ---------------------------------------------------------------------------------------

type Step = Record<string, unknown>;
type Sequence = { id: string; steps: Step[] };
const sequences = () => readYaml<{ sequences: Sequence[] }>('spec/fixtures/WEB-NOTEBOOK/session.yaml').sequences;

const summary = (o: Record<string, unknown>): string =>
  o.output_type === 'stream' ? `${String(o.name)}:${String(o.text)}` : String((o.data as Record<string, unknown> | undefined)?.['text/plain'] ?? o.output_type);

const replay = async (seq: Sequence) => {
  const h = await harness();
  const { controller } = h;
  const ids = () => h.executes().map((m) => (m.header as { msg_id: string }).msg_id);
  const map = (fixtureId: unknown) => {
    if (fixtureId === null || fixtureId === undefined) return null;
    const m = /^e(\d+)$/.exec(String(fixtureId));
    return m ? (ids()[Number(m[1]) - 1] ?? `e${m[1]}-unsent`) : `unknown-${String(fixtureId)}`;
  };
  const first = seq.steps[0] ?? {};
  if (!(first.user === 'run' || first.user === 'run_all')) {
    controller.run('c1', 'c1');
    await h.openCurrent();
  }
  let tries = 0;
  let previous: FakeSocket | undefined;
  for (const step of seq.steps) {
    const http = step.http as { request: string; status: number; body?: Record<string, unknown> } | undefined;
    const kindOf = (req: string) => (req.endsWith('/interrupt') ? 'interrupt' : req.endsWith('/reset') ? 'reset' : 'create');
    if (http && step.reconnect_try === undefined) h.queues[kindOf(http.request)]!.push({ status: http.status, ...(http.body ? { body: http.body } : {}) });
    if (step.user === 'run') {
      controller.run(String(step.cell), String(step.cell));
      await h.openCurrent();
    } else if (step.user === 'run_all') {
      controller.runAll((step.cells as string[]).map((id) => ({ id, code: id })));
      await h.openCurrent();
    } else if (step.user === 'interrupt') {
      controller.interrupt();
      await h.flush();
    } else if (step.user === 'reset') {
      controller.reset();
      await h.flush();
    } else if (step.recv || step.recv_on_old_socket) {
      const r = (step.recv ?? step.recv_on_old_socket) as Record<string, unknown>;
      const content = { ...(r.content as Record<string, unknown>) };
      if ('parent_msg_id' in content) content.parent_msg_id = map(content.parent_msg_id);
      const parent = map(r.parent);
      const msg = { channel: r.channel, header: { msg_type: r.msg_type, msg_id: `k${Math.random()}` }, parent_header: parent ? { msg_id: parent } : {}, metadata: {}, content };
      const target = step.recv_on_old_socket ? previous : h.current();
      target?.onmessage?.({ data: JSON.stringify(msg) });
      await h.flush();
    } else if (step.close !== undefined) {
      previous = h.current();
      h.current()?.onclose?.({ code: Number(step.close) });
      await h.flush();
    } else if (step.reconnect_try !== undefined) {
      const target = Number(step.reconnect_try);
      while (tries < target) {
        tries += 1;
        const timer = h.timers.shift();
        expect(timer, `${seq.id}: timer for try ${tries}`).toBeDefined();
        expect(timer!.ms).toBe([1000, 2000, 4000][tries - 1]);
        if (tries === target && http) h.queues.create!.push({ status: http.status, ...(http.body ? { body: http.body } : {}) });
        const before = h.sockets.length;
        timer!.fn();
        await h.flush();
        if (h.sockets.length > before) {
          const s = h.current()!;
          if (tries === target && step.result === 'open') {
            s.onopen?.();
          } else {
            s.onclose?.({ code: 1006 });
          }
          await h.flush();
        }
      }
    }
    // assertions
    const state = controller.getState();
    const where = `${seq.id} @ ${JSON.stringify(step)}`;
    for (const [cell, expected] of Object.entries((step.cell_state ?? {}) as Record<string, string>))
      expect(state.cells[cell]?.state, where).toBe(expected);
    if ('slot' in step) expect(state.slot?.msgId ?? null, where).toBe(map(step.slot));
    for (const [cell, outs] of Object.entries((step.outputs ?? {}) as Record<string, string[]>))
      expect(state.cells[cell]?.outputs.map((o) => summary(o as never)), where).toEqual(outs);
    for (const [cell, count] of Object.entries((step.execution_count ?? {}) as Record<string, number>))
      expect(state.cells[cell]?.executionCount, where).toBe(count);
    for (const [cell, marker] of Object.entries((step.cell_marker ?? {}) as Record<string, string>))
      expect(state.cells[cell]?.marker, where).toBe(marker);
    for (const [cell, note] of Object.entries((step.cell_note ?? {}) as Record<string, string>))
      expect(state.cells[cell]?.notes, where).toContain(note);
    if (Array.isArray(step.sent)) expect(h.executes().length, where).toBe(step.sent.length);
    if (step.notice) expect(h.notify, where).toHaveBeenCalledWith(step.notice);
    if (step.session === 'forgotten') expect(state.session, where).toBeNull();
    if (step.session === 'unavailable' || step.session === 'disconnected') expect(state.connection, where).toBe(step.session);
    if (typeof step.session === 'string' && /^s\d+$/.test(step.session)) expect(state.session, where).toBe(step.session);
    if (typeof step.action === 'string' && step.action.startsWith('navigate to /api/auth/signin')) expect(h.navigate, where).toHaveBeenCalled();
  }
  return h;
};

describe('VEC-NOTEBOOK-2 sequences (FR-NOTEBOOK-5, FR-NOTEBOOK-6, FR-NOTEBOOK-8)', () => {
  for (const seq of sequences()) {
    it(seq.id, async () => {
      await replay(seq);
    });
  }

  it('a reconnect that gets 401 stops trying', async () => {
    const seq = sequences().find((s) => s.id === 'reconnect-session-expired')!;
    const h = await replay(seq);
    expect(h.timers).toHaveLength(0);
  });
});

// ---- requirements not covered by a single sequence ---------------------------------------------------------------

describe('session and socket (FR-NOTEBOOK-4, INV-NOTEBOOK-3)', () => {
  it('opening does not start a kernel; the first Run creates the session and one socket', async () => {
    const h = await harness();
    expect(h.calls).toEqual([]);
    h.controller.run('c1', 'print(1)');
    await h.openCurrent();
    h.controller.run('c2', 'print(2)');
    await h.flush();
    expect(h.calls).toEqual(['create']);
    expect(h.sockets).toHaveLength(1);
    expect(h.sockets[0]?.sessionId).toBe('s0');
  });

  it('dispose closes the socket with 1000 and later events are ignored', async () => {
    const h = await harness();
    h.controller.run('c1', 'x');
    await h.openCurrent();
    h.controller.dispose();
    expect(h.sockets[0]?.closedBy).toBe(1000);
    h.sockets[0]?.onclose?.({ code: 1006 });
    await h.flush();
    expect(h.timers).toHaveLength(0);
  });

  it('the socket URL has no query and the browser socket adds no subprotocol', async () => {
    const { socketUrl, browserSocketFactory } = await load(() => import('./kernel'));
    expect(socketUrl).toBeTypeOf('function');
    expect(socketUrl('localhost', 'a'.repeat(32))).toBe(`wss://localhost/api/kernel/sessions/${'a'.repeat(32)}/channels`);
    const args: unknown[][] = [];
    const Original = globalThis.WebSocket;
    globalThis.WebSocket = class {
      constructor(...a: unknown[]) {
        args.push(a);
      }
    } as unknown as typeof WebSocket;
    try {
      browserSocketFactory('localhost')('b'.repeat(32));
    } finally {
      globalThis.WebSocket = Original;
    }
    expect(args).toEqual([[`wss://localhost/api/kernel/sessions/${'b'.repeat(32)}/channels`]]);
  });
});

describe('execute message shape (FR-NOTEBOOK-5)', () => {
  it('sends the full header and content', async () => {
    const h = await harness();
    h.controller.run('c1', 'print(1)');
    await h.openCurrent();
    expect(h.executes()[0]).toEqual({
      channel: 'shell',
      header: { msg_id: 'm1', msg_type: 'execute_request', username: '', session: '', date: '2026-11-22T00:00:00.000Z', version: '5.3' },
      parent_header: {},
      metadata: {},
      content: { code: 'print(1)', silent: false, store_history: true, user_expressions: {}, allow_stdin: false, stop_on_error: true },
    });
  });

  it('a run is ignored while a reset runs (FR-NOTEBOOK-7)', async () => {
    const h = await harness();
    let release: () => void = () => undefined;
    h.queues.reset!.push({ status: 204 });
    const mod = h.mod;
    expect(mod.createKernelController).toBeTypeOf('function');
    const pending = new Promise<void>((r) => (release = r));
    const c = mod.createKernelController({
      lessonId: 'w01',
      api: { createSession: async () => ({ session_id: 's0' }), interrupt: async () => undefined, resetKernel: () => pending },
      openSocket: (sid: string) => new FakeSocket(sid),
      schedule: () => undefined,
      navigateToSignIn: () => undefined,
      notify: () => undefined,
      newMsgId: () => 'x',
      now: () => '',
    });
    c.reset();
    expect(c.getState().resetting).toBe(true);
    c.run('c1', 'x');
    expect(c.getState().cells.c1?.state ?? 'idle').toBe('idle');
    release();
    await h.flush();
    expect(c.getState().resetting).toBe(false);
  });
});

// ---- INV-NOTEBOOK-1 property -------------------------------------------------------------------------------------

const TERMINAL = new Set(['done', 'error', 'interrupted', 'timed_out', 'not_run', 'lost']);
const STATES = new Set(['idle', 'queued', 'running', ...TERMINAL]);

describe('state machine invariant (INV-NOTEBOOK-1)', () => {
  it('holds for random action, message and close sequences', async () => {
    const action = fc.oneof(
      fc.record({ t: fc.constant('run'), cell: fc.constantFrom('c1', 'c2', 'c3') }),
      fc.record({ t: fc.constant('runAll') }),
      fc.record({ t: fc.constant('open') }),
      fc.record({ t: fc.constant('reply'), status: fc.constantFrom('ok', 'error', 'aborted'), which: fc.constantFrom('slot', 'old', 'unknown') }),
      fc.record({ t: fc.constant('stream'), which: fc.constantFrom('slot', 'unknown') }),
      fc.record({
        t: fc.constant('notice'),
        code: fc.constantFrom('timeout', 'busy', 'rejected', 'output_truncated', 'kernel_restarted', 'kernel_reset', 'kernel_dead', 'kernel_unavailable'),
        which: fc.constantFrom('slot', 'unknown', 'none'),
      }),
      fc.record({ t: fc.constant('close'), code: fc.constantFrom(1000, 1003, 1006, 1009, 1011, 1012, 1013, 4401), old: fc.boolean() }),
      fc.record({ t: fc.constant('timer') }),
      fc.record({ t: fc.constant('interrupt') }),
    );
    await fc.assert(
      fc.asyncProperty(fc.array(action, { maxLength: 40 }), async (actions) => {
        const h = await harness();
        const everSent = new Map<string, string>();
        const terminalSeen = new Map<string, string>();
        for (const a of actions) {
          const slot = h.controller.getState().slot;
          const sock = a.t === 'close' && 'old' in a && a.old ? h.sockets.at(-2) : h.current();
          const parentFor = (which: string) =>
            which === 'slot' ? (slot?.msgId ?? 'none') : which === 'old' ? ([...everSent.keys()][0] ?? 'none') : 'zz';
          switch (a.t) {
            case 'run':
              h.controller.run(a.cell, a.cell);
              break;
            case 'runAll':
              h.controller.runAll(['c1', 'c2', 'c3'].map((id) => ({ id, code: id })));
              break;
            case 'open':
              h.current()?.onopen?.();
              break;
            case 'reply':
              sock?.onmessage?.({ data: JSON.stringify({ channel: 'shell', header: { msg_type: 'execute_reply', msg_id: 'r' }, parent_header: { msg_id: parentFor(a.which) }, metadata: {}, content: { status: a.status, execution_count: 1 } }) });
              break;
            case 'stream':
              sock?.onmessage?.({ data: JSON.stringify({ channel: 'iopub', header: { msg_type: 'stream', msg_id: 's' }, parent_header: { msg_id: parentFor(a.which) }, metadata: {}, content: { name: 'stdout', text: 'x' } }) });
              break;
            case 'notice':
              sock?.onmessage?.({ data: JSON.stringify({ channel: 'reader', header: { msg_type: 'reader_notice', msg_id: 'n' }, parent_header: {}, content: { code: a.code, parent_msg_id: a.which === 'none' ? null : parentFor(a.which) } }) });
              break;
            case 'close':
              sock?.onclose?.({ code: a.code });
              break;
            case 'timer':
              h.timers.shift()?.fn();
              break;
            case 'interrupt':
              h.controller.interrupt();
              break;
          }
          await h.flush();
          const st = h.controller.getState();
          // at most one in flight; nothing sent on a socket that never opened
          const inflight = Object.values(st.cells).filter((c) => c.state === 'running');
          if (inflight.length > 1) return false;
          for (const s of h.sockets) {
            for (const m of s.sent) {
              const header = m.header as Record<string, unknown>;
              const content = m.content as Record<string, unknown>;
              if (m.channel !== 'shell' || content.allow_stdin !== false || header.version !== '5.3') return false;
              const cell = Object.entries(st.cells).find(([id]) => content.code === id)?.[0] ?? String(content.code);
              everSent.set(String(header.msg_id), cell);
            }
          }
          for (const [id, c] of Object.entries(st.cells)) {
            if (!STATES.has(c.state)) return false;
            const prev = terminalSeen.get(id);
            if (prev && c.state !== prev && c.state !== 'running' && c.state !== 'queued' && c.state !== 'idle') return false;
            if (TERMINAL.has(c.state)) terminalSeen.set(id, c.state);
            else terminalSeen.delete(id);
          }
          if (st.slot && st.cells[st.slot.cellId]?.state !== 'running') return false;
        }
        return true;
      }),
      { numRuns: 200 },
    );
  });
});
