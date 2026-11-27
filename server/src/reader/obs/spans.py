"""Helpers modules call inside a job (SRV-OBS FR-OBS-5 e, FR-OBS-12 e, FR-OBS-13, FR-OBS-37). None of them
raises, and with tracing off none of them does anything: the session marker, the job root's handler-set
attributes, the child spans with counts, and the typed pack-build and link-run spans. Attribute values are
checked by the exporter like every other; these set them."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Final, Literal, NewType

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from reader.obs import _STATE, tracer

log = logging.getLogger("reader.obs")
FLUSH_MS: Final = 2000
_LOGGED: set[str] = set()


def _once(message: str) -> None:
    if message not in _LOGGED:
        _LOGGED.add(message)
        log.warning(message)


def _flush(provider: Any) -> None:
    try:
        provider.force_flush(FLUSH_MS)
    except Exception:
        _once("obs session_marker_failed")


def session_marker(key: str, value: str) -> None:
    """FR-OBS-12 (e): a zero-length `job.session` child of the current span carrying `key` (reader.run_id or
    reader.lesson.id), flushed on a daemon thread so it reaches MLflow before the CLI's spans."""
    provider = _STATE["provider"]
    if provider is None:
        return
    try:
        span = tracer().start_span("job.session", attributes={key: value})
        span.set_status(Status(StatusCode.OK))
        span.end()
        threading.Thread(target=_flush, args=(provider,), name="obs-session-flush", daemon=True).start()
    except Exception:
        _once("obs session_marker_failed")


def _set(span: Span, attributes: Mapping[str, Any]) -> None:
    for key, value in attributes.items():
        if value is not None:
            span.set_attribute(key, value)


def annotate_job(attributes: Mapping[str, Any]) -> None:
    """FR-OBS-5 (e): set root keys a handler learns from its own rows (e.g. reader.lesson.id) on the current
    span, which inside a handler is the job's root. None values are skipped."""
    if _STATE["provider"] is None:
        return
    try:
        _set(trace.get_current_span(), attributes)
    except Exception:
        _once("obs annotate_job_failed")


class Child:
    def __init__(self, span: Span | None) -> None:
        self._span = span

    def set(self, attributes: Mapping[str, Any]) -> None:
        """Counts known only at the end of the part."""
        try:
            if self._span is not None:
                _set(self._span, attributes)
        except Exception:
            _once("obs child_span_failed")


@contextmanager
def child_span(name: str, attributes: Mapping[str, Any] | None = None) -> Iterator[Child]:
    """FR-OBS-13: a child of the current span; status OK on success, ERROR (no description) on an exception,
    which is re-raised unchanged."""
    span: Span | None = None
    token: object = None
    if _STATE["provider"] is not None:
        try:
            given = {k: v for k, v in (attributes or {}).items() if v is not None}
            span = tracer().start_span(name, attributes=given, record_exception=False,
                                       set_status_on_exception=False)
            token = otel_context.attach(trace.set_span_in_context(span))
        except Exception:
            _once("obs child_span_failed")
    child = Child(span)
    try:
        yield child
    except BaseException:
        _end(span, token, StatusCode.ERROR)
        raise
    _end(span, token, StatusCode.OK)


# --- Typed child spans (FR-OBS-36, -37): parameters map to keys through a table; no plain str parameter. ---

Hex64 = NewType("Hex64", str)  # 64 lowercase hex: a sha256, a pack id or a version
OverlayKind = Literal["base", "profile", "tags", "vectors", "prefixes", "summaries", "concept_evidence"]
PackState = Literal["building", "sealed", "failed"]

PACK_KEYS: Final[Mapping[str, str]] = {
    "pack_id": "reader.pack.id", "overlay_version": "reader.overlay.version",
    "material_sha256": "reader.material_sha256", "overlay_kind": "reader.overlay.kind",
    "state": "reader.pack.state",
    "spans": "reader.pack.spans", "outline_nodes": "reader.pack.outline_nodes", "rows": "reader.pack.rows",
    "calls": "reader.build.calls", "usd": "reader.build.usd", "cap_hit": "reader.build.cap_hit",
    "pack_a": "reader.pack.a", "pack_b": "reader.pack.b", "mentions": "reader.link.mentions",
    "concepts_proposed": "reader.link.concepts_proposed", "pairs_blocked": "reader.link.pairs_blocked",
    "pairs_kept": "reader.link.pairs_kept", "judge_calls": "reader.link.judge_calls",
    "judge_replayed": "reader.link.judge_replayed", "links": "reader.link.links",
}


def keyed(keys: Mapping[str, str], values: Mapping[str, Any]) -> dict[str, Any]:
    """The values whose parameter name is in `keys`, under their attribute keys; None values are skipped."""
    return {keys[name]: value for name, value in values.items() if name in keys and value is not None}


class PackPart:
    def __init__(self, child: Child) -> None:
        self._child = child

    def done(self, *, state: PackState, spans: int, outline_nodes: int, rows: int, calls: int, usd: float,
             cap_hit: bool) -> None:
        """reader.build.usd = the build's settled ledger sum, for reading only (never a cost key)."""
        self._child.set(keyed(PACK_KEYS, locals()))


class LinkPart:
    def __init__(self, child: Child) -> None:
        self._child = child

    def done(self, *, mentions: int, concepts_proposed: int, pairs_blocked: int, pairs_kept: int,
             judge_calls: int, judge_replayed: int, links: int, usd: float, cap_hit: bool) -> None:
        self._child.set(keyed(PACK_KEYS, locals()))


@contextmanager
def pack_build(*, pack_id: Hex64, overlay_version: Hex64, material_sha256: Hex64,
               overlay_kind: OverlayKind) -> Iterator[PackPart]:
    """FR-OBS-37: one `pack.build` (CHAIN) per base pack or overlay build or batch."""
    with child_span("pack.build", keyed(PACK_KEYS, locals())) as child:
        yield PackPart(child)


@contextmanager
def link_run(*, pack_a: Hex64, pack_b: Hex64, overlay_version: Hex64) -> Iterator[LinkPart]:
    """FR-OBS-37: one `link.run` (CHAIN) per pair run, with one count per B4 step."""
    with child_span("link.run", keyed(PACK_KEYS, locals())) as child:
        yield LinkPart(child)


def _end(span: Span | None, token: object, code: StatusCode) -> None:
    try:
        if token is not None:
            otel_context.detach(token)  # type: ignore[arg-type]
        if span is not None:
            span.set_status(Status(code))
            span.end()
    except Exception:
        _once("obs child_span_failed")
