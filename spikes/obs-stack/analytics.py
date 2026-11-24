"""Spike obs-stack round 4 (never shipped; V.obs-analytics): what MLflow 3.18.0 shows natively when our spans carry its
conventions. Runs in the spike networks (no published port, zero credit). Writes /out/analytics.json.

Synthetic traces, ids and numbers only (no text): `baseline` is today's shapes (SRV-OBS FR-OBS-3..6); `proposed` adds
the candidate conventions: root `mlflow.spanType` CHAIN, root `mlflow.traceTag.reader.*` tags, root `session.id`,
agent.call `gen_ai.operation.name` invoke_agent with semconv cache keys and input tokens that include the cache, and
(variant `with_cost`) `mlflow.llm.cost` on agent.call as a JSON string. The CLI-shaped children use VEC-OBS-2's keys.
Then: MLflow's REST view of each trace (span types, token usage, cost, model, tags, metadata), the trace searches a
saved view would use, and the trace-metrics queries behind the overview charts.
"""

import json
import time
import urllib.error
import urllib.request

from opentelemetry import context as otel_context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode

MLFLOW = "http://mlflow:5000"
OUT: dict = {"traces": {}}
provider = TracerProvider(resource=Resource({"service.name": "reader-worker", "service.version": "0.1.0"}))
provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint="http://rotel:4318/v1/traces")))
ours = provider.get_tracer("reader")
cli_provider = TracerProvider(resource=Resource({"service.name": "claude-code", "service.version": "2.1.330",
                                                  "host.arch": "arm64", "os.type": "linux", "os.version": "6"}))
cli_provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint="http://rotel:4318/v1/traces")))
cli = cli_provider.get_tracer("com.anthropic.claude_code.tracing")
NOW = time.time_ns()
MODEL = "claude-sonnet-5-5"
RUN = "10000000-0000-4000-8000-0000000000aa"


def _cli_children(start: int, ms: int, usage: tuple[int, int, int, int]) -> None:
    inp, out, cread, cwrite = usage
    with cli.start_as_current_span("claude_code.interaction", start_time=start, attributes={
        "span.type": "claude_code.interaction", "gen_ai.operation.name": "invoke_agent",
        "gen_ai.provider.name": "anthropic", "interaction.sequence": 1, "user_prompt": "<REDACTED>",
    }, end_on_exit=False) as inter:
        with cli.start_as_current_span("claude_code.llm_request", start_time=start + 1_000_000, attributes={
            "span.type": "claude_code.llm_request", "gen_ai.request.model": MODEL, "model": MODEL,
            "gen_ai.provider.name": "anthropic", "input_tokens": inp, "output_tokens": out,
            "cache_read_tokens": cread, "cache_creation_tokens": cwrite, "stop_reason": "end_turn",
        }, end_on_exit=False) as req:
            req.end(end_time=start + (ms - 1) * 1_000_000)
        inter.end(end_time=start + ms * 1_000_000)


def job(name: str, kind: str, variant: str, *, offset_s: float, ms: int, usage: tuple[int, int, int, int],
        ledger_usd: float, stage: str | None = None, outcome: str = "succeeded", code: str | None = None) -> str:
    start = NOW - int(offset_s * 1e9)
    attrs = {"reader.job.kind": kind, "reader.job.id": 100 + len(OUT["traces"]), "reader.job.attempt": 0,
             "reader.job.outcome": outcome}
    if stage:
        attrs.update({"reader.stage": stage, "reader.run_id": RUN, "reader.step": 1})
    if code:
        attrs["reader.error_code"] = code
    if variant != "baseline":
        attrs["mlflow.spanType"] = "CHAIN"
        attrs["mlflow.traceTag.mlflow.traceName"] = f"job {kind}"
        for k in ("reader.job.kind", "reader.job.outcome", "reader.stage", "reader.error_code"):
            if k in attrs:
                attrs["mlflow.traceTag." + k] = attrs[k]
        if stage:
            attrs["session.id"] = RUN
    root = ours.start_span(f"job {kind}", context=otel_context.Context(), kind=SpanKind.CONSUMER,
                           start_time=start, attributes=attrs)
    from opentelemetry import trace
    with trace.use_span(root, end_on_exit=False):
        inp, out, cread, cwrite = usage
        call_attrs = {"reader.task": "tutor" if kind == "tutor.answer" else "fact_check",
                      "gen_ai.request.model": MODEL, "gen_ai.usage.output_tokens": out,
                      "reader.cost_usd": ledger_usd, "reader.stop_reason": "end_turn"}
        if stage:
            call_attrs["reader.stage"] = stage
        if variant == "baseline":
            call_attrs.update({"gen_ai.usage.input_tokens": inp, "gen_ai.usage.cache_read_input_tokens": cread,
                               "gen_ai.usage.cache_creation_input_tokens": cwrite})
        else:
            call_attrs.update({"gen_ai.operation.name": "invoke_agent", "gen_ai.provider.name": "anthropic",
                               "mlflow.spanType": "AGENT",
                               "gen_ai.usage.input_tokens": inp + cread + cwrite,
                               "gen_ai.usage.cache_read.input_tokens": cread,
                               "gen_ai.usage.cache_write.input_tokens": cwrite})
        if variant == "with_cost":
            call_attrs["mlflow.llm.cost"] = json.dumps(
                {"input_cost": 0.0, "output_cost": 0.0, "total_cost": ledger_usd})
        call = ours.start_span("agent.call", kind=SpanKind.CLIENT, start_time=start + 2_000_000,
                               attributes=call_attrs)
        with trace.use_span(call, end_on_exit=False):
            _cli_children(start + 3_000_000, ms - 6, usage)
        if outcome in ("dead", "failed"):
            call.set_status(Status(StatusCode.ERROR))
        call.end(end_time=start + (ms - 2) * 1_000_000)
    if outcome in ("dead", "failed"):
        root.set_status(Status(StatusCode.ERROR))
    root.end(end_time=start + ms * 1_000_000)
    tid = format(root.get_span_context().trace_id, "032x")
    OUT["traces"][name] = {"trace_id": tid, "variant": variant, "ms": ms, "ledger_usd": ledger_usd,
                           "usage": usage}
    return tid


def compile_like(name: str, offset_s: float) -> str:
    """A compile-shaped trace for the timeline question: a CHAIN root, 4 stage children (two of them in parallel),
    each with agent.call → 3 × (interaction → llm_request + tool): 1 + 4 × (1 + 1 + 3 × 3) = 45 spans."""
    from opentelemetry import trace
    start = NOW - int(offset_s * 1e9)
    ms = 1_000_000
    root = ours.start_span("job pipeline.stage", context=otel_context.Context(), kind=SpanKind.CONSUMER,
                           start_time=start, attributes={"reader.job.kind": "pipeline.stage", "mlflow.spanType": "CHAIN",
                                                         "mlflow.traceTag.mlflow.traceName": "job pipeline.stage"})
    # (stage, start ms, duration ms): fact_check and review overlap (parallel children).
    plan = [("draft", 0, 30_000), ("fact_check", 31_000, 20_000), ("review", 33_000, 22_000), ("revise", 56_000, 15_000)]
    with trace.use_span(root, end_on_exit=False):
        for stage, at, dur in plan:
            s0 = start + at * ms
            st = ours.start_span(f"stage {stage}", start_time=s0, attributes={"mlflow.spanType": "CHAIN"})
            with trace.use_span(st, end_on_exit=False):
                call = ours.start_span("agent.call", start_time=s0 + ms, attributes={
                    "mlflow.spanType": "AGENT", "gen_ai.operation.name": "invoke_agent"})
                with trace.use_span(call, end_on_exit=False):
                    turn = (dur - 4_000) // 3
                    for i in range(3):
                        t0 = s0 + (2_000 + i * turn) * ms
                        inter = cli.start_span("claude_code.interaction", start_time=t0,
                                               attributes={"span.type": "claude_code.interaction"})
                        with trace.use_span(inter, end_on_exit=False):
                            req = cli.start_span("claude_code.llm_request", start_time=t0 + 100 * ms, attributes={
                                "span.type": "claude_code.llm_request", "model": MODEL, "input_tokens": 100,
                                "output_tokens": 50, "gen_ai.provider.name": "anthropic"})
                            req.end(end_time=t0 + (turn * 2 // 3) * ms)
                            tool = cli.start_span("claude_code.tool", start_time=t0 + (turn * 2 // 3 + 50) * ms,
                                                  attributes={"span.type": "claude_code.tool"})
                            tool.end(end_time=t0 + (turn - 100) * ms)
                        inter.end(end_time=t0 + (turn - 50) * ms)
                call.end(end_time=s0 + (dur - 500) * ms)
            st.end(end_time=s0 + dur * ms)
    root.end(end_time=start + 72_000 * ms)
    tid = format(root.get_span_context().trace_id, "032x")
    OUT["traces"][name] = {"trace_id": tid, "variant": "compile_like"}
    return tid


def http_span(name: str, route: str, status: int, offset_s: float, ms: int) -> None:
    start = NOW - int(offset_s * 1e9)
    span = ours.start_span(f"GET {route}", context=otel_context.Context(), kind=SpanKind.SERVER, start_time=start,
                           attributes={"http.request.method": "GET", "http.route": route,
                                       "http.response.status_code": status,
                                       "mlflow.traceTag.mlflow.traceName": f"GET {route}"})
    if status >= 500:
        span.set_status(Status(StatusCode.ERROR))
    span.end(end_time=start + ms * 1_000_000)
    OUT["traces"][name] = {"trace_id": format(span.get_span_context().trace_id, "032x"), "variant": "http"}


def call(path: str, body: dict | None = None, method: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(MLFLOW + path, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:300]}


def view(tid: str) -> dict:
    status, body = call(f"/ajax-api/3.0/mlflow/traces/get?trace_id=tr-{tid}")
    t = body.get("trace") or {}
    info = t.get("trace_info") or {}
    spans = []
    for s in t.get("spans", []):
        attrs = s.get("attributes") or {}
        if isinstance(attrs, list):
            attrs = {a["key"]: next(iter(a["value"].values())) for a in attrs}
        spans.append({"name": s["name"], **{k: attrs.get(k) for k in (
            "mlflow.spanType", "mlflow.chat.tokenUsage", "mlflow.llm.cost", "mlflow.llm.model",
            "mlflow.llm.provider") if k in attrs}})
    return {"status": status, "metadata": {k: v for k, v in (info.get("trace_metadata") or {}).items()
                                           if k.startswith("mlflow.trace.") or k in ("session.id",)},
            "tags": {k: v for k, v in (info.get("tags") or {}).items()}, "state": info.get("state"),
            "execution_duration": info.get("execution_duration"), "spans": spans}


def search(filter_string: str, order_by: list[str] | None = None) -> dict:
    body = {"locations": [{"type": "MLFLOW_EXPERIMENT", "mlflow_experiment": {"experiment_id": "0"}}],
            "max_results": 100, "filter": filter_string}
    if order_by:
        body["order_by"] = order_by
    status, res = call("/ajax-api/3.0/mlflow/traces/search", body)
    ids = [t["trace_id"].removeprefix("tr-") for t in res.get("traces", [])]
    names = {v["trace_id"]: k for k, v in OUT["traces"].items()}
    return {"status": status, "matches": [names.get(i, i) for i in ids], "error": res.get("error")}


def metrics(view_type: str, metric: str, aggs: list[dict], dims: list[str] | None = None,
            filters: list[str] | None = None) -> dict:
    body = {"experiment_ids": ["0"], "view_type": view_type, "metric_name": metric, "aggregations": aggs}
    if dims:
        body["dimensions"] = dims
    if filters:
        body["filters"] = filters
    status, res = call("/ajax-api/3.0/mlflow/traces/metrics", body)
    return {"status": status, "data_points": res.get("data_points", res.get("error"))}


def main() -> None:
    for _ in range(60):
        try:
            if urllib.request.urlopen(MLFLOW + "/health", timeout=2).status == 200:
                break
        except Exception:
            time.sleep(1)
    job("tutor_baseline", "tutor.answer", "baseline", offset_s=600, ms=4000, usage=(20, 300, 9000, 0),
        ledger_usd=0.05)
    job("tutor_fast", "tutor.answer", "proposed", offset_s=500, ms=2000, usage=(20, 200, 9000, 0), ledger_usd=0.03)
    job("tutor_slow", "tutor.answer", "proposed", offset_s=400, ms=9000, usage=(30, 900, 9000, 2000),
        ledger_usd=0.11)
    job("stage_fact_check", "pipeline.stage", "proposed", offset_s=300, ms=40000, usage=(50, 4000, 30000, 8000),
        ledger_usd=0.21, stage="fact_check")
    job("stage_review_dead", "pipeline.stage", "proposed", offset_s=200, ms=60000, usage=(50, 6000, 30000, 8000),
        ledger_usd=0.30, stage="review", outcome="dead", code="exhausted")
    job("stage_draft_with_cost", "pipeline.stage", "with_cost", offset_s=100, ms=95000,
        usage=(80, 12000, 40000, 9000), ledger_usd=0.62, stage="draft")
    compile_like("compile_like", 150)
    http_span("http_ok", "/api/obs/summary", 200, 50, 30)
    http_span("http_500", "/api/lessons/{lesson_id}", 500, 40, 12)
    provider.force_flush()
    cli_provider.force_flush()
    time.sleep(8)
    OUT["views"] = {k: view(v["trace_id"]) for k, v in OUT["traces"].items()}
    OUT["searches"] = {
        "failed (state)": search("trace.status = 'ERROR'"),
        "dead jobs (tag)": search("tag.`reader.job.outcome` = 'dead'"),
        "stage review (tag)": search("tag.`reader.stage` = 'review'"),
        "tutor (tag kind)": search("tag.`reader.job.kind` = 'tutor.answer'"),
        "tutor (trace name)": search("trace.name = 'job tutor.answer'"),
        "slow tutor > 5 s": search("trace.name = 'job tutor.answer' AND trace.execution_time_ms > 5000"),
        "slow > 5 s (alone)": search("trace.execution_time_ms > 5000"),
        "slow > 5 s (tag kind)": search("tag.`reader.job.kind` = 'tutor.answer' AND trace.execution_time_ms > 5000"),
        "slowest tutor first (tag)": search("tag.`reader.job.kind` = 'tutor.answer'", ["execution_time_ms DESC"]),
        "error code (tag)": search("tag.`reader.error_code` = 'exhausted'"),
        "span type CHAIN": search("span.type = 'CHAIN'"),
        "run session": search(f"metadata.`mlflow.trace.session` = '{RUN}'"),
        "newest first": search("trace.name = 'job pipeline.stage'", ["timestamp_ms DESC"]),
        "slowest first": search("trace.name = 'job pipeline.stage'", ["execution_time_ms DESC"]),
        "span type AGENT": search("span.type = 'AGENT'"),
        "span name agent.call": search("span.name = 'agent.call'"),
    }
    sum_ = [{"aggregation_type": "SUM"}]
    OUT["metrics"] = {
        "trace_count by name": metrics("TRACES", "trace_count", [{"aggregation_type": "COUNT"}], ["trace_name"]),
        "latency p95 by name": metrics("TRACES", "latency", [{"aggregation_type": "PERCENTILE",
                                                               "percentile_value": 95}], ["trace_name"]),
        "trace status counts": metrics("TRACES", "trace_count", [{"aggregation_type": "COUNT"}], ["trace_status"]),
        "total_tokens sum by name": metrics("TRACES", "total_tokens", sum_, ["trace_name"]),
        "span total_cost sum by span name": metrics("SPANS", "total_cost", sum_, ["span_name"]),
        "span total_cost sum by model": metrics("SPANS", "total_cost", sum_, ["span_model_name"]),
        "span count by type": metrics("SPANS", "span_count", [{"aggregation_type": "COUNT"}], ["span_type"]),
        "span latency p95 by type": metrics("SPANS", "latency", [{"aggregation_type": "PERCENTILE",
                                                                   "percentile_value": 95}], ["span_type"]),
        "tokens by name (tutor)": metrics("TRACES", "total_tokens", sum_, ["trace_name"],
                                          ["trace.tag.`reader.job.kind` = 'tutor.answer'"]),
        "tagged: trace_count of dead": metrics("TRACES", "trace_count", [{"aggregation_type": "COUNT"}], None,
                                               ["trace.tag.`reader.job.outcome` = 'dead'"]),
    }
    with open("/out/analytics.json", "w") as f:
        json.dump(OUT, f, indent=1, default=str)
    with open("/out/browser-ids.json", "w") as f:
        json.dump({k: v["trace_id"] for k, v in OUT["traces"].items()}, f)
    print("done", len(OUT["traces"]))


if __name__ == "__main__":
    main()
