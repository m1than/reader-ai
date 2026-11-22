"""The relay between browser WebSockets and one kernel's channels (SRV-KERNEL FR-KERNEL-7, -8, -10..-12, -14).

One Hub per session holds the upstream WebSocket, the execution queue (one execute_request in flight, up to 8
queued), the pending non-execute ids, per-execution output budgets and the timers. Each attached browser
connection has its own writer with a bounded send buffer. Logs carry types, counts, sizes and codes only
(INV-KERNEL-6).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol

from websockets.exceptions import ConnectionClosed

from reader.kernels.protocol import (
    BROWSER_FRAME_MAX,
    EXEC_OUTPUT_MAX,
    EXEC_TIMEOUT_S,
    INTERRUPT_GRACE_S,
    PENDING_OTHER_MAX,
    QUEUE_MAX,
    SEND_BUFFER_MAX,
    Rejected,
    inbound,
    message_type,
    notice,
    outbound,
    parent_id,
)
from reader.kernels.upstream import UpstreamError

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection

    from reader.kernels.service import KernelService, Session

log = logging.getLogger("reader.kernels")

MAX_CONNECTIONS: Final = 2       # FR-KERNEL-7
TICK_S: Final = 0.25
RECHECK_S: Final = 60.0          # FR-KERNEL-7


class BrowserConn(Protocol):
    async def receive(self) -> str | bytes | None: ...
    async def send_text(self, text: str) -> None: ...
    async def close(self, code: int) -> None: ...


@dataclass
class Attached:
    conn: BrowserConn
    recheck: Callable[[], Awaitable[bool]]
    checked_at: float
    queue: asyncio.Queue[str | int] = field(default_factory=asyncio.Queue)
    buffered: int = 0
    closed: bool = False


@dataclass
class InFlight:
    msg_id: str
    started: float
    interrupted_at: float | None = None
    escalated: bool = False


class Hub:
    def __init__(self, service: KernelService, session: Session) -> None:
        self.service, self.session = service, session
        self.attached: list[Attached] = []
        self.upstream: ClientConnection | None = None
        self.inflight: InFlight | None = None
        self.queue: deque[dict[str, Any]] = deque()
        self.pending_other: set[str] = set()
        self.output: dict[str, int] = {}
        self.truncated: set[str] = set()
        self.dropped: Counter[str] = Counter()
        self.closing = False
        self.tasks: list[asyncio.Task[Any]] = []
        self.started = asyncio.Event()

    # Lifecycle
    async def start(self) -> bool:
        """Open the upstream WebSocket (FR-KERNEL-7); on failure run FR-KERNEL-14 and report False."""
        endpoint = self.service.resolve(self.session.handle)
        try:
            self.upstream = await self.service.ws(endpoint, self.session.kernel_id)
        except UpstreamError:
            self.service.spawn(self.service.upstream_lost(self.session))
            return False
        self.tasks = [asyncio.ensure_future(self._pump()), asyncio.ensure_future(self._timer())]
        self.started.set()
        return True

    def terminate(self, code: str | None, close_code: int) -> None:
        """End every browser connection (notice first, when given) and the upstream; never blocks."""
        if self.closing:
            return
        self.closing = True
        for att in list(self.attached):
            if code is not None:
                self._enqueue(att, json.dumps(notice(code)))
            self._close(att, close_code)
        self._stop()

    def _stop(self) -> None:
        current = asyncio.current_task()
        for task in self.tasks:
            if task is not current:
                task.cancel()
        if self.upstream is not None:
            self.service.spawn(self.upstream.close())
        if self.dropped:
            log.info("kernel_relay_dropped session=%s counts=%s", self.session.id, dict(self.dropped))

    def attach(self, conn: BrowserConn, recheck: Callable[[], Awaitable[bool]]) -> Attached:
        att = Attached(conn, recheck, self.service.clock())
        self.attached.append(att)
        return att

    def detach(self, att: Attached) -> None:
        if att in self.attached:
            self.attached.remove(att)
        if not self.attached and not self.closing:
            self.closing = True
            if self.session.hub is self:
                self.session.hub = None
            self._stop()

    async def serve(self, att: Attached) -> None:
        """Run one browser connection until it ends."""
        writer = asyncio.ensure_future(self._writer(att))
        try:
            while not att.closed:
                frame = await att.conn.receive()
                if frame is None:
                    break
                if isinstance(frame, bytes):
                    self._close(att, 1003)
                    break
                if len(frame.encode("utf-8", "surrogatepass")) > BROWSER_FRAME_MAX:
                    self._close(att, 1009)
                    break
                await self._from_browser(att, frame)
        finally:
            att.closed = True
            with contextlib.suppress(asyncio.QueueFull):
                att.queue.put_nowait(-1)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer, 2.0)
            writer.cancel()
            self.detach(att)

    # Browser → kernel (FR-KERNEL-8, -10)
    def _pending_ids(self) -> set[str]:
        ids = set(self.pending_other)
        ids.update(m["header"]["msg_id"] for m in self.queue)
        if self.inflight is not None:
            ids.add(self.inflight.msg_id)
        return ids

    async def _from_browser(self, att: Attached, frame: str) -> None:
        try:
            msg = inbound(
                frame, session_id=self.session.id, handle=self.session.handle, pending=self._pending_ids()
            )
        except Rejected as r:
            self._enqueue(att, json.dumps(notice("rejected", r.msg_id)))
            self.dropped[f"rejected:{r.reason}"] += 1
            return
        msg_id = msg["header"]["msg_id"]
        if msg["header"]["msg_type"] == "execute_request":
            if self.inflight is None:
                await self._forward_execute(msg)
            elif len(self.queue) < QUEUE_MAX:
                self.queue.append(msg)
            else:
                self._enqueue(att, json.dumps(notice("busy", msg_id)))
            return
        if len(self.pending_other) >= PENDING_OTHER_MAX:
            self._enqueue(att, json.dumps(notice("busy", msg_id)))
            return
        self.pending_other.add(msg_id)
        await self._send_upstream(msg)

    async def _forward_execute(self, msg: dict[str, Any]) -> None:
        msg_id = msg["header"]["msg_id"]
        self.inflight = InFlight(msg_id, self.service.clock())
        self.output[msg_id] = 0
        await self._send_upstream(msg)

    async def _send_upstream(self, msg: dict[str, Any]) -> None:
        if self.upstream is None:
            return
        with contextlib.suppress(ConnectionClosed):
            await self.upstream.send(json.dumps(msg))

    async def _next(self) -> None:
        self.inflight = None
        if self.queue:
            await self._forward_execute(self.queue.popleft())

    def _free(self) -> None:
        """FR-KERNEL-10: on kernel_restarted or kernel_dead, free the slot and drop the queue."""
        if self.inflight is not None:
            self.output.pop(self.inflight.msg_id, None)
        self.inflight = None
        self.queue.clear()
        self.pending_other.clear()

    # Kernel → browser (FR-KERNEL-8, -11, -12)
    async def _pump(self) -> None:
        if self.upstream is None:
            return
        try:
            async for frame in self.upstream:
                await self._from_kernel(frame)
                await asyncio.sleep(0)  # let the writers drain between messages of a burst
        except ConnectionClosed as e:
            if self.closing:
                return
            # FR-KERNEL-12: a message over 4 MiB, detected while receiving, closes upstream with 1009.
            if e.sent is not None and e.sent.code == 1009:
                self._broadcast(notice("output_truncated", self.inflight.msg_id if self.inflight else None))
                self.service.drop(self.session, "kernel_dead", 1000)
                return
        if not self.closing:
            await self.service.upstream_lost(self.session)

    async def _from_kernel(self, frame: str | bytes) -> None:
        msg = outbound(frame)
        if msg is None:
            self.dropped[message_type(frame)] += 1
            return
        mt = msg["header"]["msg_type"]
        pid = parent_id(msg)
        if msg["channel"] == "iopub" and mt == "status":
            state = msg["content"].get("execution_state")
            if state in ("restarting", "dead"):
                self._broadcast(msg)
                self._free()
                self._broadcast(notice("kernel_restarted" if state == "restarting" else "kernel_dead"))
                return
        if msg["channel"] == "iopub" and pid is not None and pid in self.output:
            size = len(frame)
            if pid in self.truncated or self.output[pid] + size > EXEC_OUTPUT_MAX:
                if pid not in self.truncated:
                    self.truncated.add(pid)
                    self._broadcast(notice("output_truncated", pid))
                self.dropped["iopub:over_budget"] += 1
                return
            self.output[pid] += size
        self._broadcast(msg)
        if msg["channel"] == "shell":
            if mt == "execute_reply" and self.inflight is not None and pid == self.inflight.msg_id:
                await self._next()
            elif pid is not None:
                self.pending_other.discard(pid)

    def _broadcast(self, msg: dict[str, Any]) -> None:
        text = json.dumps(msg)
        for att in list(self.attached):
            self._enqueue(att, text)

    # Per-connection writer (FR-KERNEL-12)
    def _enqueue(self, att: Attached, text: str) -> None:
        if att.closed:
            return
        att.buffered += len(text)
        if att.buffered > SEND_BUFFER_MAX:
            # The writer may be stuck on a slow browser: close directly instead of queueing behind it.
            att.closed = True
            self.service.spawn(self._hard_close(att, 1013))
            return
        att.queue.put_nowait(text)

    async def _hard_close(self, att: Attached, code: int) -> None:
        with contextlib.suppress(Exception):
            await att.conn.close(code)
        self.detach(att)

    def _close(self, att: Attached, code: int) -> None:
        if att.closed:
            return
        att.closed = True
        att.queue.put_nowait(code)

    async def _writer(self, att: Attached) -> None:
        while True:
            item = await att.queue.get()
            if isinstance(item, int):
                if item >= 0:
                    with contextlib.suppress(Exception):
                        await att.conn.close(item)
                return
            att.buffered -= len(item)
            try:
                await att.conn.send_text(item)
            except Exception:  # a gone browser ends only its own connection
                att.closed = True
                return

    # Timers (FR-KERNEL-7, -10)
    async def _timer(self) -> None:
        while not self.closing:
            await self.service.sleep(TICK_S)
            now = self.service.clock()
            fl = self.inflight
            if fl is not None:
                if fl.interrupted_at is None and now - fl.started >= EXEC_TIMEOUT_S:
                    fl.interrupted_at = now
                    self._broadcast(notice("timeout", fl.msg_id))
                    with contextlib.suppress(Exception):
                        await self.service.interrupt(self.session.handle, self.session.id)
                elif (
                    fl.interrupted_at is not None
                    and not fl.escalated
                    and now - fl.interrupted_at >= INTERRUPT_GRACE_S
                ):
                    fl.escalated = True
                    self.service.spawn(self._reset())
            for att in list(self.attached):
                if not att.closed and now - att.checked_at >= RECHECK_S:
                    att.checked_at = now
                    ok = False
                    with contextlib.suppress(Exception):
                        ok = await att.recheck()
                    if not ok:
                        self._close(att, 4401)

    async def _reset(self) -> None:
        with contextlib.suppress(Exception):
            await self.service.reset(self.session.handle)
