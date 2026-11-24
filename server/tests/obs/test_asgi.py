"""SRV-OBS [unit]: HTTP and WebSocket spans, the request log line and the trace-id log filter.

@trace FR-OBS-3 FR-OBS-4 INV-OBS-3
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from .conftest import ROUTE_TEMPLATES, catalogs, config, finished, obs_setup, require

CANARY = "bookcanary-2b8e"
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _app() -> Any:
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route, WebSocketRoute
    from starlette.websockets import WebSocket

    async def lesson(request: Any) -> Response:
        if request.path_params["lesson_id"] == "boom":
            raise RuntimeError(CANARY)
        return JSONResponse({"ok": True})

    async def health(_: Any) -> Response:
        return Response(status_code=200)

    async def channels(ws: WebSocket) -> None:
        await ws.accept()
        await ws.receive_text()
        await ws.close(code=4001)

    inner = Starlette(
        routes=[
            Route("/api/lessons/{lesson_id}", lesson, methods=["GET", "POST"]),
            Route("/api/healthz", health),
            Route("/api/obs/gate", health),
            WebSocketRoute("/api/kernel/sessions/{session_id}/channels", channels),
        ]
    )
    return require("reader.obs.asgi").TraceMiddleware(inner, ROUTE_TEMPLATES)


async def _get(app: Any, path: str, **kw: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="https://localhost") as c:
        return await c.get(path, **kw)


async def test_http_span_has_the_route_template_only(caplog: Any) -> None:
    foreign = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    with obs_setup() as (handle, memory), caplog.at_level(logging.INFO, logger="reader.obs.http"):
        r = await _get(
            _app(),
            f"/api/lessons/{CANARY}?q={CANARY}",
            headers={"traceparent": foreign, "cookie": f"s={CANARY}"},
        )
        assert r.status_code == 200
        (span,) = finished(handle, memory)
    assert span.name == "GET /api/lessons/{lesson_id}"
    assert dict(span.attributes) == {
        "http.request.method": "GET",
        "http.route": "/api/lessons/{lesson_id}",
        "http.response.status_code": 200,
        "mlflow.traceTag.mlflow.traceName": "GET /api/lessons/{lesson_id}",  # FR-OBS-12 (d): a root's name
    }
    assert span.kind.name == "SERVER"
    assert format(span.context.trace_id, "032x") != "a" * 32  # the client's traceparent is ignored
    assert span.parent is None
    assert CANARY not in span.to_json()
    lines = [r.getMessage() for r in caplog.records if r.name == "reader.obs.http"]
    trace = format(span.context.trace_id, "032x")
    assert len(lines) == 1
    assert lines[0].startswith("request method=GET route=/api/lessons/{lesson_id} status=200 ms=")
    assert lines[0].endswith(f" trace={trace}")
    assert CANARY not in lines[0]


async def test_unmatched_route_is_other_and_health_and_gate_are_silent(caplog: Any) -> None:
    with obs_setup() as (handle, memory), caplog.at_level(logging.INFO, logger="reader.obs.http"):
        assert (await _get(_app(), f"/api/{CANARY}")).status_code == 404
        assert (await _get(_app(), "/api/healthz")).status_code == 200
        assert (await _get(_app(), "/api/obs/gate")).status_code == 200
        assert (await _get(_app(), "/index.html")).status_code == 404  # not under /api/
        spans = finished(handle, memory)
    assert [s.name for s in spans] == ["GET other"]
    assert dict(spans[0].attributes)["http.route"] == "other"
    lines = [r.getMessage() for r in caplog.records if r.name == "reader.obs.http"]
    assert len(lines) == 1 and " route=other status=404 " in lines[0]


async def test_unhandled_exception_sets_error_without_description() -> None:
    with obs_setup() as (handle, memory):
        r = await _get(_app(), "/api/lessons/boom")
        assert r.status_code == 500
        (span,) = finished(handle, memory)
    assert span.status.status_code.name == "ERROR"
    assert span.status.description is None
    assert list(span.events) == []
    assert CANARY not in span.to_json()


def test_websocket_session_span(caplog: Any) -> None:
    from starlette.testclient import TestClient

    with obs_setup() as (handle, memory), caplog.at_level(logging.INFO, logger="reader.obs.http"):
        with (
            TestClient(_app()) as client,
            client.websocket_connect(f"/api/kernel/sessions/{CANARY}/channels") as ws,
        ):
            ws.send_text(CANARY)
            ws.receive()
        (span,) = finished(handle, memory)
    assert span.name == "WS /api/kernel/sessions/{session_id}/channels"
    attrs = dict(span.attributes)
    assert attrs["http.route"] == "/api/kernel/sessions/{session_id}/channels"
    assert attrs["reader.ws.close_code"] == 4001
    assert CANARY not in span.to_json()
    lines = [r.getMessage() for r in caplog.records if r.name == "reader.obs.http"]
    assert len(lines) == 1 and lines[0].startswith(
        "ws route=/api/kernel/sessions/{session_id}/channels close=4001 "
    )


async def test_disabled_tracing_still_logs_without_trace(caplog: Any) -> None:
    setup = require("reader.obs.setup")
    handle = setup.setup("api", config(enabled=False), catalogs=catalogs())
    try:
        with caplog.at_level(logging.INFO, logger="reader.obs.http"):
            assert (await _get(_app(), "/api/lessons/x")).status_code == 200
    finally:
        handle.shutdown()
    lines = [r.getMessage() for r in caplog.records if r.name == "reader.obs.http"]
    assert len(lines) == 1 and "trace=" not in lines[0]


async def test_inv_obs_3_failing_exporter_changes_no_response() -> None:

    class Down:
        def export(self, spans: Any) -> Any:
            raise ConnectionRefusedError

        def shutdown(self) -> None: ...

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    setup = require("reader.obs.setup")
    handle = setup.setup("api", config(), exporter=Down(), catalogs=catalogs())
    try:
        r = await _get(_app(), "/api/lessons/x")
        assert (r.status_code, r.json()) == (200, {"ok": True})
        handle.force_flush()
    finally:
        handle.shutdown()


@pytest.mark.parametrize("broken", ["start_span", "set_attribute", "end"])
async def test_inv_obs_3_a_raising_tracer_changes_no_response(
    broken: str, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    """Round 1: every OTel call of the middleware is guarded; the request and its log line go on."""
    import reader.obs.asgi as asgi

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("otel broke")

    class Span:
        def set_attribute(self, *_a: Any) -> None:
            if broken == "set_attribute":
                boom()

        def set_status(self, *_a: Any) -> None: ...

        def end(self) -> None:
            if broken == "end":
                boom()

        def is_recording(self) -> bool:
            return True

        def get_span_context(self) -> Any:
            from opentelemetry.trace import INVALID_SPAN_CONTEXT

            return INVALID_SPAN_CONTEXT

    class Tracer:
        def start_span(self, *_a: Any, **_k: Any) -> Any:
            if broken == "start_span":
                boom()
            return Span()

    monkeypatch.setattr(asgi, "tracer", lambda: Tracer())
    with caplog.at_level(logging.INFO, logger="reader.obs.http"):
        r = await _get(_app(), "/api/lessons/x")
    assert (r.status_code, r.json()) == (200, {"ok": True})
    assert any(m.getMessage().startswith("request method=GET") for m in caplog.records)


def test_trace_filter_appends_once_inside_a_span() -> None:
    logs = require("reader.obs.logs")
    filt = logs.TraceFilter()
    record = logging.LogRecord("reader.jobs", logging.INFO, __file__, 1, "job failed: kind=%s", ("x",), None)
    assert filt.filter(record) is True
    assert record.getMessage() == "job failed: kind=x"  # no current span: nothing added
    with obs_setup() as _, require("reader.obs").tracer().start_as_current_span("agent.call") as span:
        record = logging.LogRecord(
            "reader.jobs", logging.INFO, __file__, 1, "job failed: kind=%s", ("x",), None
        )
        filt.filter(record)
        filt.filter(record)  # idempotent
        trace = format(span.get_span_context().trace_id, "032x")
        assert record.getMessage() == f"job failed: kind=x trace={trace}"
