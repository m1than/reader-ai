"""CLI canary harness probe (SRV-OBS INV-OBS-2, FR-OBS-6 b; moved from spikes/cli-otel-content).

Runs the bundled CLI under SRV-AGENT's CLI environment plus the telemetry additions the test passes in OBS_ENV (JSON,
from reader.obs.cli_env.cli_env_additions with the endpoint pointed at the local receiver), inside our own parent
span, then scans everything the OTLP receiver got. Modes: `agent-env` (the additions as given) and
`positive-control` (plus OTEL_LOG_USER_PROMPTS=1 and OTEL_LOG_ASSISTANT_RESPONSES=1). Writes /out/report.json.
SRV-FRESH's WebSearch run (moved from spikes/web-search-otel), as SRV-AGENT FR-AGENT-15 drives it: tools
["WebSearch"], allowed_tools [], a PreToolUse hook matching WebSearch, max_turns 2, the prompt `Query: <q>`,
MAX_THINKING_TOKENS=0 and CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION=1. Modes: `websearch-allow` (the gate allows
the stub's query once), `websearch-deny` (the planned query differs, so the gate denies the stub's) and
`websearch-tool-content` (allow plus OTEL_LOG_TOOL_CONTENT=1, the positive control).
No real token, no network, no credit.
"""

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, query
from google.protobuf.json_format import MessageToDict
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

CANARIES = {
    k: os.environ[k]
    for k in ("CANARY_PROMPT", "CANARY_SYSTEM", "CANARY_RESPONSE", "HANDLE",
              "CANARY_QUERY", "CANARY_TITLE", "CANARY_URL", "CANARY_SUMMARY")
}
ADDITIONS = json.loads(os.environ["OBS_ENV"])
MODES = {
    "agent-env": {},
    "positive-control": {"OTEL_LOG_USER_PROMPTS": "1", "OTEL_LOG_ASSISTANT_RESPONSES": "1"},
    "websearch-allow": {},
    "websearch-deny": {},
    "websearch-tool-content": {"OTEL_LOG_TOOL_CONTENT": "1"},
}
SEARCH_ENV = {"MAX_THINKING_TOKENS": "0", "CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION": "1"}


class Gate:
    """SRV-FRESH FR-FRESH-6's rule: WebSearch with exactly {query: <planned>}, once."""

    def __init__(self, planned: str) -> None:
        self.planned = planned
        self.allowed = 0
        self.denied = 0

    async def __call__(self, data: dict, tool_use_id: object, context: object) -> dict:
        ok = (data.get("tool_name") == "WebSearch" and data.get("tool_input") == {"query": self.planned}
              and self.allowed == 0)
        if ok:
            self.allowed += 1
        else:
            self.denied += 1
        out = {"hookEventName": "PreToolUse", "permissionDecision": "allow" if ok else "deny"}
        if not ok:
            out["permissionDecisionReason"] = "denied"
        return {"hookSpecificOutput": out}

provider = TracerProvider(resource=Resource.create({"service.name": "reader-canary"}))
provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint="http://127.0.0.1:4318/v1/traces")))
tracer = provider.get_tracer("reader")


def cli_env(home: Path, extra: dict[str, str]) -> dict[str, str]:
    env = {k: "" for k in os.environ}  # SRV-AGENT: every other variable of the worker is passed blank
    env.update({
        "PATH": os.environ["PATH"], "HOME": str(home), "LANG": "C.UTF-8",
        "CLAUDE_CODE_OAUTH_TOKEN": os.environ["HANDLE"], "ANTHROPIC_BASE_URL": "http://127.0.0.1:8080",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1000",
        "HTTP_PROXY": "http://127.0.0.1:8081", "HTTPS_PROXY": "http://127.0.0.1:8081", "NO_PROXY": "127.0.0.1",
        **ADDITIONS, **extra,
    })
    return env


async def run(mode: str) -> dict:
    Path("/tmp/mode").write_text(mode)
    home = Path(f"/tmp/home-{mode}")
    home.mkdir(mode=0o700)
    (home / "system.md").write_text(f"You are a tutor. Marker {CANARIES['CANARY_SYSTEM']}.")
    gate: Gate | None = None
    if mode.startswith("websearch"):
        planned = CANARIES["CANARY_QUERY"] if mode != "websearch-deny" else "planned query of another run"
        gate = Gate(planned)
        prompt, job = f"Query: {planned}", "job freshness.web"
        opts = ClaudeAgentOptions(
            tools=["WebSearch"], allowed_tools=[], setting_sources=[], max_turns=2, model="claude-haiku-5-5",
            hooks={"PreToolUse": [HookMatcher(matcher="WebSearch", hooks=[gate])]},
            system_prompt={"type": "file", "path": str(home / "system.md")},
            env=cli_env(home, {**SEARCH_ENV, **MODES[mode]}), cwd=str(home),
        )
    else:
        prompt, job = f"Explain: {CANARIES['CANARY_PROMPT']}", "job tutor.answer"
        opts = ClaudeAgentOptions(tools=[], allowed_tools=[], setting_sources=[], max_turns=1,
                                  model="claude-sonnet-5-5",
                                  system_prompt={"type": "file", "path": str(home / "system.md")},
                                  env=cli_env(home, MODES[mode]))
    out: dict = {"mode": mode, "error": None, "tool_results": 0}
    with tracer.start_as_current_span(job), tracer.start_as_current_span("agent.call") as call:
        out["call_span"] = format(call.get_span_context().span_id, "016x")
        try:
            async for msg in query(prompt=prompt, options=opts):
                if getattr(msg, "tool_use_result", None):
                    out["tool_results"] += 1
        except Exception as exc:
            out["error"] = type(exc).__name__
    if gate is not None:
        out["gate"] = {"allowed": gate.allowed, "denied": gate.denied}
    provider.force_flush()
    time.sleep(3)
    shutil.rmtree(home, ignore_errors=True)
    return out


def main() -> None:
    runs = [asyncio.run(run(m)) for m in (sys.argv[1:] or list(MODES))]
    provider.shutdown()
    time.sleep(1)
    reqs = [json.loads(line) for line in open("/out/requests.jsonl")]
    report: dict = {"modes": {}}
    for r in runs:
        mine = [q for q in reqs if q.get("mode") == r["mode"]]
        hits = {k: 0 for k in CANARIES}
        spans: list[dict] = []
        auth = False
        for q in (q for q in mine if q["server"] == "otlp"):
            data = open(q["file"], "rb").read()
            auth = auth or q["authorization_present"]
            for k, v in CANARIES.items():
                hits[k] += data.count(v.encode())
            msg = ExportTraceServiceRequest()
            msg.ParseFromString(data)
            doc = MessageToDict(msg)
            for rs in doc.get("resourceSpans", []):
                res = {a["key"]: a.get("value") for a in rs.get("resource", {}).get("attributes", [])}
                for ss in rs.get("scopeSpans", []):
                    for s in ss.get("spans", []):
                        spans.append({"service": (res.get("service.name") or {}).get("stringValue"),
                                      "resource_keys": sorted(res), "name": s.get("name"),
                                      "span_id": s.get("spanId"), "parent": s.get("parentSpanId"),
                                      "keys": sorted(a["key"] for a in s.get("attributes", []))})
        call = next((s["span_id"] for s in spans if s["name"] == "agent.call"), None)
        cli = [s for s in spans if s["service"] != "reader-canary"]
        report["modes"][r["mode"]] = {
            "error": r["error"], "hits": hits, "authorization_present": auth,
            "gate": r.get("gate"), "tool_results": r["tool_results"],
            "upstream_kinds": [q.get("kind") for q in mine if q["server"] == "upstream"],
            "other_hosts": [q["target"] for q in mine if q["server"] == "forward_proxy"],
            "cli_spans": cli, "under_agent_call": [s["name"] for s in cli if call and s["parent"] == call],
        }
    with open("/out/report.json", "w") as f:
        json.dump(report, f, indent=2)


if __name__ == "__main__":
    main()
