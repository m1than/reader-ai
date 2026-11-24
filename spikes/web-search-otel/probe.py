"""Spike probe (never shipped): one WebSearch run of the bundled CLI the way SRV-FRESH's search call drives it
(SRV-AGENT's CLI environment, tools ["WebSearch"], allowed_tools [], a PreToolUse hook that allows the planned query
once, max_turns 2, the prompt "Query: <q>", CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION=1), with SRV-OBS's trace export
on, inside our own parent span; then scan everything the OTLP receiver got for the canaries.

Modes:
  content-off    SRV-OBS's cli_env_additions(): trace export on and the four content flags 0 (production)
  content-unset  trace export on, content flags unset (CLI defaults)
  tool-details   positive control: OTEL_LOG_TOOL_DETAILS=1
  tool-content   positive control: OTEL_LOG_TOOL_CONTENT=1
Writes /out/report.json. No real token, no network, no credit.
"""

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, query
from google.protobuf.json_format import MessageToDict
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

CANARIES = {k: os.environ[k] for k in ("CANARY_QUERY", "CANARY_TITLE", "CANARY_URL", "CANARY_SUMMARY",
                                        "CANARY_SYSTEM", "HANDLE")}
QUERY = CANARIES["CANARY_QUERY"]
OTEL_ON = {  # SRV-OBS cli_env_additions() without the content flags, the receiver on 127.0.0.1
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
    "OTEL_TRACES_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_LOGS_EXPORTER": "none",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
}
CONTENT_OFF = {k: "0" for k in ("OTEL_LOG_USER_PROMPTS", "OTEL_LOG_ASSISTANT_RESPONSES", "OTEL_LOG_TOOL_CONTENT",
                                 "OTEL_LOG_TOOL_DETAILS")}
MODES = {
    "content-off": CONTENT_OFF,
    "content-unset": {},
    "tool-details": {**CONTENT_OFF, "OTEL_LOG_TOOL_DETAILS": "1"},
    "tool-content": {**CONTENT_OFF, "OTEL_LOG_TOOL_CONTENT": "1"},
}

provider = TracerProvider(resource=Resource.create({"service.name": "reader-spike"}))
provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint="http://127.0.0.1:4318/v1/traces")))
tracer = provider.get_tracer("reader.spike.web-search-otel")


def cli_env(home: Path, extra: dict[str, str]) -> dict[str, str]:
    env = {k: "" for k in os.environ}  # SRV-AGENT: every other variable of the worker is passed blank
    env.update({
        "PATH": os.environ["PATH"], "HOME": str(home), "LANG": "C.UTF-8",
        "CLAUDE_CODE_OAUTH_TOKEN": os.environ["HANDLE"], "ANTHROPIC_BASE_URL": "http://127.0.0.1:8080",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1000", "MAX_THINKING_TOKENS": "0",
        "CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION": "1",
        "HTTP_PROXY": "http://127.0.0.1:8081", "HTTPS_PROXY": "http://127.0.0.1:8081", "NO_PROXY": "127.0.0.1",
        **OTEL_ON, **extra,
    })
    return env


class Gate:
    """SRV-FRESH FR-FRESH-6's rule, inline: WebSearch with exactly {query: QUERY}, once."""

    def __init__(self) -> None:
        self.allowed = 0
        self.denied = 0

    async def __call__(self, data: dict, tool_use_id: Any, context: Any) -> dict:
        ok = (data.get("tool_name") == "WebSearch" and data.get("tool_input") == {"query": QUERY}
              and self.allowed == 0)
        if ok:
            self.allowed += 1
        else:
            self.denied += 1
        out = {"hookEventName": "PreToolUse", "permissionDecision": "allow" if ok else "deny"}
        if not ok:
            out["permissionDecisionReason"] = "denied"
        return {"hookSpecificOutput": out}


async def run(mode: str) -> dict:
    Path("/tmp/mode").write_text(mode)
    home = Path(f"/tmp/home-{mode}")
    home.mkdir(mode=0o700)
    (home / "system.md").write_text(f"Call WebSearch once with the query. Marker {CANARIES['CANARY_SYSTEM']}.")
    gate = Gate()
    opts = ClaudeAgentOptions(
        tools=["WebSearch"], allowed_tools=[], setting_sources=[], max_turns=2, model="claude-haiku-5-5",
        hooks={"PreToolUse": [HookMatcher(matcher="WebSearch", hooks=[gate])]},
        system_prompt={"type": "file", "path": str(home / "system.md")}, env=cli_env(home, MODES[mode]),
        cwd=str(home),
    )
    out: dict = {"mode": mode, "error": None, "messages": [], "tool_results": 0}
    with tracer.start_as_current_span("reader.job spike") as job, tracer.start_as_current_span("agent.call"):
        out["trace_id"] = format(job.get_span_context().trace_id, "032x")
        try:
            async for msg in query(prompt=f"Query: {QUERY}", options=opts):
                out["messages"].append(type(msg).__name__)
                if getattr(msg, "tool_use_result", None):
                    out["tool_results"] += 1
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    out["gate"] = {"allowed": gate.allowed, "denied": gate.denied}
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
                    "scope": ss.get("scope", {}).get("name"),
                    "name": s.get("name"), "span_id": s.get("spanId"), "parent": s.get("parentSpanId"),
                    "attributes": {a["key"]: a.get("value") for a in s.get("attributes", [])},
                    "events": [{"name": e.get("name"), "keys": sorted(a["key"] for a in e.get("attributes", []))}
                               for e in s.get("events", [])],
                })
    return rows


def where(spans: list[dict], value: str) -> list[str]:
    """`<span name>.<attribute>` for every attribute whose value holds the canary."""
    return sorted({f"{s['name']}.{k}" for s in spans for k, v in s["attributes"].items() if value in json.dumps(v)})


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
        cli = [s for s in spans if s["service"] != "reader-spike"]
        report["modes"][mode] = {
            "otlp_paths": sorted({q["path"] for q in otlp}),
            "authorization_present": any(q["authorization_present"] for q in otlp),
            "upstream_kinds": [q.get("kind") for q in mine if q["server"] == "upstream"],
            "other_hosts": [q["target"] for q in mine if q["server"] == "forward_proxy"],
            "canary_hits_decoded": hits, "canary_hits_raw": raw_hits,
            "canary_where": {k: where(cli, v) for k, v in CANARIES.items()},
            "cli_span_names": [s["name"] for s in cli],
            "cli_span_keys": {s["name"]: sorted(s["attributes"]) for s in cli},
        }
    with open("/out/report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(json.dumps({m: {"spans": v["cli_span_names"], "hits": v["canary_hits_raw"], "where": v["canary_where"],
                          "upstream": v["upstream_kinds"], "other_hosts": v["other_hosts"]}
                      for m, v in report["modes"].items()}, indent=1))
    print(json.dumps([{k: r[k] for k in ("mode", "error", "gate", "tool_results")} for r in runs]))


if __name__ == "__main__":
    main()
