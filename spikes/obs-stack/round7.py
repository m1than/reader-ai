"""Spike obs-stack round 7 (never shipped): where do compile Runs live? Round 6 showed that in experiment 0 (where
OTLP puts every trace) a Run with linked traces is listed under Evaluation runs as `Eval`, beside the bench runs.
This round puts the compile Run in its own experiment `runs` and links experiment 0's stage traces to it, then
measures (API here, UI in round7-tabs.js): does the link succeed, which traces does a run-scoped search return,
and where is the Run listed? Synthetic ids and numbers only; zero credit. Writes /out/round7.json and
/out/round7-ids.json.
"""

import json
import sys
import time

sys.path.insert(0, "/spike")
import analytics as a  # noqa: E402
from phase2 import MLFLOW, call, wait  # noqa: E402

OUT: dict = {}
IDS: dict = {}


def search(exp: str, flt: str) -> dict:
    s, body = call("/ajax-api/3.0/mlflow/traces/search", {
        "locations": [{"type": "MLFLOW_EXPERIMENT", "mlflow_experiment": {"experiment_id": exp}}],
        "filter": flt, "max_results": 100})
    return {"status": s, "count": len(body.get("traces", [])), "error": body.get("error")}


def main() -> None:
    import mlflow
    from mlflow import MlflowClient

    wait()
    mlflow.set_tracking_uri(MLFLOW)
    c = MlflowClient()
    stage_traces = {st: a.job(f"stage_{st}", "pipeline.stage", "proposed", offset_s=300 - i * 30, ms=40000,
                              usage=(50, 4000, 30000, 8000), ledger_usd=usd, stage=st)
                    for i, (st, usd) in enumerate([("draft", 0.62), ("fact_check", 0.21),
                                                   ("adversarial_review", 0.30), ("revise", 0.71)])}
    a.provider.force_flush()
    a.cli_provider.force_flush()
    time.sleep(6)
    exp = c.create_experiment("runs", tags={"reader.kind": "runs"})
    r = c.create_run(exp, run_name="compile 10000000", tags={"reader.kind": "compile", "reader.run_id": a.RUN})
    rid = r.info.run_id
    for k, v in {"cost_usd.total": 1.84, "cost_usd.draft": 0.62, "claims.supported": 13, "pages": 24}.items():
        c.log_metric(rid, k, v)
    c.log_param(rid, "version_key", "abababababab")
    c.set_terminated(rid, "FINISHED")
    OUT["link"] = call("/api/2.0/mlflow/traces/link-to-run",
                       {"trace_ids": [f"tr-{t}" for t in stage_traces.values()], "run_id": rid})
    time.sleep(2)
    OUT["search_exp0_by_run"] = search("0", f"metadata.`mlflow.sourceRun` = '{rid}'")
    OUT["search_runs_exp_by_run"] = search(exp, f"metadata.`mlflow.sourceRun` = '{rid}'")
    OUT["search_runs_exp_all"] = search(exp, "")
    # The client's run-scoped search (what a Run's Traces tab is built on): locations = the run's experiment.
    try:
        got = mlflow.search_traces(run_id=rid, return_type="list")
        OUT["client_search_by_run"] = len(got)
    except Exception as e:  # noqa: BLE001
        OUT["client_search_by_run_error"] = f"{type(e).__name__}: {str(e)[:300]}"
    try:
        got = mlflow.search_traces(locations=["0", exp], run_id=rid, return_type="list")
        OUT["client_search_by_run_both"] = len(got)
    except Exception as e:  # noqa: BLE001
        OUT["client_search_by_run_both_error"] = f"{type(e).__name__}: {str(e)[:300]}"
    # Where is the Run listed? Runs of each experiment.
    OUT["runs_in_exp0"] = len(c.search_runs(["0"]))
    OUT["runs_in_runs_exp"] = len(c.search_runs([exp]))
    exp_info = c.get_experiment(exp)
    OUT["runs_exp_tags"] = dict(exp_info.tags)
    IDS.update({"runs_experiment": exp, "run": rid, "stage_traces": stage_traces})
    json.dump(OUT, open("/out/round7.json", "w"), indent=1, default=str)
    json.dump(IDS, open("/out/round7-ids.json", "w"), indent=1, default=str)
    print("done")


if __name__ == "__main__":
    main()
