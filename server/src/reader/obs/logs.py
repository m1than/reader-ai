"""The trace-id log filter (SRV-OBS FR-OBS-4): ` trace=<32 hex>` on every record emitted while a recording
span is current; nothing else is added, and a record passing twice gets one suffix."""

from __future__ import annotations

import logging

from opentelemetry import trace


class TraceFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "_reader_trace", False) or not isinstance(record.msg, str):
            return True
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if span.is_recording() and ctx.is_valid:
            record.msg = f"{record.msg} trace={ctx.trace_id:032x}"
            record._reader_trace = True
        return True
