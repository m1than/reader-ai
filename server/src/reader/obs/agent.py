"""The `agent.call` span (SRV-OBS FR-OBS-6, FR-OBS-12 b-c; SRV-AGENT FR-AGENT-14): current while the CLI runs,
so the Agent SDK injects its TRACEPARENT and the CLI's spans nest under it. It carries the GenAI semconv
tokens, the settled cost and its input part (ours: the CLI's spans carry none), the lesson, course and step,
the stop reason and the error code. A failure of a helper never reaches the call."""

from __future__ import annotations

import functools
import logging
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Final

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from reader.obs import tracer

log = logging.getLogger("reader.obs")
_MICRO: Final = Decimal("0.000001")


def input_part(settled: Decimal, usage: Any, rates: Mapping[str, Decimal]) -> Decimal:
    """FR-OBS-12 (c): the input share of the settled amount, by pricing.yaml's rates for the call's tokens
    (each cache write at its own TTL's rate); 0 when the call priced nothing (the whole amount is output)."""
    priced_in = (
        Decimal(int(usage.input)) * rates["input"]
        + Decimal(int(usage.cache_read)) * rates["cache_read"]
        + Decimal(int(usage.cache_write_5m)) * rates["cache_write_5m"]
        + Decimal(int(usage.cache_write_1h)) * rates["cache_write_1h"]
    )
    priced_out = Decimal(int(usage.output)) * rates["output"]
    whole = priced_in + priced_out
    if whole == 0:
        return Decimal(0).quantize(_MICRO)
    return min((settled * priced_in / whole).quantize(_MICRO, ROUND_HALF_EVEN), settled)


class AgentSpan:
    def __init__(self, span: Span | None, model: str) -> None:
        self._span, self._model = span, model

    def settled(self, usage: Any, settled_usd: Decimal | None, stop_reason: str | None) -> None:
        try:
            if self._span is None:
                return
            if usage is not None:
                cached = int(usage.cache_read) + int(usage.cache_write_5m) + int(usage.cache_write_1h)
                self._span.set_attribute("gen_ai.usage.input_tokens", int(usage.input) + cached)
                self._span.set_attribute("gen_ai.usage.output_tokens", int(usage.output))
                self._span.set_attribute("gen_ai.usage.cache_read.input_tokens", int(usage.cache_read))
                written = int(usage.cache_write_5m) + int(usage.cache_write_1h)
                self._span.set_attribute("gen_ai.usage.cache_write.input_tokens", written)
            if settled_usd is not None:
                self._span.set_attribute("reader.cost_usd", float(settled_usd))
                part = Decimal(0)
                if usage is not None:
                    from reader.agent.pricing import rates

                    part = input_part(Decimal(settled_usd), usage, rates(self._model))
                self._span.set_attribute("reader.cost_input_usd", float(part))
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


@functools.cache
def pipeline_stages() -> frozenset[str]:
    """SRV-PIPELINE's declared stage names (the allow-list's `pipeline_stages`)."""
    from reader.pipeline.stages import load

    return frozenset(load().stages)


@contextmanager
def agent_call(
    task: str,
    stage: str | None,
    model: str,
    lesson_id: uuid.UUID | None = None,
    course_id: uuid.UUID | None = None,
    step: int | None = None,
) -> Iterator[AgentSpan]:
    span: Span | None = None
    token: object = None
    try:
        attributes: dict[str, Any] = {
            "reader.task": task, "gen_ai.request.model": model, "gen_ai.operation.name": "invoke_agent",
            "gen_ai.provider.name": "anthropic",
        }
        if stage and stage in pipeline_stages():  # the tutor's calls carry no stage (round 1)
            attributes["reader.stage"] = stage
        if lesson_id is not None:
            attributes["reader.lesson.id"] = str(lesson_id)
        if course_id is not None:
            attributes["reader.course.id"] = str(course_id)
        if step is not None:
            attributes["reader.step"] = int(step)
        span = tracer().start_span(
            "agent.call", kind=SpanKind.CLIENT, attributes=attributes, record_exception=False,
            set_status_on_exception=False,
        )
        token = otel_context.attach(trace.set_span_in_context(span))
    except Exception:
        log.warning("obs agent span not started")
    wrapper = AgentSpan(span, model)
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
