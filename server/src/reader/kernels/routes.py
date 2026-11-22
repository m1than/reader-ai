"""Kernel routes as data (SRV-KERNEL FR-KERNEL-1..7, -11, -13); included through reader.api.app.ROUTERS.

HTTP routes go through SRV-AUTH's dispatcher (`auth: session`). The WebSocket route authenticates itself
through `store.session_user`, after SRV-AUTH's CSRF gate has checked the Origin (FR-AUTH-12).
"""

from __future__ import annotations

import contextlib
import json

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.websockets import WebSocket

from reader.api.http import MAX_BODY_BYTES, Ctx, Route, WsRoute, error
from reader.auth import store
from reader.auth.policy import SESSION_COOKIE, Settings
from reader.kernels.protocol import notice
from reader.kernels.relay import MAX_CONNECTIONS, Hub
from reader.kernels.service import KernelService, ServiceError


def service_of(app: Starlette) -> KernelService:
    """The process-wide kernel service; tests may install one with an injected resolver first."""
    svc = getattr(app.state, "kernel_service", None)
    if not isinstance(svc, KernelService):
        svc = KernelService()
        app.state.kernel_service = svc
    return svc


def _err(e: ServiceError) -> Response:
    return error(e.status, e.code)


async def _no_payload(ctx: Ctx) -> bool:
    """FR-KERNEL-3: no body, or the body `{}` (its JSON type is checked by the CSRF gate). Reads at most
    MAX_BODY_BYTES; anything larger, unparseable or nested too deeply is not `{}`."""
    body = bytearray()
    async for chunk in ctx.request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return False
    if not body:
        return True
    try:
        return bool(json.loads(body) == {})
    except (ValueError, RecursionError):
        return False


async def create(ctx: Ctx) -> Response:
    data = await ctx.json()
    lesson_id = data.get("lesson_id") if isinstance(data, dict) else None
    try:
        status, session = await service_of(ctx.request.app).create(ctx.signed_in.handle, lesson_id)
    except ServiceError as e:
        return _err(e)
    return JSONResponse({"session_id": session.id, "lesson_id": session.lesson_id}, status_code=status)


async def list_sessions(ctx: Ctx) -> Response:
    return JSONResponse({"sessions": service_of(ctx.request.app).list(ctx.signed_in.handle)})


async def delete(ctx: Ctx) -> Response:
    if not await _no_payload(ctx):
        return error(415, "content_type")
    try:
        await service_of(ctx.request.app).delete(ctx.signed_in.handle, ctx.request.path_params["session_id"])
    except ServiceError as e:
        return _err(e)
    return Response(status_code=204)


async def interrupt(ctx: Ctx) -> Response:
    if not await _no_payload(ctx):
        return error(415, "content_type")
    try:
        session_id = ctx.request.path_params["session_id"]
        await service_of(ctx.request.app).interrupt(ctx.signed_in.handle, session_id)
    except ServiceError as e:
        return _err(e)
    return Response(status_code=204)


async def reset(ctx: Ctx) -> Response:
    if not await _no_payload(ctx):
        return error(415, "content_type")
    try:
        await service_of(ctx.request.app).reset(ctx.signed_in.handle)
    except ServiceError as e:
        return _err(e)
    return Response(status_code=204)


ROUTES: tuple[Route, ...] = (
    Route("POST", "/api/kernel/sessions", "session", create),
    Route("GET", "/api/kernel/sessions", "session", list_sessions),
    Route("DELETE", "/api/kernel/sessions/{session_id}", "session", delete),
    Route("POST", "/api/kernel/sessions/{session_id}/interrupt", "session", interrupt),
    Route("POST", "/api/kernel/reset", "session", reset),
)


class _Conn:
    """A Starlette WebSocket as the relay's BrowserConn."""

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws

    async def receive(self) -> str | bytes | None:
        message = await self.ws.receive()
        if message["type"] == "websocket.disconnect":
            return None
        if message.get("text") is not None:
            return str(message["text"])
        if message.get("bytes") is not None:
            return bytes(message["bytes"])
        return None

    async def send_text(self, text: str) -> None:
        await self.ws.send_text(text)

    async def close(self, code: int) -> None:
        await self.ws.close(code)


async def _deny(ws: WebSocket, status: int, code: str) -> None:
    if "websocket.http.response" in ws.scope.get("extensions", {}):
        await ws.send_denial_response(error(status, code))
    else:
        await ws.close(1008)


async def channels(ws: WebSocket, settings: Settings, engine: AsyncEngine) -> None:
    """FR-KERNEL-7: authenticate, find the caller's own session, attach to its relay hub."""
    if [v for k, v in ws.headers.items() if k == "origin"] != [settings.origin]:
        await _deny(ws, 403, "origin")
        return
    cookie = ws.cookies.get(SESSION_COOKIE)
    try:
        user = await store.session_user(engine, cookie)
    except (SQLAlchemyError, OSError):
        await _deny(ws, 503, "unavailable")
        return
    if user is None:
        await _deny(ws, 401, "unauthenticated")
        return
    svc = service_of(ws.app)
    if not svc.user(user.handle).usable:
        # FR-KERNEL-11: a failed reset dropped every session, so this check comes before the lookup.
        await _close_with(ws, "kernel_unavailable", 1013)
        return
    try:
        session = svc.get(user.handle, ws.path_params["session_id"])
    except ServiceError as e:
        await _deny(ws, e.status, e.code)
        return

    async def recheck() -> bool:
        return (await store.session_user(engine, cookie)) is not None

    # Reserve the slot before the first await, so the limit of 2 holds under concurrent upgrades.
    current = session.hub
    hub = Hub(svc, session) if current is None or current.closing else current
    session.hub = hub
    if len(hub.attached) >= MAX_CONNECTIONS:
        await _deny(ws, 409, "too_many_connections")
        return
    att = hub.attach(_Conn(ws), recheck)
    try:
        await ws.accept()
    except Exception:
        hub.detach(att)
        raise
    # The session may have been dropped while the handshake completed.
    if svc.user(user.handle).sessions.get(session.id) is not session or hub.closing or att.closed:
        hub.detach(att)
        await _close_with(ws, "kernel_reset", 1012)
        return
    await hub.ensure_started()   # idempotent: whichever attacher gets here first starts the hub
    await hub.serve(att)


async def _close_with(ws: WebSocket, code: str, close_code: int) -> None:
    """Accept if needed, send one notice and close (the browser cannot read a refused upgrade's status)."""
    if ws.client_state.name == "CONNECTING":
        await ws.accept()
    with contextlib.suppress(Exception):
        await ws.send_text(json.dumps(notice(code)))
        await ws.close(close_code)


WS_ROUTES: tuple[WsRoute, ...] = (WsRoute("/api/kernel/sessions/{session_id}/channels", channels),)
