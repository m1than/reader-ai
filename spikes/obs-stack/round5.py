"""Spike obs-stack round 5 (never shipped): the V.obs-analytics design review, round 1 (B1, B2, B8). Runs in the driver
container (spike networks, no published port, zero credit). PHASE selects the work; each writes /out/<PHASE>.json.

  cost     B1: what MLflow prices a CLI-shaped `claude_code.llm_request` at, under the catalog the stack was started
           with: our pricing model, a bundled model absent from pricing.yaml, no provider key, an unknown model; plus
           an `agent.call` with the ledger's `mlflow.llm.cost` and the span cost metric over those traces.
  session  B2: the CLI spans carry VEC-OBS-2's full key sets, `session.id` and `gen_ai.conversation.id` included.
           (s1) exported first, then the root with `session.id` = the run; (s2) a zero-length `job.session` child
           with the run's `session.id`, exported before the CLI starts (1.5 s ahead), then the CLI, then the root;
           (s3) as s2 with no gap.
  bulk     B8: BULK compile-like traces (45 spans each) through a batch exporter, for Jaeger's memory per span.
  probe    B8: PROBE tutor-like traces; seconds until MLflow returns each one complete (Jaeger stopped or OOM-killed).
"""

import json
import os
import sys
import time
import uuid

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind

sys.path.insert(0, "/spike")
import analytics as a  # noqa: E402

PHASE = os.environ["PHASE"]
MS = 1_000_000
OUT: dict = {"phase": PHASE, "catalog": os.environ.get("CATALOG_NOTE", "")}
RUN = "10000000-0000-4000-8000-0000000000aa"


def wait_mlflow() -> None:
    import urllib.request
    for _ in range(90):
        try:
            if urllib.request.urlopen(a.MLFLOW + "/health", timeout=2).status == 200:
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)


def tid_of(span: trace.Span) -> str:
    return format(span.get_span_context().trace_id, "032x")


def root(name: str, start: int, attrs: dict) -> trace.Span:
    return a.ours.start_span(name, context=otel_context.Context(), kind=SpanKind.CONSUMER, start_time=start,
                             attributes={"mlflow.spanType": "CHAIN", **attrs})


def cli_full(start: int, ms: int, model: str | None, provider: bool, session: str) -> None:
    """VEC-OBS-2's key sets with plausible values (ids, counts, enums; `user_prompt` the CLI's literal redaction)."""
    common = {"session.id": session, "gen_ai.conversation.id": session, "user.id": "u" * 64, "terminal.type": "none",
              "anthropic.custom_base_url": "false"}
    if provider:
        common.update({"gen_ai.provider.name": "anthropic", "anthropic.provider": "firstParty"})
    with a.cli.start_as_current_span("claude_code.interaction", start_time=start, end_on_exit=False, attributes={
        **common, "span.type": "claude_code.interaction", "gen_ai.operation.name": "invoke_agent",
        "interaction.sequence": 1, "interaction.duration_ms": ms, "parent.source": "env", "queued_sends": 0,
        "user_prompt": "<REDACTED>", "user_prompt_length": 1200}) as inter:
        req_attrs = {**common, "span.type": "claude_code.llm_request", "attempt": 1, "input_tokens": 50,
                     "output_tokens": 4000, "cache_read_tokens": 30000, "cache_creation_tokens": 8000,
                     "duration_ms": ms - 2, "effort": "medium", "first_content_ms": 900, "ttft_ms": 900,
                     "gen_ai.response.finish_reasons": "end_turn", "llm_request.context": "main",
                     "query_source_safe": "sdk", "speed": "normal", "stop_reason": "end_turn", "success": True}
        if model:
            req_attrs.update({"gen_ai.request.model": model, "model": model})
        if provider:
            req_attrs["gen_ai.system"] = "anthropic"
        with a.cli.start_as_current_span("claude_code.llm_request", start_time=start + MS, attributes=req_attrs,
                                         end_on_exit=False) as req:
            req.end(end_time=start + (ms - 1) * MS)
        inter.end(end_time=start + ms * MS)


def phase_cost() -> None:
    cases = [("pricing_model", a.MODEL, True), ("bundled_not_in_pricing", "claude-haiku-4-5", True),
             ("no_provider", a.MODEL, False), ("unknown_model", "claude-none-0-0", True)]
    ids = {}
    for i, (name, model, prov) in enumerate(cases):
        start = time.time_ns() - (300 - i * 10) * 10**9
        r = root("job tutor.answer", start, {"mlflow.traceTag.mlflow.traceName": f"cost {name}",
                                             "mlflow.traceTag.spike.case": f"cost-{os.environ.get('TAG', '')}"})
        with trace.use_span(r, end_on_exit=False):
            call = a.ours.start_span("agent.call", start_time=start + MS, attributes={
                "mlflow.spanType": "AGENT", "gen_ai.operation.name": "invoke_agent", "gen_ai.provider.name": "anthropic",
                "gen_ai.request.model": a.MODEL, "gen_ai.usage.input_tokens": 38050, "gen_ai.usage.output_tokens": 4000,
                "mlflow.llm.cost": json.dumps({"input_cost": 0.08, "output_cost": 0.04, "total_cost": 0.12})})
            with trace.use_span(call, end_on_exit=False):
                cli_full(start + 2 * MS, 8000, model, prov, str(uuid.uuid4()))
            call.end(end_time=start + 8100 * MS)
        r.end(end_time=start + 8200 * MS)
        ids[name] = tid_of(r)
    a.provider.force_flush()
    a.cli_provider.force_flush()
    time.sleep(10)
    for name, tid in ids.items():
        v = a.view(tid)
        OUT[name] = {"trace_cost": v["metadata"].get("mlflow.trace.cost"),
                     "spans": {s["name"]: s.get("mlflow.llm.cost") for s in v["spans"]},
                     "provider": {s["name"]: s.get("mlflow.llm.provider") for s in v["spans"]}}
    OUT["span_cost_sum_by_model"] = a.metrics("SPANS", "total_cost", [{"aggregation_type": "SUM"}], ["span_model_name"],
                                              [f"trace.tag.`spike.case` = 'cost-{os.environ.get('TAG', '')}'"])
    OUT["expected_ledger_sum"] = round(0.12 * len(cases), 6)


def phase_session() -> None:
    res = {}
    for case, marker, gap_ms in (("s1_cli_first", False, 0), ("s2_marker_1500ms", True, 1500),
                                 ("s3_marker_no_gap", True, 0)):
        start = time.time_ns() - 200 * 10**9
        cli_session = str(uuid.uuid4())
        r = root("job pipeline.stage", start, {"session.id": RUN, "mlflow.traceTag.mlflow.traceName": case})
        with trace.use_span(r, end_on_exit=False):
            if marker:
                m = a.ours.start_span("job.session", start_time=start + MS, attributes={"session.id": RUN})
                m.end(end_time=start + MS)
                a.provider.force_flush()
                time.sleep(gap_ms / 1000)
            call = a.ours.start_span("agent.call", start_time=start + 2 * MS, attributes={"mlflow.spanType": "AGENT"})
            with trace.use_span(call, end_on_exit=False):
                cli_full(start + 3 * MS, 5000, a.MODEL, True, cli_session)
            a.cli_provider.force_flush()
            time.sleep(1.5)
            call.end(end_time=start + 5100 * MS)
        r.end(end_time=start + 5200 * MS)
        a.provider.force_flush()
        res[case] = {"trace": tid_of(r), "cli_session": cli_session}
    time.sleep(10)
    for case, d in res.items():
        v = a.view(d["trace"])
        got = v["metadata"].get("mlflow.trace.session")
        OUT[case] = {"session": got, "is_run": got == RUN, "is_cli": got == d["cli_session"]}
    OUT["sessions_search"] = a.call("/ajax-api/3.0/mlflow/traces/search", {
        "locations": [{"type": "MLFLOW_EXPERIMENT", "mlflow_experiment": {"experiment_id": "0"}}],
        "max_results": 50, "filter": f"metadata.`mlflow.trace.session` = '{RUN}'"})[0]


def batch_tracers() -> None:
    p = TracerProvider(resource=Resource({"service.name": "reader-worker", "service.version": "0.1.0"}))
    p.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint="http://rotel:4318/v1/traces"),
                                            max_queue_size=20000, max_export_batch_size=512))
    c = TracerProvider(resource=Resource({"service.name": "claude-code", "service.version": "2.1.330"}))
    c.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint="http://rotel:4318/v1/traces"),
                                            max_queue_size=20000, max_export_batch_size=512))
    a.ours, a.cli, a.provider, a.cli_provider = p.get_tracer("reader"), c.get_tracer("cc"), p, c


def phase_bulk() -> None:
    batch_tracers()
    n = int(os.environ.get("BULK", "200"))
    t0 = time.time()
    for i in range(n):
        a.compile_like(f"bulk_{i}", 140 - i * 0.001)
        if i % 50 == 49:
            a.provider.force_flush()
            a.cli_provider.force_flush()
    a.provider.force_flush()
    a.cli_provider.force_flush()
    OUT.update({"traces": n, "spans": n * 45, "seconds": round(time.time() - t0, 1)})


def phase_bulkfull() -> None:
    """B8: BULK tutor-shaped traces whose CLI spans carry VEC-OBS-2's full key sets (4 spans each)."""
    batch_tracers()
    n = int(os.environ.get("BULK", "1000"))
    t0 = time.time()
    for i in range(n):
        start = time.time_ns() - 100 * 10**9
        r = root("job tutor.answer", start, {"reader.job.kind": "tutor.answer", "reader.job.id": 1000 + i,
                                              "reader.job.attempt": 0, "reader.job.outcome": "succeeded",
                                              "reader.question_id": str(uuid.uuid4())})
        with trace.use_span(r, end_on_exit=False):
            call = a.ours.start_span("agent.call", start_time=start + MS, attributes={
                "reader.task": "tutor", "gen_ai.request.model": a.MODEL, "gen_ai.usage.input_tokens": 9020,
                "gen_ai.usage.output_tokens": 366, "reader.cost_usd": 0.03, "reader.stop_reason": "end_turn"})
            with trace.use_span(call, end_on_exit=False):
                cli_full(start + 2 * MS, 2000, a.MODEL, True, str(uuid.uuid4()))
            call.end(end_time=start + 2100 * MS)
        r.end(end_time=start + 2200 * MS)
        if i % 100 == 99:
            a.provider.force_flush()
            a.cli_provider.force_flush()
    a.provider.force_flush()
    a.cli_provider.force_flush()
    OUT.update({"traces": n, "spans": n * 4, "seconds": round(time.time() - t0, 1)})


def phase_probe() -> None:
    batch_tracers()
    n = int(os.environ.get("PROBE", "5"))
    t0 = time.time()
    tids = [a.job(f"probe_{i}", "tutor.answer", "proposed", offset_s=5, ms=2000, usage=(20, 200, 9000, 0),
                  ledger_usd=0.03) for i in range(n)]
    a.provider.force_flush()
    a.cli_provider.force_flush()
    done: dict[str, float] = {}
    while time.time() - t0 < 60 and len(done) < n:
        for t in tids:
            if t not in done and len(a.view(t).get("spans", [])) >= 3:
                done[t] = round(time.time() - t0, 1)
        time.sleep(0.5)
    OUT.update({"sent": n, "complete": len(done), "seconds": sorted(done.values()), "within_30s": all(
        s <= 30 for s in done.values()) and len(done) == n})


def phase_gate() -> None:
    """B8: per-site origins through Caddy. POST (non-GET needs the exact origin) with the owner cookie."""
    import urllib.error
    import urllib.request
    rows = []
    for port, site in ((8446, "mlflow"), (8448, "jaeger")):
        for origin in ("http://localhost:8446", "http://localhost:8448", None):
            for spoof in (None, "mlflow", "jaeger"):
                headers = {"Cookie": "__Host-reader_session=owner", "Host": f"localhost:{port}"}
                if origin:
                    headers["Origin"] = origin
                if spoof:
                    headers["X-Reader-Site"] = spoof
                req = urllib.request.Request(f"http://caddy:{port}/__probe", data=b"{}", method="POST", headers=headers)
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:
                        status = r.status
                except urllib.error.HTTPError as e:
                    status = e.code
                except Exception as e:  # noqa: BLE001
                    status = str(e)[:80]
                rows.append({"site": site, "origin": origin, "client_x_reader_site": spoof,
                             "status": status, "passed_gate": status not in (401, 403)})
    OUT["rows"] = rows


def main() -> None:
    wait_mlflow()
    {"cost": phase_cost, "session": phase_session, "bulk": phase_bulk, "bulkfull": phase_bulkfull,
     "probe": phase_probe, "gate": phase_gate}[PHASE]()
    tag = os.environ.get("TAG", "")
    json.dump(OUT, open(f"/out/{PHASE}{tag}.json", "w"), indent=1, default=str)
    print("done", PHASE, tag)


if __name__ == "__main__":
    main()
