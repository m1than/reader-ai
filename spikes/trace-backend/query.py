"""Query a backend for one trace. Phoenix through its REST API from the host; MLflow through its Python client
inside the server container. Prints one JSON object (ids, names, statuses; no content).

    python3 query.py phoenix|mlflow <otel-trace-id-hex>            nesting, timed triage, score round trip
    python3 query.py phoenix|mlflow <otel-trace-id-hex> --count    only the number of spans ingested (replay check)

Triage timing covers only fetching the trace's spans (fetch_s) and walking the tree to the failing stage (walk_s).
For MLflow, fetch_s is timed inside the container around get_trace(), so Python start-up of `docker exec` is excluded.
"""

import json
import subprocess
import sys
import time
import urllib.request

BACKEND, TRACE = sys.argv[1], sys.argv[2]
COUNT_ONLY = "--count" in sys.argv
PHOENIX = "http://127.0.0.1:6006"

MLFLOW_FETCH = r"""
import json, sys, time, mlflow
mlflow.set_tracking_uri("http://localhost:5000")
t0 = time.monotonic()
t = mlflow.get_trace("tr-" + sys.argv[1])
fetch_s = time.monotonic() - t0
spans = [{"name": s.name, "span_id": s.span_id, "parent_id": s.parent_id,
          "status": getattr(s.status.status_code, "name", str(s.status.status_code)),
          "message": str(s.status.description or "")[:200]} for s in t.data.spans]
print(json.dumps({"spans": spans, "fetch_s": fetch_s}))
"""

MLFLOW_SCORE = r"""
import json, sys, mlflow
from mlflow.entities import AssessmentSource, AssessmentSourceType
mlflow.set_tracking_uri("http://localhost:5000")
tid = "tr-" + sys.argv[1]
mlflow.log_feedback(trace_id=tid, name="reader.quality", value=0.8, rationale="spike score", span_id=sys.argv[2],
                    source=AssessmentSource(source_type=AssessmentSourceType.CODE, source_id="spike"))
print(json.dumps([(a.name, getattr(a, "value", None), a.span_id) for a in mlflow.get_trace(tid).info.assessments]))
"""


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def post(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def in_mlflow(script: str, *args: str) -> str:
    out = subprocess.run(["docker", "exec", "-i", "reader-spike-trace-mlflow-1", "python", "-", *args],
                         input=script, capture_output=True, text=True, check=True).stdout
    return out.strip().splitlines()[-1]


def fetch() -> tuple[list[dict], float]:
    if BACKEND == "mlflow":
        r = json.loads(in_mlflow(MLFLOW_FETCH, TRACE))
        return r["spans"], r["fetch_s"]
    t0, raw, cursor = time.monotonic(), [], None
    while True:
        page = get(f"{PHOENIX}/v1/projects/default/spans?limit=1000" + (f"&cursor={cursor}" if cursor else ""))
        raw += page["data"]
        cursor = page.get("next_cursor")
        if not cursor:
            break
    spans = [{"name": s["name"], "span_id": s["context"]["span_id"], "parent_id": s["parent_id"],
              "status": s["status_code"], "message": str(s.get("status_message") or "")[:200]}
             for s in raw if s["context"]["trace_id"] == TRACE]
    return spans, time.monotonic() - t0


def score(span_id: str) -> list:
    if BACKEND == "mlflow":
        return json.loads(in_mlflow(MLFLOW_SCORE, TRACE, span_id))
    post(f"{PHOENIX}/v1/span_annotations?sync=true", {"data": [{
        "span_id": span_id, "name": "reader.quality", "annotator_kind": "CODE",
        "result": {"label": "pass", "score": 0.8, "explanation": "spike score"}}]})
    return [(a["name"], a["result"].get("score"), a["span_id"])
            for a in get(f"{PHOENIX}/v1/projects/default/span_annotations?span_ids={span_id}")["data"]]


spans, fetch_s = fetch()
if COUNT_ONLY:
    print(json.dumps({"span_count": len(spans), "fetch_s": round(fetch_s, 3)}))
    sys.exit(0)

t_walk = time.monotonic()
by_id = {s["span_id"]: s for s in spans}
errors = [s for s in spans if s["status"] == "ERROR"]
# Triage: the deepest error span (a CLI span under a stage) names the cause; walk up to the stage it belongs to.
deepest = max(errors, key=lambda s: (s["name"].startswith("claude_code"), len(s["message"])), default=None)
cur = deepest
while cur and not cur["name"].startswith("reader.stage"):
    cur = by_id.get(cur["parent_id"])
walk_s = time.monotonic() - t_walk

stages = {s["span_id"]: s["name"] for s in spans if s["name"].startswith("reader.stage")}
interactions = [s for s in spans if s["name"] == "claude_code.interaction"]
ok_stage = next(s for s in spans if s["name"].startswith("reader.stage") and s["status"] != "ERROR")
read_back = score(ok_stage["span_id"])
print(json.dumps({
    "span_count": len(spans),
    "nesting": {
        "interactions": len(interactions),
        "interactions_under_stage": sum(1 for s in interactions if s["parent_id"] in stages),
        "llm_requests_under_interaction": sum(1 for s in spans if s["name"] == "claude_code.llm_request"
                                              and by_id.get(s["parent_id"], {}).get("name") == "claude_code.interaction"),
        "edges": [{"span": s["name"], "span_id": s["span_id"], "parent": by_id.get(s["parent_id"], {}).get("name"),
                   "parent_id": s["parent_id"]} for s in spans],
    },
    "triage": {"fetch_s": round(fetch_s, 3), "walk_s": round(walk_s, 6),
               "error_spans": [{"name": e["name"], "message": e["message"]} for e in errors],
               "cause": deepest["message"] if deepest else None, "stage": cur["name"] if cur else None},
    "score": {"span_id": ok_stage["span_id"], "read_back": read_back, "visible": bool(read_back)},
}))
