"""Spike obs-stack round 6 (never shipped): SRV-OBS phase 2 — "use all advantages of MLflow to manage everything here as a
dashboard" (owner, 2026-11-24). Measures, against the spike MLflow (bundled catalog replaced, no egress, zero credit):

  runs      a "compile" experiment with one Run per compile run (params: course, lesson, stage models, pipeline
            version; metrics: ledger cost by stage, tokens, durations, verify and fact-check pass counts, pages) and its
            stage traces linked (POST /mlflow/traces/link-to-run); an "ingest" experiment with one Run per import
            (pages/min, chunks, converter version) and its trace linked. Synthetic ids and numbers only.
  datasets  the repository's committed bench sets as MLflow GenAI evaluation datasets: questions-v2 (+ t01 from
            bench-fixed) with gold pages as expectations; questions.yaml (v1). The question text is committed repo data,
            the agent's own wording (each file's header says nothing is quoted from the book).
  evals     Evaluation runs from committed bench results with code scorers only: the search bench
            (spikes/embeddings-local/results/*-v2/*.json per-question ranks: hit@1/3/10, reciprocal rank) through
            mlflow.genai.evaluate over precomputed outputs (no predict_fn, no model call), and the tutor bench aggregate
            (e2e/live/results/2026-11-23-tutor-bench.json) as a Run with metrics. A second evaluate run = a new Run.
  versions  agent versions as LoggedModels (prompt set sha, models, pipeline config as params/tags) and how traces can
            point at one (OTLP root attribute vs REST).
  labels    a HUMAN assessment (bool + ids-only metadata) on a trace, as the app would send it.
  budgets   whether MLflow 3.18 has budget or alert features outside the AI gateway.
Writes /out/phase2.json and /out/phase2-ids.json.
"""

import glob
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

import yaml

sys.path.insert(0, "/spike")
import analytics as a  # noqa: E402  (OTLP helpers and the compile-like trace)

MLFLOW = "http://mlflow:5000"
OUT: dict = {}
IDS: dict = {}
REPO = "/repo"


def call(path: str, body: dict | None = None, method: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(MLFLOW + path, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:400]}


def wait() -> None:
    for _ in range(120):
        try:
            if urllib.request.urlopen(MLFLOW + "/health", timeout=2).status == 200:
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)


def runs() -> None:
    import mlflow
    from mlflow import MlflowClient

    mlflow.set_tracking_uri(MLFLOW)
    c = MlflowClient()
    exp = c.create_experiment("compile", tags={"reader.kind": "compile"})
    IDS["compile_experiment"] = exp
    lesson, course, run_id = "20000000-0000-4000-8000-0000000000bb", "50000000-0000-4000-8000-0000000000cc", \
        "10000000-0000-4000-8000-0000000000aa"
    stage_traces = {st: a.job(f"stage_{st}", "pipeline.stage", "proposed", offset_s=300 - i * 30, ms=40000,
                              usage=(50, 4000, 30000, 8000), ledger_usd=usd, stage=st)
                    for i, (st, usd) in enumerate([("draft", 0.62), ("fact_check", 0.21), ("adversarial_review", 0.30),
                                                   ("revise", 0.71)])}
    a.provider.force_flush()
    a.cli_provider.force_flush()
    time.sleep(6)
    # (a) a Run in a separate "compile" experiment, traces stay in experiment 0: is a cross-experiment link allowed?
    rx = c.create_run(exp, run_name=f"compile {run_id[:8]}", tags={"reader.run_id": run_id})
    OUT["link_cross_experiment"] = call("/api/2.0/mlflow/traces/link-to-run",
                                        {"trace_ids": [f"tr-{t}" for t in stage_traces.values()],
                                         "run_id": rx.info.run_id})
    c.set_terminated(rx.info.run_id, "FINISHED")
    IDS["compile_run_cross"] = rx.info.run_id
    # (b) the Run in experiment 0, where the traces are (the Evaluation runs / Runs views are per experiment).
    r = c.create_run("0", run_name=f"compile {run_id[:8]}", tags={
        "reader.kind": "compile", "reader.run_id": run_id, "reader.lesson.id": lesson, "reader.course.id": course,
        "reader.outcome": "finished"})
    rid = r.info.run_id
    IDS["compile_run"] = rid
    params = {"lesson_id": lesson, "course_id": course, "pipeline_version": "v1", "prompt_set_sha256": "ab" * 32,
              "model.draft": "claude-sonnet-5-5", "model.fact_check": "claude-sonnet-5-5",
              "model.adversarial_review": "claude-opus-5-5", "model.revise": "claude-sonnet-5-5", "reader_version": "0.1.0"}
    for k, v in params.items():
        c.log_param(rid, k, v)
    metrics = {"cost_usd.draft": 0.62, "cost_usd.fact_check": 0.21, "cost_usd.adversarial_review": 0.30,
               "cost_usd.revise": 0.71, "cost_usd.total": 1.84, "tokens.input": 152200, "tokens.output": 16000,
               "duration_s.total": 160, "claims.total": 14, "claims.supported": 13, "labs.cells": 12, "labs.cells_ok": 12,
               "pages": 24, "loops": 1}
    for k, v in metrics.items():
        c.log_metric(rid, k, v)
    for step, (k, v) in enumerate([("cost_usd.cumulative", 0.62), ("cost_usd.cumulative", 0.83),
                                   ("cost_usd.cumulative", 1.13), ("cost_usd.cumulative", 1.84)], start=1):
        c.log_metric(rid, k, v, step=step)
    c.set_terminated(rid, "FINISHED")
    link = call("/api/2.0/mlflow/traces/link-to-run",
                {"trace_ids": [f"tr-{t}" for t in stage_traces.values()], "run_id": rid})
    OUT["link_traces_to_run"] = link
    got = call("/ajax-api/3.0/mlflow/traces/search", {
        "locations": [{"type": "MLFLOW_EXPERIMENT", "mlflow_experiment": {"experiment_id": "0"}}],
        "filter": f"metadata.`mlflow.sourceRun` = '{rid}'", "max_results": 50})
    OUT["traces_by_run_metadata"] = {"status": got[0], "count": len(got[1].get("traces", [])), "error": got[1].get("error")}
    # Ingest: one Run per import.
    ing = "0"
    tid = a.job("ingest_import", "ingest.import", "proposed", offset_s=600, ms=120000, usage=(0, 0, 0, 0), ledger_usd=0.0)
    a.provider.force_flush()
    time.sleep(5)
    ri = c.create_run(ing, run_name="import aa11aa11", tags={"reader.kind": "ingest",
                                                             "reader.material_sha256": "aa11" * 16})
    for k, v in {"converter_version": "r2", "material_sha256": "aa11" * 16}.items():
        c.log_param(ri.info.run_id, k, v)
    for k, v in {"pages": 594, "spans": 9463, "chunks": 2285, "pages_per_min_import": 297.0, "import_s": 120,
                 "embed_chunks_per_s": 2.71}.items():
        c.log_metric(ri.info.run_id, k, v)
    c.set_terminated(ri.info.run_id, "FINISHED")
    OUT["link_ingest"] = call("/api/2.0/mlflow/traces/link-to-run", {"trace_ids": [f"tr-{tid}"], "run_id": ri.info.run_id})
    IDS["ingest_run"] = ri.info.run_id
    IDS["stage_traces"] = stage_traces


def _questions() -> list[dict]:
    v2 = yaml.safe_load(open(f"{REPO}/spikes/embeddings-local/questions-v2.yaml"))
    fixed = yaml.safe_load(open(f"{REPO}/spec/fixtures/SRV-TUTOR/bench-fixed.yaml"))
    qs = [q for q in v2["questions"] if q["id"] != "v03"] + fixed["questions"]
    return qs


def datasets() -> None:
    import mlflow
    from mlflow.genai import datasets as gd

    mlflow.set_tracking_uri(MLFLOW)
    exp = "0"
    qs = _questions()
    src = open(f"{REPO}/spikes/embeddings-local/questions-v2.yaml", "rb").read()
    ds = gd.create_dataset(name="tutor-bench-v2", experiment_id=[exp],
                           tags={"source": "spikes/embeddings-local/questions-v2.yaml + spec/fixtures/SRV-TUTOR/bench-fixed.yaml",
                                 "source_sha256": hashlib.sha256(src).hexdigest(), "language": "ru"})
    ds.merge_records([{"inputs": {"question_id": q["id"], "question": q["question"], "chapter": q["chapter"]},
                       "expectations": {"gold_pages": q["gold_pages"]}} for q in qs])
    v1 = yaml.safe_load(open(f"{REPO}/spikes/embeddings-local/questions.yaml"))
    ds1 = gd.create_dataset(name="search-bench-v1", experiment_id=[exp],
                            tags={"source": "spikes/embeddings-local/questions.yaml"})
    ds1.merge_records([{"inputs": {"question_id": q["id"], "question": q["question"]},
                        "expectations": {"gold_pages": q["gold_pages"]}} for q in v1["questions"]])
    OUT["datasets"] = {"tutor-bench-v2": {"id": ds.dataset_id, "records": len(qs)},
                       "search-bench-v1": {"id": ds1.dataset_id, "records": len(v1["questions"])}}
    IDS["dataset"] = ds.dataset_id


def evals() -> None:
    import mlflow
    from mlflow.genai.scorers import scorer

    mlflow.set_tracking_uri(MLFLOW)
    mlflow.set_experiment(experiment_id="0")
    qs = {q["id"]: q for q in _questions()}

    @scorer
    def hit_at_10(outputs: dict, expectations: dict) -> bool:
        return bool(set(outputs["top_pages"][:10]) & set(expectations["gold_pages"]))

    @scorer
    def hit_at_3(outputs: dict, expectations: dict) -> bool:
        return bool(set(outputs["top_pages"][:3]) & set(expectations["gold_pages"]))

    @scorer
    def reciprocal_rank(outputs: dict) -> float:
        r = outputs.get("rank")
        return 1.0 / r if r else 0.0

    results = []
    for path in sorted(glob.glob(f"{REPO}/spikes/embeddings-local/results/20261120T232835-v2/*.json")):
        res = json.load(open(path))
        data = [{"inputs": {"question_id": p["id"], "question": qs[p["id"]]["question"]} if p["id"] in qs
                 else {"question_id": p["id"]},
                 "outputs": {"top_pages": p["top_pages"], "rank": p.get("rank")},
                 "expectations": {"gold_pages": qs.get(p["id"], {}).get("gold_pages", [])}}
                for p in res["per_question"] if p["id"] in qs]
        with mlflow.start_run(run_name=f"search bench {res['model']}") as run:
            mlflow.log_params({"model": res["model"], "chunk_chars": res["chunk_chars"], "overlap": res["overlap"],
                               "questions_file": "spikes/embeddings-local/questions-v2.yaml"})
            mlflow.log_metrics({"embed_s": res["embed_s"], "chunks_per_s": res["chunks_per_s"]})
            ev = mlflow.genai.evaluate(data=data, scorers=[hit_at_3, hit_at_10, reciprocal_rank])
            results.append({"model": res["model"], "run_id": run.info.run_id, "metrics": dict(ev.metrics)})
    tb = json.load(open(f"{REPO}/e2e/live/results/2026-11-23-tutor-bench.json"))
    with mlflow.start_run(run_name="tutor bench 2026-11-23") as run:
        mlflow.log_params({"commit": tb["commit"][:40], "question_set": "questions-v2 v03->t01", "n": tb["n"]})
        mlflow.log_metrics({"grounded_share": tb["grounded_share"], "gold_cited_share": tb["gold_cited_share"],
                            "latency_p50_s": tb["latency_s"]["p50"], "latency_p95_s": tb["latency_s"]["p95"],
                            "mean_usd": tb["mean_usd"], "total_usd": tb["total_usd"]})
        mlflow.set_tags({"reader.bench": "tutor", "mlflow.runType": "evaluation"})
        results.append({"tutor_bench_run": run.info.run_id})
    OUT["evals"] = results


def versions() -> None:
    import mlflow

    mlflow.set_tracking_uri(MLFLOW)
    mlflow.set_experiment(experiment_id="0")
    out = {}
    for name, prompt_sha in (("reader-agent", "ab" * 32), ("reader-agent", "cd" * 32)):
        m = mlflow.create_external_model(name=name, params={"prompt_set_sha256": prompt_sha,
                                                            "model.tutor": "claude-sonnet-5-5", "pipeline_version": "v1"},
                                         tags={"reader.version": "0.1.0", "reader.prompt_set_sha256": prompt_sha})
        out[prompt_sha[:4]] = m.model_id
    IDS["models"] = out
    mid = out["abab"]
    # (1) OTLP: root attributes carrying the model id, as a trace tag and as a bare attribute.
    from opentelemetry import context as otel_context
    from opentelemetry.trace import SpanKind
    span = a.ours.start_span("job tutor.answer", context=otel_context.Context(), kind=SpanKind.CONSUMER, attributes={
        "reader.job.kind": "tutor.answer", "mlflow.spanType": "CHAIN",
        "mlflow.traceTag.mlflow.traceName": "job tutor.answer", "mlflow.traceTag.mlflow.modelId": mid,
        "mlflow.traceTag.reader.version": "abab", "mlflow.modelId": mid,
        "mlflow.llm.cost": json.dumps({"input_cost": 0.02, "output_cost": 0.01, "total_cost": 0.03})})
    span.end()
    tid = format(span.get_span_context().trace_id, "032x")
    a.provider.force_flush()
    time.sleep(5)
    s1 = call("/ajax-api/3.0/mlflow/traces/search", {
        "locations": [{"type": "MLFLOW_EXPERIMENT", "mlflow_experiment": {"experiment_id": "0"}}],
        "filter": f"metadata.`mlflow.modelId` = '{mid}'", "max_results": 10})
    s2 = call("/ajax-api/3.0/mlflow/traces/search", {
        "locations": [{"type": "MLFLOW_EXPERIMENT", "mlflow_experiment": {"experiment_id": "0"}}],
        "filter": f"tag.`mlflow.modelId` = '{mid}'", "max_results": 10})
    # (2) REST: the client's link API, if any.
    linked = None
    try:
        from mlflow import MlflowClient
        fn = getattr(MlflowClient(), "link_traces_to_model", None) or getattr(MlflowClient(), "link_traces_to_logged_model", None)
        linked = "no client API" if fn is None else str(fn(model_id=mid, trace_ids=[f"tr-{tid}"]))
    except Exception as e:  # noqa: BLE001
        linked = f"error: {str(e)[:200]}"
    s3 = call("/ajax-api/3.0/mlflow/traces/search", {
        "locations": [{"type": "MLFLOW_EXPERIMENT", "mlflow_experiment": {"experiment_id": "0"}}],
        "filter": f"metadata.`mlflow.modelId` = '{mid}'", "max_results": 10})
    OUT["versions"] = {"models": out, "by_metadata_otlp": len(s1[1].get("traces", [])), "by_tag": len(s2[1].get("traces", [])),
                       "rest_link": linked, "by_metadata_after_link": len(s3[1].get("traces", [])),
                       "search_errors": [s1[1].get("error"), s2[1].get("error")]}
    IDS["versioned_trace"] = tid


def labels() -> None:
    import mlflow
    from mlflow.entities import AssessmentSource

    mlflow.set_tracking_uri(MLFLOW)
    tid = IDS["stage_traces"]["revise"]
    fb = mlflow.log_feedback(trace_id=f"tr-{tid}", name="owner.lesson_ok", value=False,
                             source=AssessmentSource(source_type="HUMAN", source_id="owner"),
                             metadata={"lesson_id": "20000000-0000-4000-8000-0000000000bb", "reason": "wrong_fact"})
    OUT["labels"] = {"assessment_id": fb.assessment_id, "trace": tid}
    s = call("/ajax-api/3.0/mlflow/traces/get?trace_id=tr-" + tid)
    OUT["labels"]["on_trace"] = [x.get("assessment_name") for x in
                                 ((s[1].get("trace") or {}).get("trace_info") or {}).get("assessments", [])]
    # Labeling sessions (Databricks-only in OSS?) and the OSS label schemas + review queues (the Review tab).
    try:
        from mlflow.genai import labeling
        sess = labeling.create_labeling_session(name="owner review", assigned_users=["owner"])
        OUT["labels"]["labeling_session"] = str(sess)[:120]
    except Exception as e:  # noqa: BLE001
        OUT["labels"]["labeling_session_error"] = f"{type(e).__name__}: {str(e)[:240]}"
    try:
        from mlflow.genai import label_schemas as ls
        from mlflow.genai import review_queues as rq
        mlflow.set_experiment(experiment_id="0")
        schema = ls.create_label_schema(name="owner.lesson_ok", type="feedback", input=ls.InputPassFail(positive_label="good", negative_label="bad"),
                                        instruction="Is this lesson good?", enable_comment=False, experiment_id="0")
        sid = getattr(schema, "schema_id", None) or getattr(schema, "label_schema_id", None)
        q = rq.create_review_queue("lessons", queue_type="custom", users=[], schema_ids=[sid], experiment_id="0")
        qid = getattr(q, "queue_id", None)
        items = rq.add_items_to_review_queue(qid, item_ids=[f"tr-{t}" for t in IDS["stage_traces"].values()])
        OUT["labels"]["review_queue"] = {"schema": sid, "queue": qid, "items": len(items)}
        IDS["review_queue"] = qid
    except Exception as e:  # noqa: BLE001
        OUT["labels"]["review_queue_error"] = f"{type(e).__name__}: {str(e)[:300]}"


def budgets() -> None:
    # Budgets exist only as AI-gateway policies (recorded by the gateway's on_complete callbacks); an exceeded window
    # fires a `budget_policy.exceeded` webhook (an outbound HTTP call from MLflow). Measure that the list works and
    # that spend logged outside the gateway (our OTLP traces) never moves a window.
    st = call("/api/3.0/mlflow/gateway/budgets/list", method="GET")
    cr = call("/api/3.0/mlflow/gateway/budgets/create", {"budget_unit": "USD", "budget_amount": 0.01,
                                                         "duration": {"unit": "DAYS", "value": 1},
                                                         "target_scope": "GLOBAL", "budget_action": "ALERT"})
    win = call("/api/3.0/mlflow/gateway/budgets/windows", method="GET")
    OUT["budgets"] = {"list": st, "create": cr, "windows": win}


def main() -> None:
    wait()
    for name, fn in (("runs", runs), ("datasets", datasets), ("evals", evals), ("versions", versions),
                     ("labels", labels), ("budgets", budgets)):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            OUT[f"{name}_error"] = f"{type(e).__name__}: {str(e)[:400]}"
    # How many traces exist now, and how many were written by evaluate (outside the allow-list exporter)?
    allt = call("/ajax-api/3.0/mlflow/traces/search", {
        "locations": [{"type": "MLFLOW_EXPERIMENT", "mlflow_experiment": {"experiment_id": "0"}}], "max_results": 500})
    traces = allt[1].get("traces", [])
    OUT["trace_count"] = len(traces)
    OUT["eval_traces"] = sum(1 for t in traces if (t.get("trace_metadata") or {}).get("mlflow.sourceRun") and
                             any(e.get("run_id") == (t.get("trace_metadata") or {}).get("mlflow.sourceRun")
                                 for e in OUT.get("evals", []) if isinstance(e, dict)))
    json.dump(OUT, open("/out/phase2.json", "w"), indent=1, default=str)
    json.dump(IDS, open("/out/phase2-ids.json", "w"), indent=1, default=str)
    print("done")


if __name__ == "__main__":
    main()
