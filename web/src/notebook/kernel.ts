// Kernel session client over SRV-KERNEL's relay (WEB-NOTEBOOK FR-NOTEBOOK-4..8, INV-NOTEBOOK-1, INV-NOTEBOOK-3).
// One session and one WebSocket per open lesson view; at most one execute_request in flight (the slot), freed only by
// the matching execute_reply or a lost event; one reconnect procedure; every socket has a generation and events of
// an old socket are ignored. Transitions follow spec/fixtures/WEB-NOTEBOOK/session.yaml (VEC-NOTEBOOK-2).

export type CellState = 'idle' | 'queued' | 'running' | 'done' | 'error' | 'interrupted' | 'timed_out' | 'not_run' | 'lost';
export type KernelOutput = Record<string, unknown>;

export interface CellRun {
  state: CellState;
  outputs: KernelOutput[];
  executionCount: number | null;
  marker: 'timed_out' | null;
  notes: string[];
}

export type Connection = 'none' | 'connecting' | 'open' | 'reconnecting' | 'unavailable' | 'disconnected';

export interface KernelState {
  cells: Record<string, CellRun>;
  slot: { msgId: string; cellId: string } | null;
  session: string | null;
  connection: Connection;
  resetting: boolean;
  /** The last reset failed (e.g. 503); the view offers it again. */
  resetFailed: boolean;
}

export interface SocketLike {
  send(data: string): void;
  close(code?: number): void;
  onopen: (() => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: ((ev: { code: number }) => void) | null;
}

export interface KernelApi {
  createSession(lessonId: string): Promise<{ session_id: string }>;
  interrupt(sessionId: string): Promise<unknown>;
  resetKernel(): Promise<unknown>;
}

export interface KernelDeps {
  lessonId: string;
  api: KernelApi;
  openSocket(sessionId: string): SocketLike;
  schedule(fn: () => void, ms: number): void;
  navigateToSignIn(): void;
  notify(code: string): void;
  newMsgId(): string;
  now(): string;
}

export interface KernelController {
  run(cellId: string, code: string): void;
  runAll(cells: Array<{ id: string; code: string }>): void;
  interrupt(): void;
  reset(): void;
  retry(): void;
  dispose(): void;
  getState(): KernelState;
  subscribe(listener: () => void): () => void;
}

export const socketUrl = (host: string, sessionId: string): string =>
  `wss://${host}/api/kernel/sessions/${encodeURIComponent(sessionId)}/channels`;

/** The browser socket: no subprotocol, no query string (INV-NOTEBOOK-3). */
export const browserSocketFactory =
  (host: string) =>
  (sessionId: string): SocketLike =>
    new WebSocket(socketUrl(host, sessionId)) as unknown as SocketLike;

/** Backoff of the reconnect procedure (FR-NOTEBOOK-8): three tries cover a container restart. */
export const RECONNECT_DELAYS_MS = [1000, 2000, 4000] as const;

/** Close codes after which the session is forgotten and created again on the next Run, with their notice. */
const FORGET: Record<number, string> = { 1000: 'kernel_dead', 1003: 'rejected', 1009: 'output_truncated', 1012: 'kernel_reset' };
const LOST_NOTICES = new Set(['kernel_restarted', 'kernel_reset', 'kernel_dead', 'kernel_unavailable']);

const statusOf = (e: unknown): number => (typeof (e as { status?: unknown })?.status === 'number' ? (e as { status: number }).status : 0);
const codeOf = (e: unknown): string => {
  const c = (e as { code?: unknown })?.code;
  return typeof c === 'string' && c ? c : 'kernel_upstream';
};

const obj = (v: unknown): Record<string, unknown> => (v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {});

const emptyRun = (): CellRun => ({ state: 'idle', outputs: [], executionCount: null, marker: null, notes: [] });

export const createKernelController = (deps: KernelDeps): KernelController => {
  let state: KernelState = { cells: {}, slot: null, session: null, connection: 'none', resetting: false, resetFailed: false };
  const listeners = new Set<() => void>();
  let fifo: Array<{ cellId: string; code: string; group: number | null }> = [];
  const groups = new Map<string, number | null>();
  const pendingClear = new Set<string>();
  let socket: SocketLike | null = null;
  let generation = 0;
  let disposed = false;
  let interruptFor: string | null = null;
  let tries = 0;
  let groupSeq = 0;

  const emit = () => {
    state = { ...state, cells: { ...state.cells } };
    for (const l of listeners) l();
  };
  const cell = (id: string): CellRun => state.cells[id] ?? emptyRun();
  const patch = (id: string, p: Partial<CellRun>) => {
    state.cells[id] = { ...cell(id), ...p };
  };

  /** A lost event: the in-flight cell becomes lost, queued cells return to idle, the slot is freed. */
  const lost = () => {
    if (state.slot) patch(state.slot.cellId, { state: 'lost' });
    for (const q of fifo) patch(q.cellId, { state: 'idle' });
    fifo = [];
    state.slot = null;
    interruptFor = null;
  };

  const cancelGroup = (group: number | null) => {
    if (group === null) return;
    fifo = fifo.filter((q) => {
      if (q.group !== group) return true;
      patch(q.cellId, { state: 'idle' });
      return false;
    });
  };

  const failHttp = (e: unknown) => {
    if (statusOf(e) === 401) deps.navigateToSignIn();
    else deps.notify(codeOf(e));
  };

  const send = (message: Record<string, unknown>) => socket?.send(JSON.stringify(message));

  const pump = () => {
    if (disposed || state.slot || fifo.length === 0) return;
    if (!socket || state.connection !== 'open') {
      connect();
      return;
    }
    const next = fifo.shift()!;
    const msgId = deps.newMsgId();
    state.slot = { msgId, cellId: next.cellId };
    groups.set(msgId, next.group);
    pendingClear.delete(next.cellId);
    patch(next.cellId, { state: 'running', outputs: [], executionCount: null, marker: null, notes: [] });
    send({
      channel: 'shell',
      header: { msg_id: msgId, msg_type: 'execute_request', username: '', session: '', date: deps.now(), version: '5.3' },
      parent_header: {},
      metadata: {},
      content: { code: next.code, silent: false, store_history: true, user_expressions: {}, allow_stdin: false, stop_on_error: true },
    });
    emit();
  };

  const openSocketFor = (sessionId: string, isReconnect: boolean) => {
    if (disposed) return;
    state.session = sessionId;
    const mine = ++generation;
    const s = deps.openSocket(sessionId);
    socket = s;
    let opened = false;
    s.onopen = () => {
      if (mine !== generation || disposed) return;
      opened = true;
      tries = 0;
      state.connection = 'open';
      emit();
      pump();
    };
    s.onmessage = (ev) => {
      if (mine !== generation || disposed) return;
      onMessage(ev.data);
    };
    s.onclose = (ev) => {
      if (mine !== generation || disposed) return;
      generation++;
      socket = null;
      onClose(ev.code, opened, isReconnect);
    };
  };

  const connect = () => {
    if (disposed || state.connection === 'connecting' || state.connection === 'reconnecting') return;
    state.connection = 'connecting';
    emit();
    deps.api.createSession(deps.lessonId).then(
      ({ session_id }) => {
        if (disposed) return;
        openSocketFor(session_id, false);
      },
      (e: unknown) => {
        if (disposed) return;
        state.connection = 'none';
        lost();
        failHttp(e);
        emit();
      },
    );
  };

  const nextTry = () => {
    if (disposed) return;
    if (tries >= RECONNECT_DELAYS_MS.length) {
      lost();
      state.connection = 'disconnected';
      state.session = null;
      deps.notify('connect_failed');
      emit();
      return;
    }
    const delay = RECONNECT_DELAYS_MS[tries]!;
    tries += 1;
    deps.schedule(() => {
      if (disposed) return;
      deps.api.createSession(deps.lessonId).then(
        ({ session_id }) => openSocketFor(session_id, true),
        (e: unknown) => {
          if (disposed) return;
          if (statusOf(e) === 401) {
            lost();
            state.connection = 'none';
            state.session = null;
            deps.navigateToSignIn();
            emit();
            return;
          }
          nextTry();
        },
      );
    }, delay);
  };

  const startReconnect = () => {
    tries = 0;
    state.connection = 'reconnecting';
    emit();
    nextTry();
  };

  const onClose = (code: number, opened: boolean, isReconnect: boolean) => {
    lost();
    if (isReconnect && !opened) {
      emit();
      nextTry();
      return;
    }
    if (code === 4401) {
      state.connection = 'none';
      emit();
      deps.navigateToSignIn();
      return;
    }
    if (code === 1013) {
      state.connection = 'unavailable';
      state.session = null;
      deps.notify('kernel_unavailable');
      emit();
      return;
    }
    const forget = FORGET[code];
    if (forget !== undefined) {
      state.connection = 'none';
      state.session = null;
      deps.notify(forget);
      emit();
      return;
    }
    startReconnect();
  };

  /** An iopub output message as an nbformat output, with its fields checked and coerced; null when malformed. */
  const toOutput = (type: string, c: Record<string, unknown>): KernelOutput | null => {
    const str = (v: unknown): string | null => (typeof v === 'string' ? v : Array.isArray(v) && v.every((x) => typeof x === 'string') ? v.join('') : null);
    const bundle = (v: unknown): Record<string, unknown> | null => (v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null);
    switch (type) {
      case 'stream': {
        const text = str(c.text);
        const name = c.name === 'stderr' ? 'stderr' : 'stdout';
        return text === null ? null : { output_type: 'stream', name, text };
      }
      case 'display_data':
      case 'execute_result': {
        const data = bundle(c.data);
        if (!data) return null;
        const out: KernelOutput = { output_type: type, data, metadata: bundle(c.metadata) ?? {} };
        const transient = bundle(c.transient);
        if (transient && typeof transient.display_id === 'string') out.transient = { display_id: transient.display_id };
        if (type === 'execute_result') out.execution_count = typeof c.execution_count === 'number' ? c.execution_count : null;
        return out;
      }
      case 'error': {
        const tb = Array.isArray(c.traceback) ? c.traceback.filter((x): x is string => typeof x === 'string') : [];
        return { output_type: 'error', ename: str(c.ename) ?? '', evalue: str(c.evalue) ?? '', traceback: tb };
      }
      default:
        return null;
    }
  };

  const addOutput = (cellId: string, out: KernelOutput) => {
    let outputs = pendingClear.has(cellId) ? [] : cell(cellId).outputs;
    pendingClear.delete(cellId);
    const last = outputs.at(-1);
    if (out.output_type === 'stream' && last?.output_type === 'stream' && last.name === out.name) {
      outputs = [...outputs.slice(0, -1), { ...last, text: `${String(last.text)}${String(out.text)}` }];
    } else {
      outputs = [...outputs, out];
    }
    patch(cellId, { outputs });
  };

  const onMessage = (raw: unknown) => {
    let msg: Record<string, unknown>;
    try {
      msg = obj(JSON.parse(String(raw)));
    } catch {
      return;
    }
    const header = obj(msg.header);
    const content = obj(msg.content);
    const parent = obj(msg.parent_header).msg_id;
    const type = header.msg_type;
    const slot = state.slot;
    const ownsSlot = slot !== null && parent === slot.msgId;

    if (msg.channel === 'reader' && type === 'reader_notice') {
      const code = String(content.code ?? '');
      const p = content.parent_msg_id;
      const forSlot = slot !== null && p === slot.msgId;
      if (code === 'timeout' && forSlot) patch(slot.cellId, { marker: 'timed_out' });
      if (code === 'output_truncated' && forSlot) patch(slot.cellId, { notes: [...cell(slot.cellId).notes, 'output_truncated'] });
      if ((code === 'busy' || code === 'rejected') && forSlot) lost();
      if (LOST_NOTICES.has(code)) lost();
      if (code) deps.notify(code);
      emit();
      pump();
      return;
    }

    if (msg.channel === 'shell' && type === 'execute_reply') {
      if (!slot || !ownsSlot) return;
      const status = content.status;
      const marker = cell(slot.cellId).marker;
      let final: CellState;
      if (status === 'ok') final = marker === 'timed_out' ? 'timed_out' : 'done';
      else if (status === 'aborted') final = 'not_run';
      else if (marker === 'timed_out') final = 'timed_out';
      else if (interruptFor === slot.msgId && content.ename === 'KeyboardInterrupt') final = 'interrupted';
      else final = 'error';
      const count = typeof content.execution_count === 'number' ? content.execution_count : null;
      patch(slot.cellId, { state: final, executionCount: count });
      // Any failed execution stops the rest of its Run all, whatever its final label (error, interrupted, timed out).
      if (status === 'error') cancelGroup(groups.get(slot.msgId) ?? null);
      state.slot = null;
      interruptFor = null;
      emit();
      pump();
      return;
    }

    if (msg.channel !== 'iopub') return;
    if (type === 'update_display_data') {
      const id = obj(content.transient).display_id;
      if (id === undefined) return;
      for (const [cellId, run] of Object.entries(state.cells)) {
        if (!run.outputs.some((o) => obj(o.transient).display_id === id)) continue;
        patch(cellId, {
          outputs: run.outputs.map((o) => (obj(o.transient).display_id === id ? { ...o, data: content.data, metadata: content.metadata } : o)),
        });
      }
      emit();
      return;
    }
    if (!slot || !ownsSlot) return;
    const cellId = slot.cellId;
    switch (type) {
      case 'stream':
      case 'display_data':
      case 'execute_result':
      case 'error': {
        const out = toOutput(type, content);
        if (!out) return;
        addOutput(cellId, out);
        break;
      }
      case 'clear_output':
        if (content.wait === true) pendingClear.add(cellId);
        else patch(cellId, { outputs: [] });
        break;
      default:
        return;
    }
    emit();
  };

  const run = (cellId: string, code: string, group: number | null = null) => {
    if (disposed || state.resetting) return;
    const current = cell(cellId).state;
    if (current === 'running' || current === 'queued') return;
    patch(cellId, { state: 'queued' });
    fifo.push({ cellId, code, group });
    emit();
    pump();
  };

  return {
    run: (cellId, code) => run(cellId, code),
    runAll: (cells) => {
      const group = ++groupSeq;
      for (const c of cells) run(c.id, c.code, group);
    },
    interrupt: () => {
      const slot = state.slot;
      const session = state.session;
      if (!slot || !session) return;
      interruptFor = slot.msgId;
      deps.api.interrupt(session).catch((e: unknown) => {
        if (disposed) return;
        if (statusOf(e) === 404) {
          lost();
          generation++;
          socket?.close(1000);
          socket = null;
          state.session = null;
          state.connection = 'none';
          emit();
          return;
        }
        failHttp(e);
      });
    },
    reset: () => {
      if (state.resetting) return;
      state.resetting = true;
      state.resetFailed = false;
      emit();
      deps.api
        .resetKernel()
        .catch((e: unknown) => {
          if (statusOf(e) === 401) {
            deps.navigateToSignIn();
            return;
          }
          // The reset did not complete: offer it again (FR-NOTEBOOK-8, reset 503 → Retry).
          state.resetFailed = true;
          deps.notify(statusOf(e) === 503 ? 'kernel_unavailable' : codeOf(e));
        })
        .finally(() => {
          state.resetting = false;
          emit();
        });
    },
    retry: () => {
      if (disposed) return;
      // Retry after `disconnected` or `unavailable` runs the reconnect procedure itself (FR-NOTEBOOK-8).
      if (state.connection === 'disconnected' || state.connection === 'unavailable') startReconnect();
    },
    dispose: () => {
      disposed = true;
      generation++;
      socket?.close(1000);
      socket = null;
    },
    getState: () => state,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
};
