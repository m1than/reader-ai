"""Spike probe: a "lesson-compile-like" chain of small stages. Each stage is our OTel span `reader.stage`; inside it
one Agent SDK query runs, and the SDK passes TRACEPARENT to the CLI so the CLI's spans nest under the stage.
One stage is seeded to fail (an invalid model). A hard spend cap is enforced across stages.
Prints one JSON object: trace id, stage span ids, per-stage cost and errors. Never prints the environment.
"""

import asyncio
import json
import os
import time

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

CAP_USD = float(os.environ.get("CAP_USD", "0.5"))
NO_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "Task", "TodoWrite", "NotebookEdit"]
STAGES = [  # name, model, prompt
    ("draft", "claude-haiku-5-5", "In two sentences, explain what a hash function is."),
    ("fact_check", "claude-nonexistent-0-0", "Check the previous answer."),  # seeded failure: invalid model
    ("review", "claude-haiku-5-5", "Give one short review comment on: 'A hash maps data to a fixed-size value.'"),
]

provider = TracerProvider(resource=Resource.create({"service.name": "reader-ai", "reader.run": os.environ.get("RUN_ID", "")}))
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=os.environ["READER_OTLP_TRACES"])))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("reader.spike.trace-backend")


async def run_stage(name: str, model: str, prompt: str) -> dict[str, object]:
    row: dict[str, object] = {"stage": name, "model": model}
    with tracer.start_as_current_span(f"reader.stage {name}") as span:
        span.set_attribute("reader.stage", name)
        span.set_attribute("gen_ai.request.model", model)
        ctx = span.get_span_context()
        row["span_id"] = format(ctx.span_id, "016x")
        t0 = time.monotonic()
        try:
            opts = ClaudeAgentOptions(model=model, max_turns=1, allowed_tools=[], disallowed_tools=NO_TOOLS,
                                      setting_sources=[])
            async for msg in query(prompt=prompt, options=opts):
                if isinstance(msg, ResultMessage):
                    row["cost_usd"] = msg.total_cost_usd or 0.0
                    row["is_error"] = msg.is_error
                    if msg.is_error:
                        span.set_status(Status(StatusCode.ERROR, f"agent result error: {msg.result}"[:300]))
        except Exception as exc:  # the seeded failure lands here or as an error result
            row["error"] = f"{type(exc).__name__}: {exc}"[:300]
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, row["error"]))
        row["seconds"] = round(time.monotonic() - t0, 2)
    return row


async def main() -> None:
    rows, spent = [], 0.0
    with tracer.start_as_current_span("reader.job lesson-compile-spike") as job:
        trace_id = format(job.get_span_context().trace_id, "032x")
        job_span = format(job.get_span_context().span_id, "016x")
        for name, model, prompt in STAGES:
            if spent >= CAP_USD:
                rows.append({"stage": name, "skipped": f"spend cap {CAP_USD} reached"})
                continue
            row = await run_stage(name, model, prompt)
            spent += float(row.get("cost_usd") or 0.0)
            rows.append(row)
    provider.force_flush()
    print(json.dumps({"trace_id": trace_id, "job_span_id": job_span, "spent_usd": round(spent, 6), "stages": rows}))


asyncio.run(main())
