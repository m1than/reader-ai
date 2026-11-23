"""SRV-OBS [unit]: the canary for this process — what the OTLP exporter sends holds no book text or secret.

@trace INV-OBS-2 FR-OBS-7

An in-test OTLP/HTTP receiver (a local socket) captures the protobuf bodies the real OTLP exporter sends through the
AllowListExporter; requests, a job run and an agent-call span are driven with canaries in every input.
"""

from __future__ import annotations

import gzip
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from .conftest import ROUTE_TEMPLATES, catalogs, config, require

CANARIES = (
    "bookcanary-91d0",
    "questioncanary-77aa",
    "answercanary-03fe",
    "systemcanary-5c1d",
    "runtokencanary-e4b2",
    "secretcanary-a9c3",
)


class _Receiver(BaseHTTPRequestHandler):
    bodies: list[bytes] = []

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        if self.headers.get("Content-Encoding", "") == "gzip":
            body = gzip.decompress(body)
        _Receiver.bodies.append(body)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def receiver() -> Iterator[str]:
    _Receiver.bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1/traces"
    finally:
        server.shutdown()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_no_canary_reaches_the_exporter(receiver: str) -> None:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    from starlette.applications import Starlette
    from starlette.routing import Route

    setup = require("reader.obs.setup")
    prop = require("reader.obs.propagation")
    obs = require("reader.obs")
    book, question, answer, system, run_token, secret = CANARIES

    async def lesson(request: Any) -> Any:
        await request.body()
        with obs.tracer().start_as_current_span("agent.call") as span:
            span.set_attribute("prompt", f"{system} {book} {question}")
            span.set_attribute("reader.task", run_token)
            span.add_event("reply", {"text": answer})
        raise RuntimeError(secret)

    app = require("reader.obs.asgi").TraceMiddleware(
        Starlette(routes=[Route("/api/lessons/{lesson_id}", lesson, methods=["POST"])]), ROUTE_TEMPLATES
    )
    handle = setup.setup("api", config(), exporter=OTLPSpanExporter(endpoint=receiver), catalogs=catalogs())
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="https://localhost") as c:
            await c.post(
                f"/api/lessons/{book}?q={question}",
                content=f'{{"a": "{answer}"}}',
                headers={"x-token": run_token, "cookie": f"s={secret}"},
            )
        with prop.job_span(
            kind="tutor.answer",
            job_id=9,
            key="tutor.answer:" + "c" * 64,
            payload={"question_id": question, "text": book},
            trace=f"00-{book}",
        ) as job:
            job.outcome("failed", secret)
        handle.force_flush()
    finally:
        handle.shutdown()
    assert _Receiver.bodies, "the exporter sent nothing"
    names: list[str] = []
    for body in _Receiver.bodies:
        for canary in CANARIES:
            assert canary.encode() not in body, canary
        request = ExportTraceServiceRequest()
        request.ParseFromString(body)
        names += [s.name for rs in request.resource_spans for ss in rs.scope_spans for s in ss.spans]
    # FR-OBS-7: the request and the job are both traced, by route template and kind.
    assert "POST /api/lessons/{lesson_id}" in names
    assert "job tutor.answer" in names
    assert "agent.call" in names


def test_fr_obs_7_every_kind_and_route_is_a_listed_span_name() -> None:
    from reader.api.app import ROUTES, WS_ROUTES
    from reader.jobs.catalog import load_kinds

    setup = require("reader.obs.setup")
    attrs = require("reader.obs.attributes")
    cats = setup.default_catalogs(config())
    allow = attrs.AllowList(cats)
    for route in ROUTES:
        if route.path not in ("/api/healthz", "/api/obs/gate"):
            assert allow.span_class(f"{route.method} {route.path}") == "http", route.path
    for ws in WS_ROUTES:
        assert allow.span_class(f"WS {ws.path}") == "ws", ws.path
    kinds = set(load_kinds())
    assert {
        "ingest.import",
        "search.embed",
        "pipeline.stage",
        "pipeline.verify",
        "tutor.answer",
        "obs.retention",
    } <= kinds
    for kind in kinds:
        assert allow.span_class(f"job {kind}") == "job", kind
    assert set(attrs.JOB_ARGS) == kinds
