"""Spike probe (never shipped): run the bundled CLI under SRV-AGENT's CLI environment plus OpenTelemetry trace export,
inside our own parent span, and scan everything the OTLP receiver got for the canaries.

Modes:
  agent-env               SRV-AGENT FR-AGENT-6 environment + trace export; content flags unset (CLI defaults)
  agent-env-explicit-off  the same + every content flag set to 0
  positive-control        the same + OTEL_LOG_USER_PROMPTS=1, OTEL_LOG_ASSISTANT_RESPONSES=1 (the scan must find them)
Writes /out/report.json. No real token, no network, no credit.
"""

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query
from google.protobuf.json_format import MessageToDict
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

CANARIES = {k: os.environ[k] for k in ("CANARY_PROMPT", "CANARY_SYSTEM", "CANARY_RESPONSE", "HANDLE")}
OTEL_ON = {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
    "OTEL_TRACES_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_LOGS_EXPORTER": "none",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
}
CONTENT_OFF = {k: "0" for k in ("OTEL_LOG_USER_PROMPTS", "OTEL_LOG_ASSISTANT_RESPONSES", "OTEL_LOG_TOOL_CONTENT",
                                 "OTEL_LOG_TOOL_DETAILS", "OTEL_LOG_RAW_API_BODIES")}
MODES = {
    "agent-env": {},
    "agent-env-explicit-off": CONTENT_OFF,
    "positive-control": {"OTEL_LOG_USER_PROMPTS": "1", "OTEL_LOG_ASSISTANT_RESPONSES": "1"},
}

provider = TracerProvider(resource=Resource.create({"service.name": "reader-spike"}))
provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint="http://127.0.0.1:4318/v1/traces")))
tracer = provider.get_tracer("reader.spike.cli-otel-content")


def cli_env(home: Path, extra: dict[str, str]) -> dict[str, str]:
    env = {k: "" for k in os.environ}  # SRV-AGENT: every other variable of the worker is passed blank
    env.update({
        "PATH": os.environ["PATH"], "HOME": str(home), "LANG": "C.UTF-8",
        "CLAUDE_CODE_OAUTH_TOKEN": os.environ["HANDLE"], "ANTHROPIC_BASE_URL": "http://127.0.0.1:8080",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1000",
        "HTTP_PROXY": "http://127.0.0.1:8081", "HTTPS_PROXY": "http://127.0.0.1:8081", "NO_PROXY": "127.0.0.1",
        **OTEL_ON, **extra,
    })
    return env


async def run(mode: str) -> dict:
    Path("/tmp/mode").write_text(mode)
    home = Path(f"/tmp/home-{mode}")
    home.mkdir(mode=0o700)
    (home / "system.md").write_text(f"You are a tutor. Secret marker {CANARIES['CANARY_SYSTEM']}.")
    opts = ClaudeAgentOptions(tools=[], allowed_tools=[], setting_sources=[], max_turns=1, model="claude-sonnet-5-5",
                              system_prompt={"type": "file", "path": str(home / "system.md")},
                              env=cli_env(home, MODES[mode]))
    out: dict = {"mode": mode, "error": None, "messages": []}
    with tracer.start_as_current_span("reader.job spike") as job, tracer.start_as_current_span("reader.stage draft"):
        out["trace_id"] = format(job.get_span_context().trace_id, "032x")
        try:
            async for msg in query(prompt=f"Explain this passage: {CANARIES['CANARY_PROMPT']}", options=opts):
                out["messages"].append(type(msg).__name__)
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    provider.force_flush()
    time.sleep(3)  # the CLI flushes before it exits; give stragglers time
    shutil.rmtree(home, ignore_errors=True)
    return out


def decode(path: str, data: bytes) -> dict:
    cls = {"/v1/traces": ExportTraceServiceRequest, "/v1/logs": ExportLogsServiceRequest,
           "/v1/metrics": ExportMetricsServiceRequest}.get(path)
    if cls is None:
        return {}
    msg = cls()
    msg.ParseFromString(data)
    return MessageToDict(msg)


def spans_of(doc: dict) -> list[dict]:
    rows = []
    for rs in doc.get("resourceSpans", []):
        res = {a["key"]: a.get("value") for a in rs.get("resource", {}).get("attributes", [])}
        for ss in rs.get("scopeSpans", []):
            for s in ss.get("spans", []):
                rows.append({
                    "service": (res.get("service.name") or {}).get("stringValue"),
                    "resource_keys": sorted(res),
                    "scope": ss.get("scope", {}).get("name"),
                    "name": s.get("name"), "trace_id": s.get("traceId"), "span_id": s.get("spanId"),
                    "parent": s.get("parentSpanId"),
                    "attributes": {a["key"]: a.get("value") for a in s.get("attributes", [])},
                    "events": [{"name": e.get("name"), "keys": sorted(a["key"] for a in e.get("attributes", []))}
                               for e in s.get("events", [])],
                })
    return rows


def main() -> None:
    runs = [asyncio.run(run(m)) for m in (sys.argv[1:] or list(MODES))]
    provider.shutdown()
    time.sleep(1)
    reqs = [json.loads(line) for line in open("/out/requests.jsonl")]
    report: dict = {"runs": runs, "modes": {}}
    for r in runs:
        mode = r["mode"]
        mine = [q for q in reqs if q.get("mode") == mode]
        otlp = [q for q in mine if q["server"] == "otlp"]
        spans, hits, raw_hits = [], {k: 0 for k in CANARIES}, {k: 0 for k in CANARIES}
        for q in otlp:
            data = open(q["file"], "rb").read()
            doc = decode(q["path"], data)
            text = json.dumps(doc)
            for k, v in CANARIES.items():
                hits[k] += text.count(v)
                raw_hits[k] += data.count(v.encode())
            spans += spans_of(doc)
        ours = {s["span_id"]: s for s in spans if s["service"] == "reader-spike"}
        stage = next((s for s in ours.values() if s["name"] == "reader.stage draft"), None)
        cli = [s for s in spans if s["service"] != "reader-spike"]
        report["modes"][mode] = {
            "otlp_requests": [{k: q[k] for k in ("path", "bytes", "header_names", "authorization_present")}
                              for q in otlp],
            "upstream_requests": [{k: q.get(k) for k in ("method", "path")} for q in mine if q["server"] == "upstream"],
            "other_hosts": [q["target"] for q in mine if q["server"] == "forward_proxy"],
            "canary_hits_decoded": hits, "canary_hits_raw": raw_hits,
            "cli_spans": [{k: s[k] for k in ("service", "scope", "name", "parent", "resource_keys", "events")}
                          | {"attribute_keys": sorted(s["attributes"])} for s in cli],
            "cli_span_attributes": [{"name": s["name"], "attributes": s["attributes"]} for s in cli],
            "cli_in_our_trace": all(s["trace_id"] == r.get("trace_id_b64", s["trace_id"]) for s in cli),
            "cli_roots_under_stage": [s["name"] for s in cli if stage and s["parent"] == stage["span_id"]],
            "trace_ids": sorted({s["trace_id"] for s in spans}),
        }
    with open("/out/report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(json.dumps({m: {"cli_spans": len(v["cli_spans"]), "hits": v["canary_hits_raw"],
                          "under_stage": v["cli_roots_under_stage"], "other_hosts": v["other_hosts"]}
                      for m, v in report["modes"].items()}, indent=1))


if __name__ == "__main__":
    main()
