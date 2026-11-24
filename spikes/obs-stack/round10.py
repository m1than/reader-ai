"""Spike obs-stack round 10 (never shipped): the phase 2 writes round 8 did not replay, and three open ops checks.
Raw REST against the pinned MLflow 3.18.0 with round 9's production flags; zero credit; synthetic ids.
  - obs.setup: rename experiment 0, set an experiment tag twice (idempotent), get-by-name; LoggedModel create,
    finalize (status READY) and search; a re-run of the whole setup changes nothing (drift check by a state dump).
  - obs.bench: datasets/search by name in `bench`.
  - obs.retention: runs/delete, logged-models delete; what the UI lists afterwards (counts).
  - ops: --allowed-hosts on a non-health path, a CORS preflight, /graphql with and without a body.
Writes /out/round10.json.
"""

import json
import time
import urllib.error
import urllib.request

MLFLOW = "http://mlflow:5000"
LOG: list[dict] = []
OUT: dict = {"raw": LOG}


def rest(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(MLFLOW + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            status, got = r.status, (json.loads(raw) if raw[:1] in b"{[" else {"_text": raw[:80].decode("latin1")})
            hdrs = dict(r.headers)
    except urllib.error.HTTPError as e:
        status, got, hdrs = e.code, {"error": e.read().decode()[:200]}, dict(e.headers)
    LOG.append({"method": method, "path": path, "status": status, "body": body})
    got["_headers"] = {k: v for k, v in hdrs.items() if k.lower().startswith("access-control")}
    return status, got


def wait() -> None:
    for _ in range(120):
        try:
            if urllib.request.urlopen(MLFLOW + "/health", timeout=2).status == 200:
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)


SETUP = {"0": {"name": "reader", "tags": {"reader.kind": "traces", "reader.managed_by": "obs.setup",
                                          "mlflow.note.content": "reader-ai traces"}},
         "bench": {"tags": {"reader.kind": "bench", "reader.managed_by": "obs.setup"}}}


def state() -> dict:
    s, got = rest("POST", "/api/2.0/mlflow/experiments/search", {"max_results": 100})
    exps = {e["experiment_id"]: {"name": e["name"], "tags": {t["key"]: t["value"] for t in e.get("tags", [])
                                                             if t["key"].startswith(("reader.", "mlflow.note"))}}
            for e in got.get("experiments", [])}
    s, got = rest("POST", "/api/2.0/mlflow/logged-models/search", {"experiment_ids": ["0"]})
    models = sorted((m["info"]["name"], m["info"].get("status")) for m in got.get("models", []))
    return {"experiments": exps, "models": models}


def setup() -> list[str]:
    """obs.setup: compare and write only the difference; returns the writes it made."""
    made = []
    s, got = rest("GET", "/api/2.0/mlflow/experiments/get?experiment_id=0")
    exp0 = got["experiment"]
    if exp0["name"] != SETUP["0"]["name"]:
        rest("POST", "/api/2.0/mlflow/experiments/update", {"experiment_id": "0", "new_name": SETUP["0"]["name"]})
        made.append("rename 0")
    have = {t["key"]: t["value"] for t in exp0.get("tags", [])}
    for k, v in SETUP["0"]["tags"].items():
        if have.get(k) != v:
            rest("POST", "/api/2.0/mlflow/experiments/set-experiment-tag", {"experiment_id": "0", "key": k, "value": v})
            made.append(f"tag 0 {k}")
    s, got = rest("GET", "/api/2.0/mlflow/experiments/get-by-name?experiment_name=bench")
    if s == 404:
        s, got = rest("POST", "/api/2.0/mlflow/experiments/create", {"name": "bench", "tags": [
            {"key": k, "value": v} for k, v in SETUP["bench"]["tags"].items()]})
        made.append("create bench")
    s, got = rest("POST", "/api/2.0/mlflow/logged-models/search", {
        "experiment_ids": ["0"], "filter": "tags.`reader.version_key` = 'abababababab'"})
    if not got.get("models"):
        s, got = rest("POST", "/api/2.0/mlflow/logged-models", {"experiment_id": "0", "name": "reader-agent",
            "params": [{"key": "prompt_set_sha256", "value": "ab" * 32}],
            "tags": [{"key": "reader.version_key", "value": "abababababab"}]})
        mid = got["model"]["info"]["model_id"]
        s, got = rest("PATCH", f"/api/2.0/mlflow/logged-models/{mid}", {"model_id": mid, "status": "LOGGED_MODEL_READY"})
        OUT["finalize"] = {"status": s, "model_status": ((got.get("model") or {}).get("info") or {}).get("status")}
        made.append("create model")
    return made


def main() -> None:
    wait()
    OUT["first_setup"] = setup()
    first = state()
    OUT["second_setup"] = setup()
    OUT["drift"] = first != state()
    OUT["state"] = state()
    # obs.bench: a dataset found again by name in `bench`.
    bench = OUT["state"]["experiments"]
    bench_id = next(k for k, v in bench.items() if v["name"] == "bench")
    rest("POST", "/api/3.0/mlflow/datasets/create", {"name": "tutor-bench-v2", "experiment_ids": [bench_id]})
    s, got = rest("POST", "/api/3.0/mlflow/datasets/search", {"experiment_ids": [bench_id],
                                                              "filter_string": "name = 'tutor-bench-v2'"})
    OUT["dataset_search"] = {"status": s, "found": len(got.get("datasets", []))}
    # obs.retention: delete a Run and a LoggedModel.
    s, got = rest("POST", "/api/2.0/mlflow/runs/create", {"experiment_id": "0", "run_name": "old"})
    rid = got["run"]["info"]["run_id"]
    OUT["run_delete"] = rest("POST", "/api/2.0/mlflow/runs/delete", {"run_id": rid})[0]
    s, got = rest("POST", "/api/2.0/mlflow/runs/search", {"experiment_ids": ["0"]})
    OUT["runs_listed_after_delete"] = len(got.get("runs", []))
    s, got = rest("POST", "/api/2.0/mlflow/logged-models/search", {"experiment_ids": ["0"]})
    mid = got["models"][0]["info"]["model_id"]
    OUT["model_delete"] = rest("DELETE", f"/api/2.0/mlflow/logged-models/{mid}")[0]
    s, got = rest("POST", "/api/2.0/mlflow/logged-models/search", {"experiment_ids": ["0"]})
    OUT["models_listed_after_delete"] = len(got.get("models", []))
    # ops checks.
    OUT["bad_host_api"] = rest("GET", "/api/2.0/mlflow/experiments/search?max_results=1", headers={"Host": "evil.example"})[0]
    OUT["bad_host_health"] = rest("GET", "/health", headers={"Host": "evil.example"})[0]
    s, got = rest("OPTIONS", "/api/2.0/mlflow/experiments/search", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    OUT["preflight"] = {"status": s, "headers": got["_headers"]}
    OUT["graphql_empty"] = rest("POST", "/graphql", {})[0]
    OUT["graphql_query"] = rest("POST", "/graphql", {"query": "{ __typename }"})[0]
    json.dump(OUT, open("/out/round10.json", "w"), indent=1, default=str)
    print(json.dumps({k: v for k, v in OUT.items() if k != "raw"})[:3000])


if __name__ == "__main__":
    main()
