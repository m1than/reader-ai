"""HTTP and WebSocket spans plus the request log line (SRV-OBS FR-OBS-3, FR-OBS-4).

One SERVER span per HTTP request and per WebSocket session under /api/ (except /api/healthz and
/api/obs/gate), named by method and the registry's route template. A client's traceparent is never read: every
request starts a new trace.
Nothing of the raw path, query, headers, cookies, client address or body reaches the span or the log line.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable
from typing import Any, Final

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from reader.obs import tracer

log = logging.getLogger("reader.obs.http")
SILENT: Final = frozenset({"/api/healthz", "/api/obs/gate"})
METHODS: Final = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"})


def _pattern(template: str) -> re.Pattern[str]:
    parts = re.split(r"(\{[^}/]+\})", template)
    return re.compile("".join("[^/]+" if p.startswith("{") else re.escape(p) for p in parts if p))


def _finish(span: Span | None, token: object, websocket: bool, state: dict[str, Any]) -> str:
    """Detach, record the status, end the span; the log line's ` trace=` suffix. Never raises (INV-OBS-3)."""
    try:
        if token is not None:
            otel_context.detach(token)  # type: ignore[arg-type]
        if span is None:
            return ""
        if websocket:
            if state["close"] is not None:
                span.set_attribute("reader.ws.close_code", state["close"])
        elif state["status"] is not None:
            span.set_attribute("http.response.status_code", state["status"])
        if state.get("error") or (not websocket and (state["status"] or 0) >= 500):
            span.set_status(Status(StatusCode.ERROR))
        span.end()
        ctx = span.get_span_context()
        return f" trace={ctx.trace_id:032x}" if ctx.is_valid else ""  # invalid: tracing is off
    except Exception:
        log.warning("obs request span not ended")
        return ""


class TraceMiddleware:
    def __init__(self, app: ASGIApp, templates: Iterable[str]) -> None:
        self.app = app
        # Literal templates first, then fewer parameters, so /api/x/static beats /api/x/{id}.
        ordered = sorted(set(templates), key=lambda t: (t.count("{"), -len(t)))
        self._routes = [(t, _pattern(t)) for t in ordered]

    def route_of(self, path: str) -> str:
        for template, pattern in self._routes:
            if pattern.fullmatch(path):
                return template
        return "other"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] not in ("http", "websocket") or not path.startswith("/api/") or path in SILENT:
            await self.app(scope, receive, send)
            return
        websocket = scope["type"] == "websocket"
        route = self.route_of(path)
        raw = str(scope.get("method", "GET")).upper()
        method = "GET" if websocket else raw if raw in METHODS else "_OTHER"
        name = f"WS {route}" if websocket else f"{method} {route}"
        state: dict[str, Any] = {"status": None, "close": None}
        started = time.monotonic()
        # INV-OBS-3: an OTel failure never reaches the request; the request then runs without a span.
        span: Span | None = None
        token: object = None
        try:
            span = tracer().start_span(
                name,
                context=otel_context.Context(),  # never the client's traceparent
                kind=SpanKind.SERVER,
                attributes={"http.request.method": method, "http.route": route},
                record_exception=False,
                set_status_on_exception=False,
            )
            token = otel_context.attach(trace.set_span_in_context(span))
        except Exception:
            log.warning("obs request span not started")

        async def sending(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["status"] = int(message["status"])
            elif message["type"] == "websocket.close":
                state["close"] = int(message.get("code", 1000))
            await send(message)

        async def receiving() -> Message:
            message = await receive()
            if message["type"] == "websocket.disconnect" and state["close"] is None:
                state["close"] = int(message.get("code", 1000))
            return message

        try:
            await self.app(scope, receiving, sending)
        except Exception:
            state["error"] = True
            if state["status"] is None:
                state["status"] = 500
            raise
        finally:
            suffix = _finish(span, token, websocket, state)
            ms = int((time.monotonic() - started) * 1000)
            if websocket:
                log.info("ws route=%s close=%s ms=%d%s", route, state["close"], ms, suffix)
            else:
                status = state["status"]
                log.info("request method=%s route=%s status=%s ms=%d%s", method, route, status, ms, suffix)
