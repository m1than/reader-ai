"""Spike obs-stack round 12 (never shipped): design review round 3, R1 and R2, as raw REST against the pinned MLflow
3.18.0 (zero credit, synthetic ids). R1: a param logged again with the same value is accepted; `runs/search` returns
a Run's params, so obs.runs can log only the missing ones. R2: PATCH logged-models/{id}/tags sets
`reader.last_started`, `logged-models/search` reads it back, and a second PATCH sets a new day. Writes
/out/round12.json with each request's method, path, status and body.
"""

import json
import time
import urllib.error
import urllib.request

MLFLOW = "http://mlflow:5000"
LOG: list[dict] = []
OUT: dict = {"raw": LOG}


def rest(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(MLFLOW + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            status, got = r.status, (json.loads(raw) if raw[:1] in b"{[" else {})
    except urllib.error.HTTPError as e:
        status, got = e.code, {"error": e.read().decode()[:200]}
    LOG.append({"method": method, "path": path, "status": status, "body": body})
    return status, got


def wait() -> None:
    for _ in range(120):
        try:
            if urllib.request.urlopen(MLFLOW + "/health", timeout=2).status == 200:
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)


def main() -> None:
    wait()
    now = int(time.time() * 1000)
    # R1: params of an incomplete Run.
    s, got = rest("POST", "/api/2.0/mlflow/runs/create", {"experiment_id": "0", "run_name": "compile r12", "start_time": now,
                                                          "tags": [{"key": "reader.run_id", "value": "r12"}]})
    rid = got["run"]["info"]["run_id"]
    params = [{"key": "version_key", "value": "abababababab"}, {"key": "model.draft", "value": "claude-sonnet-5-5"}]
    OUT["first_log"] = rest("POST", "/api/2.0/mlflow/runs/log-batch", {"run_id": rid, "params": params[:1], "metrics": [], "tags": []})[0]
    OUT["same_value_relog"] = rest("POST", "/api/2.0/mlflow/runs/log-batch", {"run_id": rid, "params": params[:1], "metrics": [], "tags": []})[0]
    s, got = rest("POST", "/api/2.0/mlflow/runs/search", {"experiment_ids": ["0"], "filter": "tags.`reader.run_id` = 'r12'", "max_results": 2})
    have = {p["key"]: p["value"] for p in got["runs"][0]["data"].get("params", [])}
    OUT["params_from_search"] = have
    missing = [p for p in params if p["key"] not in have]
    OUT["missing_log"] = rest("POST", "/api/2.0/mlflow/runs/log-batch", {"run_id": rid, "params": missing, "metrics": [], "tags": []})[0]
    OUT["changed_value_relog"] = rest("POST", "/api/2.0/mlflow/runs/log-batch", {
        "run_id": rid, "params": [{"key": "version_key", "value": "cdcdcdcdcdcd"}], "metrics": [], "tags": []})[0]
    s, got = rest("GET", f"/api/2.0/mlflow/runs/get?run_id={rid}")
    OUT["params_after"] = {p["key"]: p["value"] for p in got["run"]["data"].get("params", [])}
    # R2: the LoggedModel's last-started tag.
    s, got = rest("POST", "/api/2.0/mlflow/logged-models", {"experiment_id": "0", "name": "reader-agent",
        "tags": [{"key": "reader.version_key", "value": "abababababab"}]})
    mid = got["model"]["info"]["model_id"]
    flt = "tags.`reader.version_key` = 'abababababab'"
    for day in ("2026-11-24", "2026-11-25"):
        s, _ = rest("PATCH", f"/api/2.0/mlflow/logged-models/{mid}/tags", {"tags": [{"key": "reader.last_started", "value": day}]})
        s2, got = rest("POST", "/api/2.0/mlflow/logged-models/search", {"experiment_ids": ["0"], "filter": flt})
        tags = {t["key"]: t["value"] for t in got["models"][0]["info"].get("tags", [])}
        OUT[f"tag_{day}"] = {"patch_status": s, "search_status": s2, "read_back": tags.get("reader.last_started")}
    json.dump(OUT, open("/out/round12.json", "w"), indent=1, default=str)
    print(json.dumps({k: v for k, v in OUT.items() if k != "raw"}))


if __name__ == "__main__":
    main()
