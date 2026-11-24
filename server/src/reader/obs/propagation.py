"""Trace context across the job queue (SRV-OBS FR-OBS-5, INV-OBS-4): the traceparent of the current span, its
parser, and the job's root span — a new trace with a derived id and at most one link to the enqueuing span.
No helper here ever raises into a job: a failure leaves the job's outcome to SRV-EVENTS."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Final

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Link, Span, SpanContext, SpanKind, Status, StatusCode, TraceFlags

from reader.obs import tracer
from reader.obs.attributes import ATTRIBUTES, ERROR_CODES, JOB_ARGS

log = logging.getLogger("reader.obs")
_TRACEPARENT: Final = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})")
_CLASS: Final = re.compile(ATTRIBUTES["reader.error_class"]["pattern"])
OUTCOMES: Final = frozenset(ATTRIBUTES["reader.job.outcome"]["enum"])


def current_traceparent() -> str | None:
    """The W3C traceparent of the current span when it is valid and sampled, else None."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid or not ctx.trace_flags.sampled:
        return None
    return f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-{int(ctx.trace_flags):02x}"


def parse_traceparent(value: object) -> SpanContext | None:
    """A remote span context, or None for anything malformed, all-zero or unsampled. Never raises."""
    if not isinstance(value, str):
        return None
    match = _TRACEPARENT.fullmatch(value)
    if match is None:
        return None
    trace_id, span_id, flags = int(match.group(1), 16), int(match.group(2), 16), int(match.group(3), 16)
    if trace_id == 0 or span_id == 0 or not flags & 1:
        return None
    return SpanContext(trace_id, span_id, is_remote=True, trace_flags=TraceFlags(flags))


class JobSpan:
    """The job root span's two late facts; both calls are no-ops on a missing or non-recording span."""

    def __init__(self, span: Span | None) -> None:
        self._span = span

    def attempt(self, attempts: int) -> None:
        try:
            if self._span is not None:
                self._span.set_attribute("reader.job.attempt", int(attempts))
        except Exception:
            log.warning("obs job span: attempt not recorded")

    def outcome(self, outcome: str, code: str | None = None) -> None:
        try:
            if self._span is None or outcome not in OUTCOMES:
                return
            self._span.set_attribute("reader.job.outcome", outcome)
            if code in ERROR_CODES:
                self._span.set_attribute("reader.error_code", code)
            elif isinstance(code, str) and _CLASS.fullmatch(code):
                self._span.set_attribute("reader.error_class", code)
            if outcome in ("failed", "dead"):
                self._span.set_status(Status(StatusCode.ERROR))
        except Exception:
            log.warning("obs job span: outcome not recorded")


def _start(kind: str, job_id: int, key: str, payload: Mapping[str, Any], trace_value: object) -> Span:
    from reader.obs.ids import derive_trace_id, next_trace_id

    attributes: dict[str, Any] = {"reader.job.kind": kind, "reader.job.id": int(job_id)}
    for field, name in JOB_ARGS.get(kind, {}).items():
        if field in payload:
            attributes[name] = payload[field]
    from reader.obs import _STATE

    version = _STATE.get("version_key")  # SRV-OBS FR-OBS-21 (a): set once at the worker's start
    if isinstance(version, str):
        attributes["reader.version_key"] = version
    parent = parse_traceparent(trace_value)
    with next_trace_id(derive_trace_id(job_id, key)):
        return tracer().start_span(
            f"job {kind}",
            context=otel_context.Context(),  # a new trace: never the enqueuer's
            kind=SpanKind.CONSUMER,
            links=[Link(parent)] if parent is not None else (),
            attributes=attributes,
            record_exception=False,
            set_status_on_exception=False,
        )


@contextmanager
def job_span(
    kind: str, job_id: int, key: str, payload: Mapping[str, Any], trace: object = None
) -> Iterator[JobSpan]:
    """The job's root span, current for the whole run; a failure to create it gives a no-op JobSpan."""
    span: Span | None = None
    token: object = None
    try:
        span = _start(kind, job_id, key, payload, trace)
        token = otel_context.attach(_set_span(span))
    except Exception:
        log.warning("obs job span not started: kind=%s", kind)
    try:
        yield JobSpan(span)
    finally:
        try:
            if token is not None:
                otel_context.detach(token)  # type: ignore[arg-type]
            if span is not None:
                span.end()
        except Exception:
            log.warning("obs job span not ended: kind=%s", kind)


def _set_span(span: Span) -> otel_context.Context:
    return trace.set_span_in_context(span)
