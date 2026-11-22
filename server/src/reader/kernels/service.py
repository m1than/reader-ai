"""Kernel sessions per user: create, list, delete, interrupt, the ADR-0022 reset and nonce tracking
(SRV-KERNEL FR-KERNEL-2..6, -9, -13, -14; INV-KERNEL-1, -5).

State lives in this process (single api replica, ADR-0012). Per user: a lock that serialises session
creation, deletion and reset; the nonce recorded at the last reset or first use; whether the service is
usable; the sessions.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from reader.kernels import upstream
from reader.kernels.protocol import KERNEL_ID, LESSON_ID
from reader.kernels.upstream import Endpoint, Reply, UpstreamError, UpstreamUnresolvable

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection

    from reader.kernels.relay import Hub

log = logging.getLogger("reader.kernels")

MAX_SESSIONS: Final = 4          # OPS-SANDBOX FR-SANDBOX-3 max_kernels
RESET_DEADLINE_S: Final = 30.0   # FR-KERNEL-5
POLL_S: Final = 0.25             # FR-KERNEL-5
_STATES: Final = frozenset({"starting", "up", "exiting"})

HttpCall = Callable[..., Awaitable[Reply]]
WsConnect = Callable[[Endpoint, str], Awaitable["ClientConnection"]]


class ServiceError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


@dataclass
class Session:
    id: str
    handle: str
    lesson_id: str
    kernel_id: str
    nonce: str
    hub: Hub | None = None


@dataclass
class UserState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    nonce: str | None = None
    usable: bool = True
    started: bool = False
    sessions: dict[str, Session] = field(default_factory=dict)
    reset_task: asyncio.Task[None] | None = None


@dataclass(frozen=True)
class State:
    nonce: str
    gateway: str


def _unavailable(e: UpstreamError) -> bool:
    """The service name does not resolve: the kernels profile is not running (FR-KERNEL-13)."""
    return isinstance(e, UpstreamUnresolvable)


class KernelService:
    def __init__(
        self,
        resolve: Callable[[str], Endpoint] = upstream.default_resolve,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        http: HttpCall = upstream.http,
        ws: WsConnect = upstream.ws_connect,
    ) -> None:
        self.resolve, self.clock, self.sleep, self.http, self.ws = resolve, clock, sleep, http, ws
        self.users: dict[str, UserState] = {}
        self._background: set[asyncio.Task[Any]] = set()

    # Helpers
    def user(self, handle: str) -> UserState:
        return self.users.setdefault(handle, UserState())

    def spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def gateway(self, handle: str, method: str, path: str, body: Any = None) -> Reply:
        ep = self.resolve(handle)
        return await self.http(method, ep.host, ep.gateway_port, path, body)

    async def state(self, handle: str) -> State:
        ep = self.resolve(handle)
        reply = await self.http("GET", ep.host, ep.control_port, "/control/state")
        data = reply.json() if reply.status == 200 else None
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("nonce"), str)
            or data.get("gateway") not in _STATES
        ):
            raise UpstreamError("bad state")
        return State(str(data["nonce"]), str(data["gateway"]))

    def get(self, handle: str, session_id: str) -> Session:
        """FR-KERNEL-1: unknown and other users' sessions give the same 404."""
        session = self.user(handle).sessions.get(session_id)
        if session is None:
            raise ServiceError(404, "not_found")
        return session

    def list(self, handle: str) -> list[dict[str, str]]:
        return [{"session_id": s.id, "lesson_id": s.lesson_id} for s in self.user(handle).sessions.values()]

    def drop(self, session: Session, code: str | None, close_code: int) -> None:
        """Remove a session and end its browser connections; never waits on the relay (no lock cycles)."""
        st = self.user(session.handle)
        if st.sessions.get(session.id) is session:
            del st.sessions[session.id]
        hub, session.hub = session.hub, None
        if hub is not None:
            hub.terminate(code, close_code)
        if code == "kernel_dead":
            self.spawn(self._delete_quietly(session))
        log.info("kernel_session_dropped handle=%s session=%s code=%s", session.handle, session.id, code)

    def drop_all(self, handle: str, code: str, close_code: int) -> None:
        for session in list(self.user(handle).sessions.values()):
            self.drop(session, code, close_code)

    async def _delete_quietly(self, session: Session) -> None:
        """Best-effort upstream DELETE after a `kernel_dead` drop (FR-KERNEL-14)."""
        with contextlib.suppress(UpstreamError):
            await self.gateway(session.handle, "DELETE", f"/api/kernels/{session.kernel_id}")

    # Sessions (FR-KERNEL-2, -3, -4, -6)
    async def create(self, handle: str, lesson_id: Any) -> tuple[int, Session]:
        if not isinstance(lesson_id, str) or not LESSON_ID.fullmatch(lesson_id):
            raise ServiceError(400, "lesson_id")
        st = self.user(handle)
        if not st.started and st.usable:
            await self.reset(handle)   # FR-KERNEL-9, coalesced with an explicit reset
        async with st.lock:
            await self._ensure(handle)   # the nonce check first (a control state read), then the cap
            existing = next((s for s in st.sessions.values() if s.lesson_id == lesson_id), None)
            if existing is not None:
                return 200, existing
            if len(st.sessions) >= MAX_SESSIONS:
                raise ServiceError(409, "too_many_kernels")
            try:
                reply = await self.gateway(handle, "POST", "/api/kernels", {"name": "python3"})
                data = reply.json() if reply.status == 201 else None
            except UpstreamError as e:
                raise self._upstream_error(e) from None
            kid = data.get("id") if isinstance(data, dict) else None
            if not isinstance(kid, str) or not KERNEL_ID.fullmatch(kid):
                raise ServiceError(502, "kernel_upstream")
            session = Session(secrets.token_hex(16), handle, lesson_id, kid, st.nonce or "")
            st.sessions[session.id] = session
            log.info("kernel_session_created handle=%s session=%s", handle, session.id)
            return 201, session

    async def delete(self, handle: str, session_id: str) -> None:
        st = self.user(handle)
        async with st.lock:
            session = self.get(handle, session_id)
            try:
                reply = await self.gateway(handle, "DELETE", f"/api/kernels/{session.kernel_id}")
            except UpstreamError as e:
                raise self._upstream_error(e) from None
            if reply.status not in (200, 204, 404):
                raise ServiceError(502, "kernel_upstream")
            if reply.status == 404:
                await self._lost_locked(session)    # FR-KERNEL-14: a known kernel id answered 404
            if st.sessions.get(session.id) is session:
                self.drop(session, None, 1000)

    async def interrupt(self, handle: str, session_id: str) -> None:
        session = self.get(handle, session_id)
        try:
            reply = await self.gateway(handle, "POST", f"/api/kernels/{session.kernel_id}/interrupt", {})
        except UpstreamError as e:
            raise self._upstream_error(e) from None
        if reply.status == 404:
            await self.upstream_lost(session)
            raise ServiceError(404, "not_found")
        if reply.status not in (200, 204):
            raise ServiceError(502, "kernel_upstream")

    def _upstream_error(self, e: UpstreamError) -> ServiceError:
        if _unavailable(e):
            return ServiceError(503, "kernel_unavailable")
        return ServiceError(502, "kernel_upstream")

    # Nonce (FR-KERNEL-2, -9, -14)
    async def _ensure(self, handle: str) -> None:
        """Called under the user's lock before a session is created or returned."""
        st = self.user(handle)
        if not st.usable or not st.started:
            raise ServiceError(503, "kernel_unavailable")
        try:
            state = await self.state(handle)
        except UpstreamError as e:
            raise self._upstream_error(e) from None
        if state.nonce == st.nonce and state.gateway == "up":
            return
        self.drop_all(handle, "kernel_reset", 1012)
        if state.nonce != st.nonce and state.gateway == "up":
            st.nonce = state.nonce
            return
        await self._await_new(handle, st.nonce)

    async def _poll_new(self, handle: str, old: str | None) -> State | None:
        """Poll until a nonce other than `old` answers with `up`. The 30 s deadline is a whole: each poll's
        own timeout is clipped to the time left (FR-KERNEL-5)."""
        deadline = self.clock() + RESET_DEADLINE_S
        while (left := deadline - self.clock()) > 0:
            try:
                state = await asyncio.wait_for(self.state(handle), left)
                if state.nonce != old and state.gateway == "up":
                    return state
            except (UpstreamError, TimeoutError):
                pass
            await self.sleep(min(POLL_S, max(deadline - self.clock(), 0)))
        return None

    async def _await_new(self, handle: str, old: str | None) -> None:
        state = await self._poll_new(handle, old)
        if state is None:
            raise ServiceError(503, "kernel_unavailable")
        self.user(handle).nonce = state.nonce

    async def upstream_lost(self, session: Session) -> None:
        """FR-KERNEL-14: the upstream WebSocket closed or failed to connect, or a known kernel id answered
        404."""
        async with self.user(session.handle).lock:
            await self._lost_locked(session)

    async def _lost_locked(self, session: Session) -> None:
        st = self.user(session.handle)
        if st.sessions.get(session.id) is not session:
            return
        try:
            state = await self.state(session.handle)
        except UpstreamError:
            self.drop_all(session.handle, "kernel_unavailable", 1013)
            return
        if state.nonce != st.nonce or state.gateway == "exiting":
            self.drop_all(session.handle, "kernel_reset", 1012)
            if state.nonce != st.nonce and state.gateway == "up":
                st.nonce = state.nonce
            return
        self.drop(session, "kernel_dead", 1000)

    # Reset (FR-KERNEL-5)
    async def reset(self, handle: str) -> None:
        """Coalesced: a reset requested while one runs for the same user gets that reset's result."""
        st = self.user(handle)
        if st.reset_task is None or st.reset_task.done():
            st.reset_task = asyncio.ensure_future(self._reset_with_lock(handle))
        await asyncio.shield(st.reset_task)

    async def _reset_with_lock(self, handle: str) -> None:
        st = self.user(handle)
        async with st.lock:
            await self._reset_locked(handle)

    async def _reset_locked(self, handle: str) -> None:
        st = self.user(handle)

        def fail() -> ServiceError:
            self.drop_all(handle, "kernel_unavailable", 1013)
            st.usable = False
            log.warning("kernel_reset_failed handle=%s", handle)
            return ServiceError(503, "kernel_unavailable")

        try:
            n0 = (await self.state(handle)).nonce
            ep = self.resolve(handle)
            reply = await self.http("POST", ep.host, ep.control_port, "/control/exit")
        except UpstreamError:
            raise fail() from None
        if reply.status != 202:
            raise fail()
        self.drop_all(handle, "kernel_reset", 1012)
        state = await self._poll_new(handle, n0)
        if state is None:
            raise fail()
        st.nonce, st.usable, st.started = state.nonce, True, True
        log.info("kernel_reset handle=%s", handle)
