"""Spike obs-stack round 8 (never shipped): SRV-OBS phase 2 design review round 1, B11. Every write the phase 2
design keeps, replayed as raw REST against the pinned MLflow 3.18.0 (zero credit, synthetic ids and numbers; the
two dataset records are committed questions-v2 records). For each request: method, path, status and the body sent
(recorded in /out/round8.json). Then the client's own requests for the two shapes the raw section copies from it
(log-inputs of an evaluation dataset, an assessment override), recorded by wrapping mlflow.utils.rest_utils.
Also measured: a dataset in the `bench` experiment, link-to-run with more than 100 traces, and how a Run renders
after some of its traces are deleted (round8-tabs.js screenshots). Writes /out/round8.json, /out/round8-ids.json.
"""

import json
import sys
import time
import urllib.error
import urllib.request

import yaml

sys.path.insert(0, "/spike")
import analytics as a  # noqa: E402
from opentelemetry import context as otel_context  # noqa: E402
from opentelemetry.trace import SpanKind  # noqa: E402

MLFLOW = "http://mlflow:5000"
LOG: list[dict] = []
OUT: dict = {"raw": LOG}
IDS: dict = {}


def rest(method: str, path: str, body: dict | None = None, params: str = "") -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(MLFLOW + path + params, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status, got = r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        status, got = e.code, {"error": e.read().decode()[:300]}
    LOG.append({"method": method, "path": path + params, "status": status, "body": body})
    return status, got


def wait() -> None:
    for _ in range(120):
        try:
            if urllib.request.urlopen(MLFLOW + "/health", timeout=2).status == 200:
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)


def traces(n: int, run_id: str) -> list[str]:
    out = []
    for i in range(n):
        s = a.ours.start_span("job pipeline.stage", context=otel_context.Context(), kind=SpanKind.CONSUMER,
                              attributes={"reader.job.kind": "pipeline.stage", "mlflow.spanType": "CHAIN",
                                          "mlflow.traceTag.reader.run_id": run_id, "reader.job.id": 1000 + i})
        s.end()
        out.append(format(s.get_span_context().trace_id, "032x"))
    a.provider.force_flush()
    return out


def main() -> None:
    wait()
    now = int(time.time() * 1000)
    run_key = "10000000-0000-4000-8000-0000000000aa"
    # --- experiments: get-by-name, then create (the `bench` experiment) ---
    s, got = rest("GET", "/api/2.0/mlflow/experiments/get-by-name", params="?experiment_name=bench")
    OUT["bench_missing_status"] = s
    s, got = rest("POST", "/api/2.0/mlflow/experiments/create", {"name": "bench",
                                                                 "tags": [{"key": "reader.kind", "value": "bench"}]})
    bench = got.get("experiment_id")
    IDS["bench"] = bench
    # --- a compile Run in experiment 0: search first (idempotency), create, log-batch, update, complete tag last ---
    flt = f"tags.`reader.run_id` = '{run_key}'"
    s, got = rest("POST", "/api/2.0/mlflow/runs/search", {"experiment_ids": ["0"], "filter": flt, "max_results": 2})
    OUT["search_before"] = len(got.get("runs", []))
    s, got = rest("POST", "/api/2.0/mlflow/runs/create", {
        "experiment_id": "0", "run_name": "compile 10000000", "start_time": now,
        "tags": [{"key": "reader.kind", "value": "compile"}, {"key": "reader.run_id", "value": run_key}]})
    rid = got["run"]["info"]["run_id"]
    IDS["run"] = rid
    rest("POST", "/api/2.0/mlflow/runs/log-batch", {
        "run_id": rid,
        "params": [{"key": "version_key", "value": "abababababab"}, {"key": "snapshot_at", "value": "2026-11-24T12:00:00Z"},
                   {"key": "model.draft", "value": "claude-sonnet-5-5"}],
        "metrics": [{"key": "cost_usd.total", "value": 1.84, "timestamp": now, "step": 0},
                    {"key": "cost_usd.draft", "value": 0.62, "timestamp": now, "step": 0},
                    {"key": "claims.supported", "value": 13, "timestamp": now, "step": 0}],
        "tags": [{"key": "reader.lesson.id", "value": "20000000-0000-4000-8000-0000000000bb"}]})
    rest("POST", "/api/2.0/mlflow/runs/update", {"run_id": rid, "status": "FINISHED", "end_time": now + 160000})
    rest("POST", "/api/2.0/mlflow/runs/set-tag", {"run_id": rid, "key": "reader.complete", "value": "true"})
    # A second end of the same compile run: the Run is updated in place (metrics re-logged at a new step).
    rest("POST", "/api/2.0/mlflow/runs/log-batch", {
        "run_id": rid, "metrics": [{"key": "cost_usd.total", "value": 2.10, "timestamp": now + 1, "step": 1}],
        "params": [], "tags": [{"key": "reader.ends", "value": "2"}]})
    s, got = rest("POST", "/api/2.0/mlflow/runs/search", {"experiment_ids": ["0"], "filter": flt, "max_results": 2})
    OUT["search_after"] = len(got.get("runs", []))
    s, got = rest("GET", "/api/2.0/mlflow/runs/get", params=f"?run_id={rid}")
    OUT["run_after"] = {"status": got["run"]["info"]["status"],
                        "metrics": {m["key"]: m["value"] for m in got["run"]["data"].get("metrics", [])},
                        "params": {p["key"]: p["value"] for p in got["run"]["data"].get("params", [])},
                        "tags": sorted(t["key"] for t in got["run"]["data"].get("tags", []))}
    # A duplicate param value is refused by MLflow (params are write-once): measured, so re-logs use metrics/tags.
    s, got = rest("POST", "/api/2.0/mlflow/runs/log-batch", {
        "run_id": rid, "params": [{"key": "snapshot_at", "value": "2026-11-24T13:00:00Z"}], "metrics": [], "tags": []})
    OUT["param_rewrite_status"] = s
    OUT["param_rewrite_error"] = got.get("error", "")[:160]
    # --- link-to-run: 120 traces in one call, then 100 + 20 ---
    tids = traces(120, run_key)
    time.sleep(8)
    s, got = rest("POST", "/api/2.0/mlflow/traces/link-to-run", {"trace_ids": [f"tr-{t}" for t in tids], "run_id": rid})
    OUT["link_120"] = {"status": s, "error": got.get("error", "")[:200]}
    LOG[-1]["body"] = {"trace_ids": f"<{len(tids)} ids>", "run_id": rid}
    for chunk in (tids[:100], tids[100:]):
        s, _ = rest("POST", "/api/2.0/mlflow/traces/link-to-run", {"trace_ids": [f"tr-{t}" for t in chunk], "run_id": rid})
        LOG[-1]["body"] = {"trace_ids": f"<{len(chunk)} ids>", "run_id": rid}
    s, got = rest("POST", "/ajax-api/3.0/mlflow/traces/search", {
        "locations": [{"type": "MLFLOW_EXPERIMENT", "mlflow_experiment": {"experiment_id": "0"}}],
        "filter": f"metadata.`mlflow.sourceRun` = '{rid}'", "max_results": 500})
    OUT["linked"] = len(got.get("traces", []))
    # --- delete 5 of the run's traces, then the Run still renders (screenshot) ---
    s, got = rest("POST", "/api/3.0/mlflow/traces/delete-traces", {
        "experiment_id": "0", "request_ids": [f"tr-{t}" for t in tids[:5]]})
    OUT["deleted"] = {"status": s, "count": got.get("traces_deleted")}
    s, got = rest("POST", "/ajax-api/3.0/mlflow/traces/search", {
        "locations": [{"type": "MLFLOW_EXPERIMENT", "mlflow_experiment": {"experiment_id": "0"}}],
        "filter": f"metadata.`mlflow.sourceRun` = '{rid}'", "max_results": 500})
    OUT["linked_after_delete"] = len(got.get("traces", []))
    IDS["trace"] = tids[50]
    # --- a dataset in `bench`: create, records (two committed questions-v2 records), get ---
    v2 = yaml.safe_load(open("/repo/spikes/embeddings-local/questions-v2.yaml"))["questions"][:2]
    s, got = rest("POST", "/api/3.0/mlflow/datasets/create", {
        "name": "tutor-bench-v2", "experiment_ids": [bench],
        "tags": json.dumps({"reader.source_sha256": "ab" * 32, "reader.bench": "tutor"})})
    ds = (got.get("dataset") or {}).get("dataset_id")
    IDS["dataset"] = ds
    records = [{"inputs": {"question_id": q["id"], "question": q["question"], "chapter": q["chapter"]},
                "expectations": {"gold_pages": q["gold_pages"]}} for q in v2]
    s, got = rest("POST", f"/api/3.0/mlflow/datasets/{ds}/records", {"records": json.dumps(records)})
    OUT["records"] = got
    s, got = rest("POST", f"/api/3.0/mlflow/datasets/{ds}/records", {"records": json.dumps(records)})
    OUT["records_again"] = got  # upsert: a repeat updates, never duplicates
    s, got = rest("GET", f"/api/3.0/mlflow/datasets/{ds}")
    OUT["dataset"] = {k: (got.get("dataset") or {}).get(k) for k in ("name", "digest", "experiment_ids", "tags")}
    # --- a bench Run in `bench` with the bench_upload tag; its dataset input (raw, the client's shape below) ---
    s, got = rest("POST", "/api/2.0/mlflow/runs/create", {
        "experiment_id": bench, "run_name": "tutor bench 2026-11-23", "start_time": now,
        "tags": [{"key": "reader.bench", "value": "tutor"}, {"key": "reader.bench_upload", "value": "u" * 16}]})
    brid = got["run"]["info"]["run_id"]
    IDS["bench_run"] = brid
    rest("POST", "/api/2.0/mlflow/runs/log-batch", {"run_id": brid, "params": [
        {"key": "commit", "value": "66fba58"}, {"key": "set_sha256", "value": "ab" * 32}], "metrics": [
        {"key": "grounded_share", "value": 0.625, "timestamp": now, "step": 0},
        {"key": "gold_cited_share", "value": 0.575, "timestamp": now, "step": 0}], "tags": []})
    # --- the client's shapes (recorded): log_inputs of the evaluation dataset, override of an assessment ---
    import mlflow
    import mlflow.utils.rest_utils as ru
    from mlflow import MlflowClient
    from mlflow.entities import AssessmentSource, DatasetInput, InputTag

    real = ru.http_request

    def recording(host_creds, endpoint, method, *args, **kw):  # noqa: ANN001, ANN002, ANN003
        resp = real(host_creds, endpoint, method, *args, **kw)
        body = kw.get("json")
        LOG.append({"method": method, "path": endpoint, "status": resp.status_code, "body": body, "client": True})
        return resp

    ru.http_request = recording
    mlflow.set_tracking_uri(MLFLOW)
    c = MlflowClient()
    try:
        from mlflow.genai.datasets import get_dataset
        entity = get_dataset(dataset_id=ds)._to_mlflow_entity()
        c.log_inputs(brid, datasets=[DatasetInput(dataset=entity, tags=[InputTag("mlflow.data.context", "evaluation")])])
        OUT["log_inputs"] = "ok"
    except Exception as e:  # noqa: BLE001
        OUT["log_inputs_error"] = f"{type(e).__name__}: {str(e)[:300]}"
    c.set_terminated(brid, "FINISHED")
    # A logged model (agent version): create with params and tags, then search by tag.
    try:
        m = c.create_logged_model(experiment_id="0", name="reader-agent", tags={"reader.version_key": "abababababab"},
                                  params={"prompt_set_sha256": "ab" * 32, "pipeline_config_sha256": "cd" * 32,
                                          "stage_models": "fact_check=claude-sonnet-5-5"})
        IDS["model"] = m.model_id
        found = c.search_logged_models(experiment_ids=["0"], filter_string="tags.`reader.version_key` = 'abababababab'")
        OUT["logged_model_search"] = len(found)
    except Exception as e:  # noqa: BLE001
        OUT["logged_model_error"] = f"{type(e).__name__}: {str(e)[:300]}"
    # HUMAN assessment: create, then a relabel as an override (a new revision; the old one stays, marked invalid).
    try:
        fb = mlflow.log_feedback(trace_id=f"tr-{tids[50]}", name="owner.lesson_ok", value=True,
                                 source=AssessmentSource(source_type="HUMAN", source_id="owner"),
                                 metadata={"lesson_id": "20000000-0000-4000-8000-0000000000bb"})
        new = mlflow.override_feedback(trace_id=f"tr-{tids[50]}", assessment_id=fb.assessment_id, value=False,
                                       source=AssessmentSource(source_type="HUMAN", source_id="owner"),
                                       metadata={"lesson_id": "20000000-0000-4000-8000-0000000000bb",
                                                 "reason": "wrong_fact"})
        OUT["override"] = {"first": fb.assessment_id, "second": new.assessment_id}
        upd = mlflow.update_assessment(trace_id=f"tr-{tids[50]}", assessment_id=new.assessment_id,
                                       assessment=mlflow.entities.Feedback(name="owner.lesson_ok", value=True))
        OUT["update"] = getattr(upd, "assessment_id", None)
    except Exception as e:  # noqa: BLE001
        OUT["assessment_error"] = f"{type(e).__name__}: {str(e)[:300]}"
    ru.http_request = real
    s, got = rest("GET", "/ajax-api/3.0/mlflow/traces/get", params=f"?trace_id=tr-{tids[50]}")
    OUT["assessments_on_trace"] = [
        {k: x.get(k) for k in ("assessment_id", "assessment_name", "valid", "overrides", "feedback")}
        for x in ((got.get("trace") or {}).get("trace_info") or {}).get("assessments", [])]
    json.dump(OUT, open("/out/round8.json", "w"), indent=1, default=str)
    json.dump(IDS, open("/out/round8-ids.json", "w"), indent=1, default=str)
    print("done")


if __name__ == "__main__":
    main()
