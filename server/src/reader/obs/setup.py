"""Tracing setup for `reader-server --role api|worker` (SRV-OBS FR-OBS-1).

Disabled (`observability.enabled: false`): nothing is installed; `reader.obs.tracer()` stays OTel's no-op
tracer. Enabled: one TracerProvider with the fixed resource, the parent-based always-on sampler and the job id
generator, one BatchSpanProcessor feeding the AllowListExporter, which wraps OTLP/HTTP to Rotel. The global
propagator is W3C tracecontext only. `shutdown()` is bounded by 2 s whatever the exporter does.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Final, TypedDict

from opentelemetry.propagate import set_global_textmap
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from reader.obs import install
from reader.obs.attributes import AllowList, Catalogs
from reader.obs.export import AllowListExporter, resource
from reader.obs.ids import JobIdGenerator

log = logging.getLogger("reader.obs")

OTLP_ENDPOINT: Final = "http://rotel:4318/v1/traces"
EXPORTER_KWARGS: Final[dict[str, Any]] = {"endpoint": OTLP_ENDPOINT, "timeout": 10, "headers": {}}
# The exporter merges these environment headers into its own whatever `headers` says, so they are absent while
# it is built (code review round 1): only EXPORTER_KWARGS decide what goes to Rotel.
HEADER_ENV: Final = ("OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS")


class Batch(TypedDict):
    schedule_delay_millis: int
    max_queue_size: int
    max_export_batch_size: int
    export_timeout_millis: int


BATCH: Final[Batch] = {
    "schedule_delay_millis": 5000,
    "max_queue_size": 2048,
    "max_export_batch_size": 512,
    "export_timeout_millis": 10000,
}
SHUTDOWN_FLUSH_MS: Final = 2000


@dataclass(frozen=True)
class Settings:
    """What the retention job reads (FR-OBS-11); off until `setup` runs."""

    enabled: bool = False
    retention_days: int = 30


_CURRENT: list[Settings] = [Settings()]


def current_settings() -> Settings:
    return _CURRENT[0]


async def effective_settings(conn: Any) -> Settings:
    """`current_settings()` with the owner's retention override, read per run and per request (SRV-CONFIG
    FR-CONFIG-26, paired); without a connection the file's value stands."""
    current = current_settings()
    if conn is None:
        return current
    from reader.config.effective import retention_days

    return Settings(current.enabled, await retention_days(conn, current.retention_days))


@dataclass
class Handle:
    enabled: bool
    provider: TracerProvider | None = None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True if self.provider is None else bool(self.provider.force_flush(timeout_millis))

    def shutdown(self) -> None:
        """Flush for at most 2 s, then stop; the exporter's own timeout never holds the caller."""
        provider, self.provider = self.provider, None
        install(None)
        if provider is None:
            return

        def stop() -> None:
            try:
                provider.force_flush(SHUTDOWN_FLUSH_MS)
                provider.shutdown()
            except Exception:
                log.warning("obs shutdown failed")

        worker = threading.Thread(target=stop, name="reader-obs-shutdown", daemon=True)
        worker.start()
        worker.join(SHUTDOWN_FLUSH_MS / 1000)


def default_exporter() -> SpanExporter:
    from opentelemetry.exporter.otlp.proto.http import trace_exporter

    saved = {name: os.environ.pop(name) for name in HEADER_ENV if name in os.environ}
    try:
        return trace_exporter.OTLPSpanExporter(**EXPORTER_KWARGS)
    finally:
        os.environ.update(saved)


def default_catalogs(config: Any) -> Catalogs:
    """The code-declared sets for the allow-list's `enum_from` rules (FR-OBS-2)."""
    from reader.agent.pricing import load_pricing
    from reader.api.app import ROUTES, WS_ROUTES
    from reader.jobs.catalog import load_kinds
    from reader.pipeline.stages import load as load_stages

    return Catalogs(
        route_templates=frozenset({r.path for r in ROUTES} | {w.path for w in WS_ROUTES}),
        job_kinds=frozenset(load_kinds()),
        pipeline_stages=frozenset(load_stages().stages),
        tasks=frozenset(config.tasks),
        models=frozenset(load_pricing().models),
    )


def setup(
    role: str, config: Any, *, exporter: SpanExporter | None = None, catalogs: Catalogs | None = None
) -> Handle:
    """Install tracing for `role` (api or worker) from `config.observability`; returns the handle that shuts
    it down."""
    set_global_textmap(TraceContextTextMapPropagator())
    from reader.config.effective import file_retention_days  # the overlay's reader (FR-CONFIG-26)

    _CURRENT[0] = Settings(bool(config.observability.enabled), file_retention_days(config))
    if not config.observability.enabled:
        install(None)
        return Handle(enabled=False)
    allow = AllowList(catalogs if catalogs is not None else default_catalogs(config))
    provider = TracerProvider(
        resource=resource(role),
        sampler=ParentBased(ALWAYS_ON),
        id_generator=JobIdGenerator(),
        shutdown_on_exit=False,
    )
    inner = exporter if exporter is not None else default_exporter()
    provider.add_span_processor(BatchSpanProcessor(AllowListExporter(inner, allow, resource(role)), **BATCH))
    install(provider)
    return Handle(enabled=True, provider=provider)
