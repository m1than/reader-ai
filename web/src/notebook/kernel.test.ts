// @trace FR-NOTEBOOK-4 FR-NOTEBOOK-5 FR-NOTEBOOK-6 FR-NOTEBOOK-7 FR-NOTEBOOK-8 INV-NOTEBOOK-1 INV-NOTEBOOK-3 VEC-NOTEBOOK-2
import fc from 'fast-check';
import { describe, expect, it, vi } from 'vitest';
import { readYaml } from '../app/testing/fixtures';
import { load } from '../app/testing/load';

// ---- fakes -------------------------------------------------------------------------------------------------------

/** A WebSocket double. Like a real socket, it rejects a send before it has opened or after it has closed. */
class FakeSocket {
  sent: Array<Record<string, unknown>> = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;
  closedBy: number | null = null;
  opened = false;
  closed = false;
  constructor(readonly sessionId: string) {}
  send(data: string) {
    if (!this.opened || this.closed) throw new Error('InvalidStateError: send on a socket that is not open');
    this.sent.push(JSON.parse(data) as Record<string, unknown>);
  }
  /** Called by the client. */
  close(code?: number) {
    this.closedBy = code ?? 1000;
    this.closed = true;
  }
  /** The harness opens the socket: mark it open, then fire onopen. */
  open() {
    if (this.closed) return;
    this.opened = true;
    this.onopen?.();
  }
  /** The harness closes the socket from the server or network side. */
  drop(code: number) {
    if (this.closed) return;
    this.closed = true;
    this.onclose?.({ code });
  }
}

type Reply = { status: number; body?: Record<string, unknown> };
const apiError = (r: Reply) => Object.assign(new Error(String(r.body?.error ?? r.status)), { status: r.status, code: String(r.body?.error ?? r.status) });

type CreateEvent = { ok: boolean; status: number; reconnecting: boolean };

const harness = async () => {
  const mod = await load(() => import('./kernel'));
  expect(mod.createKernelController).toBeTypeOf('function');
  const sockets: FakeSocket[] = [];
  const timers: Array<{ fn: () => void; ms: number }> = [];
  const queues: Record<string, Reply[]> = { create: [], interrupt: [], reset: [] };
  const defaults: Record<string, Reply> = { create: { status: 201, body: { session_id: 's0' } }, interrupt: { status: 204 }, reset: { status: 204 } };
  const calls: string[] = [];
  const creates: CreateEvent[] = [];
  let controllerRef: { getState(): { connection: string } } | null = null;
  const answer = async (kind: string) => {
    calls.push(kind);
    const reconnecting = controllerRef?.getState().connection === 'reconnecting';
    const r = queues[kind]!.shift() ?? defaults[kind]!;
    if (kind === 'create') creates.push({ ok: r.status < 400, status: r.status, reconnecting });
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
  controllerRef = controller;
  const flush = async () => {
    for (let i = 0; i < 10; i++) await Promise.resolve();
  };
  const current = () => sockets.at(-1);
  /** The socket the client still uses: the newest one, unless it is closed. */
  const live = () => {
    const s = current();
    return s && !s.closed ? s : undefined;
  };
  const openCurrent = async () => {
    await flush();
    live()?.open();
    await flush();
  };
  const executes = () => sockets.flatMap((s) => s.sent).filter((m) => (m.header as { msg_type: string }).msg_type === 'execute_request');
  return { mod, controller, sockets, timers, queues, calls, creates, navigate, notify, flush, current, live, openCurrent, executes };
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
    } else if (step.user === 'retry') {
      tries = 0;
      controller.retry();
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
      h.current()?.drop(Number(step.close));
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
          if (tries === target && step.result === 'open') s.open();
          else s.drop(1006);
          await h.flush();
        }
      }
    }
    // assertions
    const state = controller.getState();
    const where = `${seq.id} @ ${JSON.stringify(step)}`;
    for (const [cell, expected] of Object.entries((step.cell_state ?? {}) as Record<string, string>))
      expect(state.cells[cell]?.state ?? 'idle', where).toBe(expected);
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

describe('execute message shape and outputs (FR-NOTEBOOK-5)', () => {
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

  it('checks and coerces output messages; malformed ones are dropped', async () => {
    const h = await harness();
    h.controller.run('c1', 'x');
    await h.openCurrent();
    const send = (msg_type: string, content: unknown) =>
      h.live()!.onmessage!({ data: JSON.stringify({ channel: 'iopub', header: { msg_type, msg_id: 'k' }, parent_header: { msg_id: 'm1' }, content }) });
    send('stream', { name: 'evil', text: ['a', 'b'] });
    send('stream', { name: 'stdout', text: 42 });
    send('display_data', { data: 'not a bundle' });
    send('error', { ename: 1, evalue: null, traceback: ['ok', 2] });
    await h.flush();
    expect(h.controller.getState().cells.c1?.outputs).toEqual([
      { output_type: 'stream', name: 'stdout', text: 'ab' },
      { output_type: 'error', ename: '', evalue: '', traceback: ['ok'] },
    ]);
  });
});

describe('reset (FR-NOTEBOOK-7)', () => {
  it('a run is ignored while a reset runs; a failed reset is offered again', async () => {
    const h = await harness();
    let release: (v?: unknown) => void = () => undefined;
    let fail: (e: unknown) => void = () => undefined;
    let pending = new Promise<unknown>((r) => (release = r));
    const mod = h.mod;
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
    expect(c.getState()).toMatchObject({ resetting: false, resetFailed: false });
    pending = new Promise<unknown>((_, rej) => (fail = rej));
    c.reset();
    fail(apiError({ status: 503, body: { error: 'kernel_unavailable' } }));
    await h.flush();
    expect(c.getState()).toMatchObject({ resetting: false, resetFailed: true });
  });
});

// ---- INV-NOTEBOOK-1 property against a reference model -----------------------------------------------------------

const TERMINAL = new Set(['done', 'error', 'interrupted', 'timed_out', 'not_run', 'lost']);
const STATES = new Set(['idle', 'queued', 'running', ...TERMINAL]);
const LOST_NOTICES = new Set(['kernel_restarted', 'kernel_reset', 'kernel_dead', 'kernel_unavailable']);
const CELLS = ['c1', 'c2', 'c3'];

/** The reference model of FR-NOTEBOOK-5 and FR-NOTEBOOK-8 for cell states, the FIFO and Run-all groups. */
class Model {
  cells = new Map<string, string>();
  fifo: Array<{ id: string; group: number | null }> = [];
  slot: { id: string; msgId: string; group: number | null } | null = null;
  timedOut = false;
  interruptFor: string | null = null;
  private groupSeq = 0;
  state = (id: string) => this.cells.get(id) ?? 'idle';
  run(id: string, group: number | null = null) {
    if (this.state(id) === 'queued' || this.state(id) === 'running') return;
    this.cells.set(id, 'queued');
    this.fifo.push({ id, group });
  }
  runAll() {
    const g = ++this.groupSeq;
    for (const id of CELLS) this.run(id, g);
  }
  /** The client sent an execute for `code`: it must be the FIFO head, with the slot free. */
  sent(code: string, msgId: string): boolean {
    const head = this.fifo[0];
    if (this.slot || !head || head.id !== code) return false;
    this.fifo.shift();
    this.slot = { id: head.id, msgId, group: head.group };
    this.cells.set(head.id, 'running');
    this.timedOut = false;
    return true;
  }
  lost() {
    if (this.slot) this.cells.set(this.slot.id, 'lost');
    for (const q of this.fifo) this.cells.set(q.id, 'idle');
    this.fifo = [];
    this.slot = null;
    this.interruptFor = null;
  }
  reply(status: string, ename: string) {
    if (!this.slot) return;
    const s = this.slot;
    const final =
      status === 'ok' ? (this.timedOut ? 'timed_out' : 'done')
      : status === 'aborted' ? 'not_run'
      : this.timedOut ? 'timed_out'
      : this.interruptFor === s.msgId && ename === 'KeyboardInterrupt' ? 'interrupted'
      : 'error';
    this.cells.set(s.id, final);
    if (status === 'error' && s.group !== null) {
      this.fifo = this.fifo.filter((q) => {
        if (q.group !== s.group) return true;
        this.cells.set(q.id, 'idle');
        return false;
      });
    }
    this.slot = null;
    this.interruptFor = null;
  }
}

describe('state machine invariant (INV-NOTEBOOK-1)', () => {
  it('matches the reference model for random actions, messages, closes and HTTP failures', async () => {
    const action = fc.oneof(
      fc.record({ t: fc.constant('run' as const), cell: fc.constantFrom(...CELLS) }),
      fc.record({ t: fc.constant('runAll' as const) }),
      fc.record({ t: fc.constant('open' as const) }),
      fc.record({
        t: fc.constant('reply' as const),
        status: fc.constantFrom('ok', 'error', 'aborted'),
        ename: fc.constantFrom('E', 'KeyboardInterrupt'),
        which: fc.constantFrom('slot', 'old', 'unknown'),
      }),
      fc.record({ t: fc.constant('stream' as const), which: fc.constantFrom('slot', 'unknown') }),
      fc.record({
        t: fc.constant('notice' as const),
        code: fc.constantFrom('timeout', 'busy', 'rejected', 'output_truncated', ...LOST_NOTICES),
        which: fc.constantFrom('slot', 'unknown', 'none'),
      }),
      fc.record({ t: fc.constant('close' as const), code: fc.constantFrom(1000, 1003, 1006, 1009, 1011, 1012, 1013, 4401), old: fc.boolean() }),
      fc.record({ t: fc.constant('timer' as const) }),
      fc.record({ t: fc.constant('failCreate' as const), status: fc.constantFrom(401, 409, 502, 503) }),
      fc.record({ t: fc.constant('interrupt' as const), status: fc.constantFrom(204, 404) }),
      fc.record({ t: fc.constant('retry' as const) }),
    );
    await fc.assert(
      fc.asyncProperty(fc.array(action, { maxLength: 40 }), async (actions) => {
        const h = await harness();
        const m = new Model();
        const everSent: string[] = [];
        let seenSends = 0;
        let seenCreates = 0;
        let seenGiveUps = 0;
        for (const a of actions) {
          const slot = h.controller.getState().slot;
          const parentFor = (which: string) => (which === 'slot' ? (slot?.msgId ?? 'none') : which === 'old' ? (everSent[0] ?? 'none') : 'zz');
          const sock = h.live();
          switch (a.t) {
            case 'run':
              m.run(a.cell);
              h.controller.run(a.cell, a.cell);
              break;
            case 'runAll':
              m.runAll();
              h.controller.runAll(CELLS.map((id) => ({ id, code: id })));
              break;
            case 'open':
              sock?.open();
              break;
            case 'reply':
              if (slot && sock && parentFor(a.which) === slot.msgId) m.reply(a.status, a.ename);
              sock?.onmessage?.({
                data: JSON.stringify({ channel: 'shell', header: { msg_type: 'execute_reply', msg_id: 'r' }, parent_header: { msg_id: parentFor(a.which) }, metadata: {}, content: { status: a.status, ename: a.ename, execution_count: 1 } }),
              });
              break;
            case 'stream':
              sock?.onmessage?.({
                data: JSON.stringify({ channel: 'iopub', header: { msg_type: 'stream', msg_id: 's' }, parent_header: { msg_id: parentFor(a.which) }, metadata: {}, content: { name: 'stdout', text: 'x' } }),
              });
              break;
            case 'notice': {
              const p = a.which === 'none' ? null : parentFor(a.which);
              if (sock) {
                const forSlot = slot !== null && p === slot.msgId;
                if (a.code === 'timeout' && forSlot) m.timedOut = true;
                if ((a.code === 'busy' || a.code === 'rejected') && forSlot) m.lost();
                if (LOST_NOTICES.has(a.code)) m.lost();
              }
              sock?.onmessage?.({ data: JSON.stringify({ channel: 'reader', header: { msg_type: 'reader_notice', msg_id: 'n' }, parent_header: {}, content: { code: a.code, parent_msg_id: p } }) });
              break;
            }
            case 'close':
              if (a.old) {
                h.sockets.at(-2)?.onclose?.({ code: a.code });
              } else if (sock) {
                m.lost();
                sock.drop(a.code);
              }
              break;
            case 'timer':
              h.timers.shift()?.fn();
              break;
            case 'failCreate':
              h.queues.create!.push({ status: a.status, body: { error: `e${a.status}` } });
              break;
            case 'interrupt':
              // The client calls the API only while an execute is in flight on a session.
              if (slot && h.controller.getState().session) {
                h.queues.interrupt!.push({ status: a.status });
                m.interruptFor = slot.msgId;
                if (a.status === 404) m.lost();
              }
              h.controller.interrupt();
              break;
            case 'retry':
              h.controller.retry();
              break;
          }
          await h.flush();
          // asynchronous lost events: a failed session POST on Run, a 401 in a reconnect try, giving up
          for (const ev of h.creates.slice(seenCreates)) if (!ev.ok && (!ev.reconnecting || ev.status === 401)) m.lost();
          seenCreates = h.creates.length;
          const giveUps = h.notify.mock.calls.filter(([c]) => c === 'connect_failed').length;
          for (let i = seenGiveUps; i < giveUps; i++) m.lost();
          seenGiveUps = giveUps;
          // sends: each must be the FIFO head with the slot free, with the FR-NOTEBOOK-5 shape
          const sends = h.executes();
          for (const msg of sends.slice(seenSends)) {
            const header = msg.header as Record<string, unknown>;
            const content = msg.content as Record<string, unknown>;
            if (msg.channel !== 'shell' || content.allow_stdin !== false || header.version !== '5.3') return false;
            if (!m.sent(String(content.code), String(header.msg_id))) return false;
            everSent.push(String(header.msg_id));
          }
          seenSends = sends.length;
          // the client's cell states equal the model's
          const st = h.controller.getState();
          for (const id of CELLS) {
            const got = st.cells[id]?.state ?? 'idle';
            if (!STATES.has(got) || got !== m.state(id)) return false;
          }
          if ((st.slot?.cellId ?? null) !== (m.slot?.id ?? null)) return false;
        }
        return true;
      }),
      { numRuns: 300 },
    );
  });
});
