"""Spike round 4, Jaeger option (never shipped). Runs in the driver container after analytics.py: checks that Rotel's
second exporter delivered the same traces to Jaeger, replays BULK compile-like traces (45 spans each) to load
Jaeger's in-memory store, and records Jaeger's view of one trace and its service list. Writes /out/jaeger.json."""

import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, "/spike")
import analytics as a  # noqa: E402

JAEGER = "http://jaeger:16686"
BULK = int(os.environ.get("BULK", "300"))
out: dict = {}


def get(path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(JAEGER + path, timeout=30) as r:
            return r.status, json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)[:200]}


ids = json.load(open("/out/browser-ids.json"))
for _ in range(30):
    status, _body = get("/api/services")
    if status == 200:
        break
    time.sleep(1)
out["services"] = get("/api/services")[1].get("data")
for name in ("compile_like", "tutor_slow", "stage_review_dead"):
    status, body = get(f"/api/traces/{ids[name]}")
    spans = (body.get("data") or [{}])[0].get("spans", []) if status == 200 else []
    out[name] = {"status": status, "spans": len(spans),
                 "names": sorted({s["operationName"] for s in spans})[:12]}
t0 = time.time()
for i in range(BULK):
    a.compile_like(f"bulk_{i}", 140 - i * 0.01)
a.provider.force_flush()
a.cli_provider.force_flush()
out["bulk"] = {"traces": BULK, "spans": BULK * 45, "seconds": round(time.time() - t0, 1)}
time.sleep(10)
status, body = get("/api/traces?service=reader-worker&limit=2000&lookback=2h")
out["bulk"]["found_reader_worker_traces"] = len(body.get("data", [])) if status == 200 else body
json.dump(out, open("/out/jaeger.json", "w"), indent=1)
print("jaeger done")
