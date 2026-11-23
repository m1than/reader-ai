"""Spike driver (never shipped). Runs inside the spike networks and writes /out/driver.json:

1. auth: requests through Caddy (forward_auth to the stub gate) with no cookie, a learner cookie, the owner cookie,
   blocked paths and a foreign Host header;
2. sustained: one lesson compile spread over DURATION_S in real time, shaped as SRV-OBS proposes (one trace per job:
   7 `pipeline.stage` job traces of 72 spans each, children exported before their root), plus a `tutor.answer` trace
   every 15 s and a UI-like poller (search + get every 5 s); then a completeness check per trace;
3. bulk: BULK_LESSONS lessons (7 × 72 spans each) as fast as the exporter goes, then the store size;
4. retention: delete every trace older than now through the client, then the store size again.
No model calls; no content attributes; ids only.
"""

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

from mlflow import MlflowClient
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

DURATION_S = float(os.environ.get("DURATION_S", "480"))
BULK = int(os.environ.get("BULK_LESSONS", "20"))
STAGES = ["draft", "fact_check", "review", "revise", "fact_check_2", "review_2", "verify_labs"]
OUT: dict = {"backend": os.environ.get("BACKEND"), "duration_s": DURATION_S, "bulk_lessons": BULK}
SAME = "http://localhost:8443"  # the dashboard origin in the spike (plain HTTP); https://localhost:8443 in the stack

provider = TracerProvider(resource=Resource.create({"service.name": "reader-ai", "service.namespace": "spike"}))
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint="http://rotel:4318/v1/traces")))
tracer = provider.get_tracer("reader.spike.obs-stack")
client = MlflowClient(tracking_uri="http://mlflow:5000")


def edge(path: str, cookie: str | None = None, host: str = "localhost:8443", method: str = "GET",
         body: bytes | None = None, origin: str | None = SAME, ctype: str = "application/json",
         base: str = "http://caddy:8443", auth_header: bool = False) -> dict:
    req = urllib.request.Request(f"{base}{path}", data=body, method=method)
    req.add_header("Host", host)
    if cookie:
        req.add_header("Cookie", f"__Host-reader_session={cookie}")
    if auth_header:
        req.add_header("Authorization", "Bearer spike-not-a-secret")
    if origin is not None:
        req.add_header("Origin", origin)
    if body is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
            status, headers = r.status, r.headers
    except urllib.error.HTTPError as e:
        data, status, headers = e.read(), e.code, e.headers
    out = {"status": status, "bytes": len(data), "mlflow_ui": b'id="root"' in data,
           "acao": headers.get("Access-Control-Allow-Origin"), "set_cookie": headers.get("Set-Cookie") is not None}
    if path.startswith("/__echo") and status == 200:
        out["upstream_saw"] = json.loads(data)["header_names"]
    return out


def wait_ready() -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < 120:
        try:
            with urllib.request.urlopen("http://mlflow:5000/health", timeout=2) as r:
                if r.status == 200:
                    return round(time.monotonic() - t0, 1)
        except Exception:
            time.sleep(0.5)
    raise SystemExit("mlflow not ready within 120 s")


def auth() -> dict:
    search = json.dumps({"max_results": 5}).encode()
    s = "/ajax-api/2.0/mlflow/experiments/search"
    foreign = "http://localhost:3000"
    return {
        "no_cookie_root": edge("/", origin=None),
        "learner_root": edge("/", "learner", origin=None),
        "owner_root": edge("/", "owner", origin=None),
        "owner_ajax_search": edge(s, "owner", method="POST", body=search),
        "no_cookie_ajax_search": edge(s, None, method="POST", body=search),
        "owner_post_no_origin": edge(s, "owner", method="POST", body=search, origin=None),
        "owner_post_foreign_localhost_origin": edge(s, "owner", method="POST", body=search, origin=foreign),
        "owner_post_app_origin": edge(s, "owner", method="POST", body=search, origin="https://localhost"),
        "owner_get_foreign_origin": edge("/ajax-api/2.0/mlflow/experiments/search?max_results=1", "owner",
                                         origin=foreign),
        "owner_get_same_origin_acao": edge("/ajax-api/2.0/mlflow/experiments/search?max_results=1", "owner"),
        # The threat B2 closes: MLflow itself answers a foreign localhost Origin with credentialed CORS.
        "direct_mlflow_foreign_origin_acao": edge("/ajax-api/2.0/mlflow/experiments/search?max_results=1", None,
                                                  origin=foreign, base="http://mlflow:5000", host="mlflow:5000"),
        "owner_text_plain_search": edge(s, "owner", method="POST", body=search, ctype="text/plain"),
        "direct_text_plain_search": edge(s, None, method="POST", body=search, ctype="text/plain", origin=None,
                                         base="http://mlflow:5000", host="mlflow:5000"),
        "direct_text_plain_delete": edge("/api/2.0/mlflow/traces/delete-traces", None, method="POST",
                                         body=json.dumps({"experiment_id": "0", "max_timestamp_millis": 1}).encode(),
                                         ctype="text/plain", origin=None, base="http://mlflow:5000",
                                         host="mlflow:5000"),
        "owner_echo_cookie_auth": edge("/__echo", "owner", auth_header=True),
        "owner_ingest_blocked": edge("/v1/traces", "owner", method="POST", body=b"{}"),
        "owner_gateway_blocked": edge("/gateway/x", "owner"),
        "owner_assistant_blocked": edge("/ajax-api/3.0/mlflow/assistant/x", "owner"),
        "owner_mcp_blocked": edge("/mcp", "owner"),
        "owner_api_gateway_blocked": edge("/api/2.0/mlflow/gateway/x", "owner"),
        "owner_foreign_host": edge("/", "owner", host="evil.example", origin=None),
    }


def rest_delete(body: dict) -> dict:
    req = urllib.request.Request("http://mlflow:5000/api/2.0/mlflow/traces/delete-traces",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.load(r)
    return {"response": out, "s": round(time.monotonic() - t0, 3)}


def retention() -> dict:
    """An old trace (40 days back) and a fresh one; REST delete with the 30-day cutoff, first with max_traces 1."""
    now_ns = time.time_ns()
    old_ns = now_ns - 40 * 86400 * 10**9
    root = tracer.start_span("job obs.old", start_time=old_ns)
    old_id = format(root.get_span_context().trace_id, "032x")
    root.end(end_time=old_ns + 10**9)
    with tracer.start_as_current_span("job obs.fresh") as fresh:
        fresh_id = format(fresh.get_span_context().trace_id, "032x")
    provider.force_flush()
    time.sleep(10)
    cutoff = int(time.time() * 1000) - 30 * 86400 * 1000

    def exists(tid: str) -> bool:
        try:
            client.get_trace("tr-" + tid)
            return True
        except Exception:
            return False

    before = {"old": exists(old_id), "fresh": exists(fresh_id)}
    first = rest_delete({"experiment_id": "0", "max_timestamp_millis": cutoff, "max_traces": 1})
    second = rest_delete({"experiment_id": "0", "max_timestamp_millis": cutoff})
    return {"before": before, "delete_max_traces_1": first, "delete_rest": second,
            "after": {"old": exists(old_id), "fresh": exists(fresh_id)}}


def linked_pair() -> dict:
    """A request span and a job trace linked to it (FR-OBS-5), for the browser pass."""
    from opentelemetry.trace import Link
    with tracer.start_as_current_span("POST /api/lessons/{lesson_id}/questions") as req:
        ctx = req.get_span_context()
    from opentelemetry import context as otel_context
    with tracer.start_as_current_span("job tutor.answer", context=otel_context.Context(), links=[Link(ctx)]) as job:
        with tracer.start_as_current_span("agent.call"):
            with tracer.start_as_current_span("claude_code.interaction"):
                with tracer.start_as_current_span("claude_code.llm_request"):
                    pass
        jid = format(job.get_span_context().trace_id, "032x")
    provider.force_flush()
    return {"request_trace": format(ctx.trace_id, "032x"), "job_trace": jid}


def stage_trace(stage: str, seconds: float) -> str:
    """One job trace: pipeline.stage root → reader.stage → 10 × (interaction → 3 llm_request + tool → 2)."""
    with tracer.start_as_current_span("pipeline.stage") as root:
        root.set_attribute("reader.job.kind", "pipeline.stage")
        root.set_attribute("reader.stage", stage)
        tid = format(root.get_span_context().trace_id, "032x")
        with tracer.start_as_current_span(f"reader.stage {stage}") as s:
            s.set_attribute("reader.stage", stage)
            for turn in range(10):
                t0 = time.time_ns()
                with tracer.start_as_current_span("claude_code.interaction"):
                    for _ in range(3):
                        with tracer.start_as_current_span("claude_code.llm_request") as r:
                            r.set_attribute("gen_ai.request.model", "claude-sonnet-5-5")
                            r.set_attribute("input_tokens", 9800)
                            r.set_attribute("output_tokens", 120 + turn)
                    with tracer.start_as_current_span("claude_code.tool"):
                        with tracer.start_as_current_span("claude_code.tool.execution"):
                            pass
                        with tracer.start_as_current_span("claude_code.tool.blocked_on_user"):
                            pass
                    left = seconds / 10 - (time.time_ns() - t0) / 1e9
                    if left > 0:
                        time.sleep(left)
    return tid


def tutor_loop(stop: threading.Event, ids: list[str]) -> None:
    while not stop.wait(15):
        with tracer.start_as_current_span("tutor.answer") as root:
            root.set_attribute("reader.job.kind", "tutor.answer")
            ids.append(format(root.get_span_context().trace_id, "032x"))
            with tracer.start_as_current_span("claude_code.interaction"):
                with tracer.start_as_current_span("claude_code.llm_request"):
                    time.sleep(0.2)


def poller(stop: threading.Event, lat: list[float], errors: list[str]) -> None:
    while not stop.wait(5):
        t0 = time.monotonic()
        try:
            found = client.search_traces(locations=["0"], max_results=20)
            if found:
                client.get_trace(found[0].info.trace_id)
            lat.append(round(time.monotonic() - t0, 3))
        except Exception as e:  # recorded, not raised
            errors.append(type(e).__name__)


def completeness(ids: list[str], expect: int) -> dict:
    rows = []
    for tid in ids:
        try:
            t = client.get_trace("tr-" + tid)
            names = [s.name for s in t.data.spans]
            rows.append({"spans": len(names), "root": next((s.name for s in t.data.spans if s.parent_id is None), None),
                         "state": str(t.info.state), "duration_ms": t.info.execution_duration})
        except Exception as e:
            rows.append({"error": type(e).__name__})
    return {"expected_spans": expect, "complete": sum(1 for r in rows if r.get("spans") == expect),
            "traces": len(ids), "rows": rows}


def store_bytes() -> int:
    out = subprocess.run(["du", "-sb", "/mlflowdata"], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else -1


def count_traces() -> int:
    n, token = 0, None
    while True:
        page = client.search_traces(locations=["0"], max_results=500, page_token=token)
        n += len(page)
        token = page.token
        if not token:
            return n


def main() -> None:
    OUT["ready_s"] = wait_ready()
    OUT["auth"] = auth()
    # 2. sustained, in real time
    stop, tutor_ids, lat, errors = threading.Event(), [], [], []
    threads = [threading.Thread(target=tutor_loop, args=(stop, tutor_ids)),
               threading.Thread(target=poller, args=(stop, lat, errors))]
    for t in threads:
        t.start()
    OUT["sustained_start"] = time.time()
    stage_ids = [stage_trace(s, DURATION_S / len(STAGES)) for s in STAGES]
    stop.set()
    for t in threads:
        t.join()
    OUT["sustained_end"] = time.time()
    provider.force_flush()
    time.sleep(10)
    OUT["stage_traces"] = completeness(stage_ids, 72)
    OUT["tutor_traces"] = completeness(tutor_ids, 3)
    lat.sort()
    OUT["poll"] = {"n": len(lat), "p50_s": lat[len(lat) // 2] if lat else None,
                   "p95_s": lat[int(len(lat) * 0.95)] if lat else None, "errors": errors}
    OUT["store_bytes_after_sustained"] = store_bytes()
    # 3. bulk
    # Round 1 emitted all lessons at once and overflowed the SDK's own 2,048-span queue ("Queue full, dropping
    # Span"); a flush after each lesson (504 spans) keeps every span, so bytes per span are exact.
    t0 = time.monotonic()
    OUT["bulk_start"] = time.time()
    bulk_ids = []
    for _ in range(BULK):
        bulk_ids += [stage_trace(s, 0) for s in STAGES]
        provider.force_flush()
    OUT["bulk_emit_s"] = round(time.monotonic() - t0, 1)
    OUT["bulk_spans"] = len(bulk_ids) * 72
    time.sleep(20)
    OUT["bulk_end"] = time.time()
    OUT["bulk_traces"] = {k: v for k, v in completeness(bulk_ids, 72).items() if k != "rows"}
    OUT["traces_total"] = count_traces()
    OUT["store_bytes_after_bulk"] = store_bytes()
    # 4. retention with a real cutoff (review B6), then the linked pair for the browser pass (review B4)
    OUT["retention_cutoff"] = retention()
    if os.environ.get("KEEP_TRACES"):
        OUT["linked"] = linked_pair()
        OUT["sample_stage_trace"] = stage_ids[0]
        time.sleep(10)
        with open("/out/driver.json", "w") as f:
            json.dump(OUT, f, indent=2, default=str)
        with open("/out/browser-ids.json", "w") as f:
            json.dump({"job_trace": OUT["linked"]["job_trace"], "request_trace": OUT["linked"]["request_trace"],
                       "stage_trace": stage_ids[0]}, f)
        print(json.dumps({"kept": True}))
        return
    t0 = time.monotonic()
    OUT["deleted"] = client.delete_traces(experiment_id="0", max_timestamp_millis=int(time.time() * 1000))
    OUT["delete_s"] = round(time.monotonic() - t0, 2)
    OUT["traces_after_delete"] = count_traces()
    OUT["store_bytes_after_delete"] = store_bytes()
    with open("/out/driver.json", "w") as f:
        json.dump(OUT, f, indent=2, default=str)
    print(json.dumps({k: OUT[k] for k in ("backend", "ready_s", "deleted", "traces_total")}))


main()
