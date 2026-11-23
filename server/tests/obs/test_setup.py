"""SRV-OBS [unit]: setup, the disabled path, the propagator, and failure isolation.

@trace FR-OBS-1 INV-OBS-3
"""

from __future__ import annotations

import time
from typing import Any

from .conftest import catalogs, config, finished, obs_setup, require


def test_disabled_installs_nothing() -> None:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    setup = require("reader.obs.setup")
    memory = InMemorySpanExporter()
    handle = setup.setup("api", config(enabled=False), exporter=memory, catalogs=catalogs())
    try:
        assert handle.enabled is False
        with require("reader.obs").tracer().start_as_current_span("agent.call") as span:
            assert not span.is_recording()
            assert require("reader.obs.propagation").current_traceparent() is None
        handle.force_flush()
        assert memory.get_finished_spans() == ()
    finally:
        handle.shutdown()


def test_enabled_exports_with_the_fixed_resource_and_scope() -> None:
    with obs_setup("worker") as (handle, memory):
        assert handle.enabled is True
        with require("reader.obs").tracer().start_as_current_span("agent.call"):
            parent = require("reader.obs.propagation").current_traceparent()
            assert parent is not None and parent.startswith("00-") and parent.endswith("-01")
        (span,) = finished(handle, memory)
        assert span.name == "agent.call"
        assert dict(span.resource.attributes) == {
            "service.name": "reader-worker",
            "service.version": require("reader.obs.export").reader_version(),
        }
        assert span.instrumentation_scope.name == "reader"


def test_processor_bounds_and_endpoint() -> None:
    setup = require("reader.obs.setup")
    assert setup.OTLP_ENDPOINT == "http://rotel:4318/v1/traces"
    assert setup.BATCH == {
        "schedule_delay_millis": 5000,
        "max_queue_size": 2048,
        "max_export_batch_size": 512,
        "export_timeout_millis": 10000,
    }
    assert setup.SHUTDOWN_FLUSH_MS == 2000


def test_propagator_is_tracecontext_only() -> None:
    from opentelemetry import propagate

    with obs_setup() as _, require("reader.obs").tracer().start_as_current_span("agent.call"):
        carrier: dict[str, str] = {}
        propagate.inject(carrier)
        assert set(carrier) == {"traceparent"}


def test_setup_is_repeatable_and_shutdown_bounded() -> None:
    from opentelemetry.sdk.trace.export import SpanExportResult

    class Slow:
        def export(self, spans: Any) -> Any:
            time.sleep(30)
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None: ...

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    setup = require("reader.obs.setup")
    handle = setup.setup("api", config(), exporter=Slow(), catalogs=catalogs())
    with require("reader.obs").tracer().start_as_current_span("agent.call"):
        pass
    started = time.monotonic()
    handle.shutdown()
    assert time.monotonic() - started < 4.0  # 2 s flush bound plus slack; never the exporter's 30 s
    with obs_setup() as (handle2, memory):
        with require("reader.obs").tracer().start_as_current_span("agent.call"):
            pass
        assert len(finished(handle2, memory)) == 1


def test_inv_obs_3_exporter_failures_never_reach_the_caller() -> None:
    from opentelemetry.sdk.trace.export import SpanExportResult

    class Refusing:
        def __init__(self, mode: str) -> None:
            self.mode = mode

        def export(self, spans: Any) -> Any:
            if self.mode == "raise":
                raise ConnectionRefusedError("rotel down")
            return SpanExportResult.FAILURE

        def shutdown(self) -> None: ...

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    setup = require("reader.obs.setup")
    for mode in ("raise", "fail"):
        handle = setup.setup("api", config(), exporter=Refusing(mode), catalogs=catalogs())
        try:
            for _ in range(5):
                with require("reader.obs").tracer().start_as_current_span("agent.call") as span:
                    span.set_attribute("reader.task", "tutor")
            handle.force_flush()
        finally:
            handle.shutdown()


def test_default_exporter_points_at_rotel_without_headers() -> None:
    setup = require("reader.obs.setup")
    exporter = setup.default_exporter()
    assert exporter._endpoint == "http://rotel:4318/v1/traces"
    assert exporter._timeout == 10
    assert not dict(exporter._headers or {})
