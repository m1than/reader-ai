"""Synthetic span replay (no model calls): one lesson-compile-sized trace through Rotel, shaped like the probe's
traces. ADR-0018 estimates about 7 stages × ~70 spans per lesson: here 1 job + 7 stages × (10 interactions × 7 spans)
= 498 spans, with gen_ai.* token attributes on the llm_request spans. Prints the trace id and span count.

    python replay.py   (env READER_OTLP_TRACES=http://rotel:4318/v1/traces)
"""

import json
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

provider = TracerProvider(resource=Resource.create({"service.name": "reader-ai", "reader.run": "replay"}))
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=os.environ["READER_OTLP_TRACES"])))
tracer = provider.get_tracer("reader.spike.trace-backend.replay")
STAGES = ["draft", "fact_check", "review", "revise", "fact_check_2", "review_2", "verify_labs"]

count = 0
with tracer.start_as_current_span("reader.job lesson-compile-replay") as job:
    count += 1
    trace_id = format(job.get_span_context().trace_id, "032x")
    for stage in STAGES:
        with tracer.start_as_current_span(f"reader.stage {stage}") as s:
            s.set_attribute("reader.stage", stage)
            count += 1
            for turn in range(10):
                with tracer.start_as_current_span("claude_code.interaction"):
                    count += 1
                    for call in range(3):
                        with tracer.start_as_current_span("claude_code.llm_request") as r:
                            r.set_attribute("gen_ai.request.model", "claude-haiku-5-5")
                            r.set_attribute("gen_ai.usage.input_tokens", 9800 + call)
                            r.set_attribute("gen_ai.usage.output_tokens", 120 + turn)
                            count += 1
                    for _ in range(1):
                        with tracer.start_as_current_span("claude_code.tool"):
                            count += 1
                            with tracer.start_as_current_span("claude_code.tool.execution"):
                                count += 1
                            with tracer.start_as_current_span("claude_code.tool.blocked_on_user"):
                                count += 1
provider.force_flush()
print(json.dumps({"trace_id": trace_id, "spans": count}))
