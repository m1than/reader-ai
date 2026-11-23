"""The `agent.call` span (SRV-OBS FR-OBS-6, SRV-AGENT FR-AGENT-14): current while the CLI runs, so the Agent
SDK injects its TRACEPARENT and the CLI's spans nest under it. It carries tokens, the settled cost (ours: the
CLI's spans carry none), the stop reason and the error code. A failure of a helper never reaches the call."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from reader.obs import tracer

log = logging.getLogger("reader.obs")


class AgentSpan:
    def __init__(self, span: Span | None) -> None:
        self._span = span

    def settled(self, usage: Any, settled_usd: Decimal | None, stop_reason: str | None) -> None:
        try:
            if self._span is None:
                return
            if usage is not None:
                self._span.set_attribute("gen_ai.usage.input_tokens", int(usage.input))
                self._span.set_attribute("gen_ai.usage.output_tokens", int(usage.output))
                self._span.set_attribute("gen_ai.usage.cache_read_input_tokens", int(usage.cache_read))
                written = int(usage.cache_write_5m) + int(usage.cache_write_1h)
                self._span.set_attribute("gen_ai.usage.cache_creation_input_tokens", written)
            if settled_usd is not None:
                self._span.set_attribute("reader.cost_usd", float(settled_usd))
            if stop_reason is not None:
                self._span.set_attribute("reader.stop_reason", stop_reason)
        except Exception:
            log.warning("obs agent span: result not recorded")

    def failed(self, code: str) -> None:
        try:
            if self._span is not None:
                self._span.set_attribute("reader.error_code", code)
                self._span.set_status(Status(StatusCode.ERROR))
        except Exception:
            log.warning("obs agent span: error not recorded")


@contextmanager
def agent_call(task: str, stage: str | None, model: str) -> Iterator[AgentSpan]:
    span: Span | None = None
    token: object = None
    try:
        attributes: dict[str, Any] = {"reader.task": task, "gen_ai.request.model": model}
        if stage:
            attributes["reader.stage"] = stage
        span = tracer().start_span(
            "agent.call", kind=SpanKind.CLIENT, attributes=attributes, record_exception=False,
            set_status_on_exception=False,
        )
        token = otel_context.attach(trace.set_span_in_context(span))
    except Exception:
        log.warning("obs agent span not started")
    wrapper = AgentSpan(span)
    try:
        yield wrapper
    except Exception as error:
        code = getattr(error, "code", None)
        if isinstance(code, str):
            wrapper.failed(code)
        raise
    finally:
        try:
            if token is not None:
                otel_context.detach(token)  # type: ignore[arg-type]
            if span is not None:
                span.end()
        except Exception:
            log.warning("obs agent span not ended")
