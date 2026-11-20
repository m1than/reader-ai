"""Query a backend for one trace: nesting, error triage (timed), and a quality score round trip.
Phoenix through its REST API from the host; MLflow through its Python client inside the server container.
Prints one JSON object (ids, names, statuses; no content).

    python3 query.py phoenix|mlflow <otel-trace-id-hex>
"""

import json
import subprocess
import sys
import time
import urllib.request

BACKEND, TRACE = sys.argv[1], sys.argv[2]
PHOENIX = "http://127.0.0.1:6006"

MLFLOW_SCRIPT = r"""
import json, sys, time, mlflow
from mlflow.entities import AssessmentSource, AssessmentSourceType
mlflow.set_tracking_uri("http://localhost:5000")
tid = "tr-" + sys.argv[1]
t0 = time.monotonic()
t = mlflow.get_trace(tid)
spans = [{"name": s.name, "span_id": s.span_id, "parent_id": s.parent_id,
          "status": s.status.status_code.name if hasattr(s.status.status_code, "name") else str(s.status.status_code),
          "message": str(s.status.description or "")[:200]} for s in t.data.spans]
fetch_s = time.monotonic() - t0
stage = next(s for s in spans if s["name"].startswith("reader.stage") and s["status"] != "ERROR")
mlflow.log_feedback(trace_id=tid, name="reader.quality", value=0.8, rationale="spike score", span_id=stage["span_id"],
                    source=AssessmentSource(source_type=AssessmentSourceType.CODE, source_id="spike"))
got = [(a.name, getattr(a, "value", None), a.span_id) for a in mlflow.get_trace(tid).info.assessments]
print(json.dumps({"spans": spans, "fetch_s": round(fetch_s, 3),
                  "score": {"span_id": stage["span_id"], "read_back": got}}))
"""


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def post(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def phoenix() -> dict:
    t0 = time.monotonic()
    raw = get(f"{PHOENIX}/v1/projects/default/spans?limit=1000")["data"]
    spans = [{"name": s["name"], "span_id": s["context"]["span_id"], "parent_id": s["parent_id"],
              "status": s["status_code"], "message": str(s.get("status_message") or "")[:200]}
             for s in raw if s["context"]["trace_id"] == TRACE]
    fetch_s = time.monotonic() - t0
    stage = next(s for s in spans if s["name"].startswith("reader.stage") and s["status"] != "ERROR")
    post(f"{PHOENIX}/v1/span_annotations?sync=true", {"data": [{
        "span_id": stage["span_id"], "name": "reader.quality", "annotator_kind": "CODE",
        "result": {"label": "pass", "score": 0.8, "explanation": "spike score"}}]})
    got = [(a["name"], a["result"].get("score"), a["span_id"])
           for a in get(f"{PHOENIX}/v1/projects/default/span_annotations?span_ids={stage['span_id']}")["data"]]
    return {"spans": spans, "fetch_s": round(fetch_s, 3), "score": {"span_id": stage["span_id"], "read_back": got}}


def mlflow() -> dict:
    out = subprocess.run(["docker", "exec", "-i", "reader-spike-trace-mlflow-1", "python", "-", TRACE],
                         input=MLFLOW_SCRIPT, capture_output=True, text=True, check=True).stdout
    return json.loads(out.strip().splitlines()[-1])


t_triage = time.monotonic()
r = phoenix() if BACKEND == "phoenix" else mlflow()
spans = r["spans"]
by_id = {s["span_id"]: s for s in spans}
stages = {s["span_id"]: s["name"] for s in spans if s["name"].startswith("reader.stage")}
interactions = [s for s in spans if s["name"] == "claude_code.interaction"]
errors = [s for s in spans if s["status"] == "ERROR"]
# Triage: the deepest error span (a CLI span under a stage) names the cause; walk up to the stage it belongs to.
deepest = max(errors, key=lambda s: (s["name"].startswith("claude_code"), len(s["message"])), default=None)
cause_stage = None
if deepest:
    cur = deepest
    while cur and not cur["name"].startswith("reader.stage"):
        cur = by_id.get(cur["parent_id"])
    cause_stage = cur["name"] if cur else None
triage_s = time.monotonic() - t_triage
print(json.dumps({
    "span_count": len(spans),
    "names": sorted({s["name"] for s in spans}),
    "nesting": {
        "interactions": len(interactions),
        "interactions_under_stage": sum(1 for s in interactions if s["parent_id"] in stages),
        "llm_requests_under_interaction": sum(1 for s in spans if s["name"] == "claude_code.llm_request"
                                              and by_id.get(s["parent_id"], {}).get("name") == "claude_code.interaction"),
        "edges": [{"span": s["name"], "span_id": s["span_id"], "parent": by_id.get(s["parent_id"], {}).get("name"),
                   "parent_id": s["parent_id"]} for s in spans],
    },
    "triage": {"seconds_via_api": round(triage_s, 2), "error_spans": [{"name": e["name"], "message": e["message"]} for e in errors],
               "cause": deepest["message"] if deepest else None, "stage": cause_stage},
    "score": {**r["score"], "visible": bool(r["score"]["read_back"])},
}))
