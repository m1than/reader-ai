"""App factory: routers from a declared list, the CSRF gate, the session lookup, the cleanup task.

ROUTERS names the modules whose `ROUTES` the app serves (FR-AUTH-10). Stage 1 lists the api and auth routers;
SRV-LESSON adds `reader.lessons.routes`; SRV-KERNEL adds `reader.kernels.routes` in its own paired change.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Final

from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

from reader.api.http import Ctx, Route, WsRoute, error
from reader.auth import store
from reader.auth.policy import CLEANUP_INTERVAL_S, RECENT_SESSION_S, SESSION_COOKIE, Settings

ROUTERS: Final = (
    "reader.api.routes",
    "reader.auth.routes",
    "reader.lessons.routes",
    "reader.kernels.routes",
    "reader.ingest.routes",  # SRV-INGEST paired change (FR-AUTH-10 note)
    "reader.pipeline.routes",
)
ROUTES: Final[tuple[Route, ...]] = tuple(
    route for module in ROUTERS for route in importlib.import_module(module).ROUTES
)
WS_ROUTES: Final[tuple[WsRoute, ...]] = tuple(
    route for module in ROUTERS for route in getattr(importlib.import_module(module), "WS_ROUTES", ())
)

log = logging.getLogger("reader.api")
_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})


def _media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


class CsrfGate:
    """FR-AUTH-12: exact Origin on non-GET `/api/*` requests and WebSocket upgrades; JSON bodies only."""

    def __init__(self, app: ASGIApp, origin: str) -> None:
        self.app, self.origin = app, origin

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        pairs = [(k.decode("latin-1").lower(), v.decode("latin-1")) for k, v in scope["headers"]]
        headers = dict(pairs)
        origins = [v for k, v in pairs if k == "origin"]
        upgrade = scope["type"] == "websocket" or headers.get("upgrade", "").lower() == "websocket"
        method = scope.get("method", "GET")
        if (upgrade or method not in _SAFE_METHODS) and origins != [self.origin]:
            await self._refuse(scope, receive, send, error(403, "origin"))
            return
        length = headers.get("content-length", "0").strip()
        has_body = not length.isdigit() or int(length) > 0 or "transfer-encoding" in headers
        if (
            method not in _SAFE_METHODS
            and has_body
            and _media_type(headers.get("content-type", "")) != ("application/json")
        ):
            await self._refuse(scope, receive, send, error(415, "content_type"))
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _refuse(scope: Scope, receive: Receive, send: Send, response: Response) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await response(scope, receive, send)


def _endpoint(
    route: Route, settings: Settings, engine: AsyncEngine
) -> Callable[[Request], Awaitable[Response]]:
    async def endpoint(request: Request) -> Response:
        try:
            user = None
            if route.auth != "public" or route.path == "/api/auth/signout":
                user = await store.session_user(engine, request.cookies.get(SESSION_COOKIE))
                if user is None and route.auth != "public":
                    return error(401, "unauthenticated")
                if (
                    user is not None
                    and route.auth == "recent_session"
                    and user.session_age_s >= RECENT_SESSION_S
                ):
                    return error(401, "reauth_required")
            return await route.handler(Ctx(request, settings, engine, user))
        except SQLAlchemyError, OSError:
            # The database failed mid-request: one line, no detail (it could name the endpoint or the driver).
            log.warning("database_unreachable %s", route.path)
            return error(503, "unavailable")

    return endpoint


def _ws_endpoint(
    route: WsRoute, settings: Settings, engine: AsyncEngine
) -> Callable[[WebSocket], Awaitable[None]]:
    async def endpoint(websocket: WebSocket) -> None:
        await route.handler(websocket, settings, engine)

    return endpoint


async def _cleanup_forever(engine: AsyncEngine, interval_s: float) -> None:
    while True:
        try:
            await store.cleanup(engine)
        except Exception:
            log.warning("auth cleanup failed")
        await asyncio.sleep(interval_s)


def create_app(
    settings: Settings, engine: AsyncEngine, *, run_cleanup_task: bool = True, jobs: object | None = None
) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(_cleanup_forever(engine, CLEANUP_INTERVAL_S)) if run_cleanup_task else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.jobs = jobs  # SRV-PIPELINE routes enqueue through it (paired change)
    for route in ROUTES:
        app.add_api_route(
            route.path, _endpoint(route, settings, engine), methods=[route.method], include_in_schema=False
        )

    for ws_route in WS_ROUTES:
        app.router.add_websocket_route(ws_route.path, _ws_endpoint(ws_route, settings, engine))

    @app.exception_handler(404)
    async def not_found(_: Request, __: Exception) -> JSONResponse:
        return error(404, "not_found")

    app.add_middleware(CsrfGate, origin=settings.origin)
    return app
