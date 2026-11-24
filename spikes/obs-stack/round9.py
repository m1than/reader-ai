"""Spike obs-stack round 9 (never shipped): SRV-OBS phase 2 "MLflow operations" (owner, 2026-11-24: "all MLflow
configuration must be done properly so it is a production-ready solution"). Zero credit; synthetic ids and numbers.

MODE=conc   concurrent writes as the worker makes them: W writer threads (the worker's job concurrency) doing
            runs/create + log-batch + update and HUMAN/CODE assessments, while OTLP spans stream in; counts the
            non-2xx answers and the latency percentiles. Run once per SQLite journal mode.
MODE=ops    the production flags: the disabled features answer, the health probes (MLflow /health, Rotel's OTLP
            receiver, Jaeger's healthcheckv2), the journal mode, an online backup (sqlite3 backup API) restored and
            served by a second MLflow, VACUUM after deleting traces.
MODE=load   a whole-book compile (LESSONS lessons × 8 jobs × 72 spans), 100 tutor questions (10 spans each) and a
            bench upload (40 records, 3 Runs): spans sent vs stored, ingest latency p95, the database's growth.
Writes /out/round9-<MODE>.json.
"""

import concurrent.futures as cf
import json
import os
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/spike")
from opentelemetry import context as otel_context  # noqa: E402
from opentelemetry import trace  # noqa: E402
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: E402
from opentelemetry.sdk.resources import Resource  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: E402
from opentelemetry.trace import SpanKind  # noqa: E402

MLFLOW = os.environ.get("MLFLOW", "http://mlflow:5000")
DB = "/mlflowdata/mlflow.db"
MODE = os.environ.get("MODE", "ops")
OUT: dict = {"mode": MODE}


def rest(method: str, path: str, body: dict | None = None, base: str = MLFLOW, timeout: float = 30) -> tuple[int, dict, float]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    t = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw[:1] in b"{[" else {}), time.monotonic() - t
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:200]}, time.monotonic() - t
    except Exception as e:  # noqa: BLE001
        return 0, {"error": f"{type(e).__name__}: {str(e)[:150]}"}, time.monotonic() - t


def wait(base: str = MLFLOW, limit: float = 180) -> float:
    t = time.time()
    while time.time() - t < limit:
        if rest("GET", "/health", base=base, timeout=2)[0] == 200:
            return time.time() - t
        time.sleep(1)
    return -1


def provider(queue: int = 50000) -> tuple[TracerProvider, BatchSpanProcessor]:
    p = TracerProvider(resource=Resource({"service.name": "reader-worker", "service.version": "0.1.0"}))
    bsp = BatchSpanProcessor(OTLPSpanExporter(endpoint="http://rotel:4318/v1/traces"), max_queue_size=queue,
                             max_export_batch_size=512, schedule_delay_millis=1000)
    p.add_span_processor(bsp)
    return p, bsp


def job_trace(tracer: trace.Tracer, kind: str, spans: int, tag: str) -> str:
    root = tracer.start_span(f"job {kind}", context=otel_context.Context(), kind=SpanKind.CONSUMER, attributes={
        "reader.job.kind": kind, "mlflow.spanType": "CHAIN", "mlflow.traceTag.reader.load": tag,
        "mlflow.traceTag.mlflow.traceName": f"job {kind}"})
    with trace.use_span(root):
        for i in range(spans - 1):
            tracer.start_span("claude_code.llm_request", attributes={
                "input_tokens": 50, "output_tokens": 400, "cache_read_tokens": 30000, "duration_ms": 900 + i,
                "model": "claude-sonnet-5-5", "span.type": "claude_code.llm_request"}).end()
    root.end()
    return format(root.get_span_context().trace_id, "032x")


def pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, max(0, int(-(-q * len(xs) // 1)) - 1))], 4)


def db_counts(path: str = DB) -> dict:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        out = {}
        for table in ("experiments", "runs", "trace_info", "spans", "assessments", "evaluation_datasets",
                      "logged_models"):
            try:
                out[table] = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                out[table] = None
        out["journal_mode"] = con.execute("PRAGMA journal_mode").fetchone()[0]
        return out
    finally:
        con.close()


def size(path: str = DB) -> int:
    return sum(os.path.getsize(p) for p in (path, path + "-wal", path + "-shm") if os.path.exists(p))


# --- MODE=conc ---

def conc() -> None:
    writers = int(os.environ.get("WRITERS", "8"))
    iters = int(os.environ.get("ITERS", "40"))
    p, _ = provider()
    tracer = p.get_tracer("reader")
    seeds = [job_trace(tracer, "pipeline.stage", 5, "conc-seed") for _ in range(writers)]
    p.force_flush()
    time.sleep(6)
    lat: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()

    def note(status: int, body: dict, dt: float) -> None:
        with lock:
            lat.append(dt)
            if not 200 <= status < 300:
                errors.append(f"{status} {str(body.get('error', ''))[:100]}")

    def writer(n: int) -> None:
        now = int(time.time() * 1000)
        for i in range(iters):
            s, b, dt = rest("POST", "/api/2.0/mlflow/runs/create", {"experiment_id": "0", "run_name": f"w{n}-{i}",
                                                                    "start_time": now})
            note(s, b, dt)
            rid = (b.get("run") or {}).get("info", {}).get("run_id")
            if rid:
                note(*rest("POST", "/api/2.0/mlflow/runs/log-batch", {"run_id": rid, "params": [], "tags": [],
                     "metrics": [{"key": f"m{k}", "value": k, "timestamp": now, "step": 0} for k in range(10)]}))
                note(*rest("POST", "/api/2.0/mlflow/runs/update", {"run_id": rid, "status": "FINISHED",
                                                                   "end_time": now + 1}))
            tid = seeds[n]
            note(*rest("POST", f"/ajax-api/3.0/mlflow/traces/tr-{tid}/assessments", {"assessment": {
                "assessment_name": f"conc.{i}", "trace_id": f"tr-{tid}", "source": {"source_type": "CODE",
                "source_id": "reader.spike"}, "feedback": {"value": True}}}))

    def spans() -> None:
        for _ in range(40):
            job_trace(tracer, "tutor.answer", 25, "conc")
            time.sleep(0.05)
        p.force_flush()

    t = time.time()
    with cf.ThreadPoolExecutor(writers + 1) as ex:
        futs = [ex.submit(writer, n) for n in range(writers)] + [ex.submit(spans)]
        for f in futs:
            f.result()
    OUT.update({"writers": writers, "requests": len(lat), "errors": len(errors), "error_samples": errors[:5],
                "p50_s": pct(lat, 0.5), "p95_s": pct(lat, 0.95), "max_s": round(max(lat), 3), "wall_s": round(time.time() - t, 1),
                "db": db_counts()})


# --- MODE=ops ---

def ops() -> None:
    OUT["health_s"] = wait()
    # Disabled features: what the server answers (the edge blocks these paths too; FR-OBS-9).
    probes = {
        "gateway_budgets": ("GET", "/api/3.0/mlflow/gateway/budgets/list"),
        "gateway_endpoints": ("GET", "/api/2.0/mlflow/gateway/endpoints/list"),
        "assistant": ("POST", "/ajax-api/3.0/mlflow/assistant/sessions"),
        "jobs": ("GET", "/ajax-api/3.0/jobs/search"),
        "mcp": ("POST", "/mcp"),
        "graphql": ("POST", "/graphql"),
        "cors_preflight": ("OPTIONS", "/api/2.0/mlflow/experiments/search"),
    }
    OUT["disabled"] = {k: rest(m, path, {} if m == "POST" else None)[0] for k, (m, path) in probes.items()}
    # CORS: a cross-origin request gets no Access-Control-Allow-Origin.
    req = urllib.request.Request(MLFLOW + "/api/2.0/mlflow/experiments/search?max_results=1",
                                 headers={"Origin": "https://evil.example"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            OUT["cors_header"] = r.headers.get("Access-Control-Allow-Origin")
            OUT["cors_status"] = r.status
    except urllib.error.HTTPError as e:
        OUT["cors_status"], OUT["cors_header"] = e.code, e.headers.get("Access-Control-Allow-Origin")
    # A Host header outside --allowed-hosts.
    req = urllib.request.Request(MLFLOW + "/health", headers={"Host": "evil.example"})
    try:
        OUT["bad_host_status"] = urllib.request.urlopen(req, timeout=5).status
    except urllib.error.HTTPError as e:
        OUT["bad_host_status"] = e.code
    # Health probes for the other two (a sidecar: Jaeger is distroless, Rotel has no health endpoint).
    OUT["rotel_probe"] = rest("POST", "/v1/traces", {}, base="http://rotel:4318")[0]
    OUT["jaeger_health"] = rest("GET", "/status", base="http://jaeger:13133")[0]
    OUT["jaeger_health_root"] = rest("GET", "/", base="http://jaeger:13133")[0]
    # Some data, then an online backup while the server runs.
    p, _ = provider()
    tracer = p.get_tracer("reader")
    tids = [job_trace(tracer, "pipeline.stage", 20, "ops") for _ in range(50)]
    p.force_flush()
    time.sleep(8)
    rest("POST", "/api/2.0/mlflow/experiments/create", {"name": "bench"})
    for i in range(5):
        rest("POST", "/api/2.0/mlflow/runs/create", {"experiment_id": "0", "run_name": f"ops-{i}"})
    before = db_counts()
    OUT["live_counts"] = before
    t = time.time()
    src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect("/tmp/backup.db")
    src.backup(dst)
    dst.close()
    src.close()
    OUT["backup_s"] = round(time.time() - t, 2)
    OUT["backup_bytes"] = os.path.getsize("/tmp/backup.db")
    OUT["backup_counts"] = db_counts("/tmp/backup.db")
    # Restore: a second MLflow served from the backup file (the driver has the full mlflow package).
    srv = subprocess.Popen(["mlflow", "server", "--host", "127.0.0.1", "--port", "5055", "--workers", "1",
                            "--backend-store-uri", "sqlite:////tmp/backup.db", "--artifacts-destination", "/tmp/art"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env={**os.environ, "MLFLOW_SERVER_ENABLE_JOB_EXECUTION": "false",
                                "MLFLOW_DISABLE_TELEMETRY": "true", "DO_NOT_TRACK": "true"})
    try:
        OUT["restore_health_s"] = wait("http://127.0.0.1:5055", 120)
        s, b, _ = rest("POST", "/api/2.0/mlflow/runs/search", {"experiment_ids": ["0"], "max_results": 100},
                       base="http://127.0.0.1:5055")
        OUT["restore_runs"] = len(b.get("runs", []))
        s, b, _ = rest("POST", "/ajax-api/3.0/mlflow/traces/search", {"locations": [{"type": "MLFLOW_EXPERIMENT",
                       "mlflow_experiment": {"experiment_id": "0"}}], "max_results": 500}, base="http://127.0.0.1:5055")
        OUT["restore_traces"] = len(b.get("traces", []))
    finally:
        srv.terminate()
        srv.wait(20)
    # Delete half the traces (retention), then measure the file; VACUUM runs with the server stopped (run-round9.sh).
    rest("POST", "/api/3.0/mlflow/traces/delete-traces", {"experiment_id": "0", "request_ids": [f"tr-{t}" for t in tids[:25]]})
    OUT["size_after_delete"] = size()


def vacuum() -> None:
    con = sqlite3.connect(DB, timeout=60)
    before = size()
    con.execute("VACUUM")
    con.close()
    OUT.update({"size_before_vacuum": before, "size_after_vacuum": size()})


def wal() -> None:
    con = sqlite3.connect(DB, timeout=60)
    OUT["journal_mode"] = con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    con.close()


# --- MODE=load ---

def load() -> None:
    lessons = int(os.environ.get("LESSONS", "60"))
    wait()
    size0 = size()
    p, bsp = provider()
    tracer = p.get_tracer("reader")
    sent = 0
    sample: list[tuple[str, float]] = []
    t = time.time()
    for lesson in range(lessons):
        for j in range(8):
            tid = job_trace(tracer, "pipeline.stage", 72, "load")
            sent += 72
            if j == 0 and lesson % 3 == 0:
                sample.append((tid, time.time()))
        time.sleep(0.2)  # a compile's jobs arrive over minutes; this compresses a whole book to a few minutes
    for q in range(100):
        tid = job_trace(tracer, "tutor.answer", 10, "load")
        sent += 10
        if q % 5 == 0:
            sample.append((tid, time.time()))
    p.force_flush()
    send_s = time.time() - t
    # Bench upload: a dataset of 40 records and 3 Runs in `bench`.
    s, b, _ = rest("POST", "/api/2.0/mlflow/experiments/create", {"name": "bench"})
    bench = b.get("experiment_id", "1")
    s, b, _ = rest("POST", "/api/3.0/mlflow/datasets/create", {"name": "tutor-bench-v2", "experiment_ids": [bench]})
    ds = (b.get("dataset") or {}).get("dataset_id")
    recs = [{"inputs": {"question_id": f"v{i:02d}", "chapter": 1 + i % 23}, "expectations": {"gold_pages": [i]}}
            for i in range(40)]
    rest("POST", f"/api/3.0/mlflow/datasets/{ds}/records", {"records": json.dumps(recs)})
    for i in range(3):
        rest("POST", "/api/2.0/mlflow/runs/create", {"experiment_id": bench, "run_name": f"bench {i}"})
    # Ingest latency: from a sampled trace's end until MLflow returns it.
    lat = []
    for tid, ended in sample:
        while time.time() - ended < 300:
            s, b, _ = rest("GET", f"/ajax-api/3.0/mlflow/traces/get?trace_id=tr-{tid}")
            if s == 200 and (b.get("trace") or {}).get("spans"):
                lat.append(time.time() - ended)
                break
            time.sleep(0.5)
    # Spans stored: wait until the count stops growing.
    last, stable = -1, 0
    while stable < 3:
        n = db_counts()["spans"] or 0
        stable = stable + 1 if n == last else 0
        last = n
        time.sleep(5)
    OUT.update({"lessons": lessons, "spans_sent": sent, "spans_stored": last, "dropped": sent - last,
                "send_s": round(send_s, 1), "ingest_p50_s": pct(lat, 0.5), "ingest_p95_s": pct(lat, 0.95),
                "ingest_max_s": round(max(lat), 2) if lat else None, "sampled": len(sample), "found": len(lat),
                "db_bytes_before": size0, "db_bytes_after": size(), "db": db_counts()})


if __name__ == "__main__":
    {"conc": conc, "ops": ops, "load": load, "vacuum": vacuum, "wal": wal,
     "wait": lambda: OUT.update({"health_s": wait()})}[MODE]()
    json.dump(OUT, open(f"/out/round9-{MODE}{os.environ.get('SUFFIX', '')}.json", "w"), indent=1, default=str)
    print(json.dumps(OUT)[:2000])
