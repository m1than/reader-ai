"""The AllowListExporter (SRV-OBS FR-OBS-2, INV-OBS-1): every span is rebuilt against the allow-list before
the OTLP exporter sees it. Names outside the list become `reader.unlisted`; unlisted or invalid attributes,
every event, the status description, link attributes and every tracestate are dropped; resource and scope
are fixed."""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Sequence
from typing import Final, cast

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.util.instrumentation import InstrumentationScope
from opentelemetry.trace import Link, SpanContext, Status, TraceState
from opentelemetry.util.types import AttributeValue

from reader.obs.attributes import FALLBACK_NAME, SCOPE, AllowList, key_name

log = logging.getLogger("reader.obs")
_SCOPE: Final = InstrumentationScope(SCOPE)


def reader_version() -> str:
    try:
        return importlib.metadata.version("reader")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"


def resource(role: str) -> Resource:
    """Exactly the two resource keys of FR-OBS-1 (`Resource.get_empty` adds no SDK defaults)."""
    return Resource.get_empty().merge(
        Resource({"service.name": f"reader-{role}", "service.version": reader_version()})
    )


def _bare(ctx: SpanContext | None) -> SpanContext | None:
    """The same ids and flags, without tracestate."""
    if ctx is None:
        return None
    return SpanContext(ctx.trace_id, ctx.span_id, ctx.is_remote, ctx.trace_flags, TraceState())


class AllowListExporter(SpanExporter):
    def __init__(self, inner: SpanExporter, allowlist: AllowList, resource: Resource) -> None:
        self._inner, self._allow, self._resource = inner, allowlist, resource
        self._logged: set[str] = set()

    def _drop(self, key: object) -> None:
        name = key_name(key)
        if name not in self._logged:
            self._logged.add(name)
            log.warning("obs dropped key=%s", name)

    def rebuild(self, span: ReadableSpan) -> ReadableSpan:
        klass = self._allow.span_class(span.name)
        given = dict(span.attributes or {})
        # `clean` keeps only values that passed a str, int, float or bool rule: valid OTel attribute values.
        kept = cast(dict[str, AttributeValue], self._allow.clean(klass, given)) if klass else {}
        for key in given:
            if key not in kept:
                self._drop(key)
        links = tuple(Link(ctx) for link in span.links if (ctx := _bare(link.context)) is not None)
        return ReadableSpan(
            name=span.name if klass else FALLBACK_NAME,
            context=_bare(span.context),
            parent=_bare(span.parent),
            resource=self._resource,
            attributes=kept,
            events=(),
            links=links,
            kind=span.kind,
            status=Status(span.status.status_code),
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=_SCOPE,
        )

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            rebuilt = [self.rebuild(span) for span in spans]
            return self._inner.export(rebuilt)
        except Exception:  # never raise into the processor; the batch is dropped (telemetry is lossy)
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        try:
            self._inner.shutdown()
        except Exception:
            log.warning("obs exporter shutdown failed")

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            return bool(self._inner.force_flush(timeout_millis))
        except Exception:
            return False
