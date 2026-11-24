"""SRV-OBS [image] (opt-in, READER_TEST_IMAGE_BUILD=1), V.obs-analytics: reader-mlflow's replaced model catalog, the
ledger's cost and the session marker through Rotel into real MLflow, and Jaeger beside it (FR-OBS-12, FR-OBS-19).

@trace FR-OBS-12 FR-OBS-13 FR-OBS-16 FR-OBS-19 NFR-OBS-1

The same harness as test_images.py: per-test names and internal networks, no published port, zero credit. Images are
used by id (never retagged). Our spans go through the real AllowListExporter (server/src mounted read-only into the
client); the CLI-shaped spans go straight to Rotel, as the CLI's do.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from collections.abc import Iterator
from typing import Any

import pytest

from . import test_images as ti
from .conftest import REPO
from .test_images import _backend, _client, sh

pytestmark = ti.pytestmark


@pytest.fixture(scope="module")
def canary_image() -> str:
    return ti.build_id("--build-arg", f"CLAUDE_AGENT_SDK={ti._sdk_pin()}", str(ti.CANARY_DIR))


@pytest.fixture(scope="module")
def mlflow_image() -> str:
    return ti.build_id("-f", str(REPO / "deploy/otel/mlflow.Dockerfile"), str(REPO))


@pytest.fixture
def prefix() -> Iterator[str]:
    name = "reader-test-obs-" + secrets.token_hex(4)
    yield name
    for kind, flag in (("ps", "-aq"), ("network", "ls"), ("volume", "ls")):
        cmd = ["docker", kind, flag] if kind == "ps" else ["docker", kind, "ls", "-q"]
        ids = sh(*cmd, "--filter", f"name={name}", check=False).split()
        if ids:
            rm = ["docker", "rm", "-f"] if kind == "ps" else ["docker", kind, "rm"]
            sh(*rm, *ids, check=False)

MLFLOW_BASE = "ghcr.io/mlflow/mlflow:v3.18.0@sha256:257204c77980b0f3450a26eb6089e487603fcdd4509529eadaf88f90669f5c13"
CATALOG_DIR = "/usr/local/lib/python3.11/site-packages/mlflow/utils/model_catalog"
LESSON = "20000000-0000-4000-8000-0000000000bb"


def _src_client(prefix: str, image: str, net: str, script: str, timeout: float = 600) -> str:
    """A client with the repository's server sources, for the real AllowListExporter."""
    src = REPO / "server" / "src"
    return sh(
        "docker", "run", "--rm", "--network", f"{prefix}-{net}", "-v", f"{src}:/src:ro", "-e", "PYTHONPATH=/src",
        "--entrypoint", "python", image, "-c", script, timeout=timeout,
    )


def test_the_image_replaces_the_bundled_catalog(mlflow_image: str) -> None:  # FR-OBS-12 (c)
    listing = sh("docker", "run", "--rm", "--network", "none", "--entrypoint", "sh", mlflow_image, "-c",
                 f"ls {CATALOG_DIR}/*.json")
    assert listing.split() == [f"{CATALOG_DIR}/anthropic.json"]  # the only *.json
    inside = sh("docker", "run", "--rm", "--network", "none", "--entrypoint", "cat", mlflow_image,
                f"{CATALOG_DIR}/anthropic.json")
    assert inside == (REPO / "deploy/otel/mlflow-catalog/anthropic.json").read_text(encoding="utf-8")
    bundled = sh("docker", "run", "--rm", "--network", "none", "--entrypoint", "python", MLFLOW_BASE, "-I", "-c",
                 f"import json; print('\\n'.join(sorted(json.load(open('{CATALOG_DIR}/anthropic.json'))['models'])))")
    listed = [ln.strip() for ln in (REPO / "deploy/otel/mlflow-bundled-anthropic.txt").read_text().splitlines()
              if ln.strip() and not ln.startswith("#")]
    assert bundled.split() == listed  # a base-image bump fails until the list is regenerated


SEND = """
import json, time, uuid
from opentelemetry import context as ctx, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
import reader.obs as obs
from reader.obs import spans
from reader.obs.attributes import AllowList, Catalogs
from reader.obs.export import AllowListExporter, resource
OTLP = "http://rotel:4318/v1/traces"
cat = Catalogs(frozenset(), frozenset({"tutor.answer"}), frozenset({"draft"}), frozenset({"tutor"}),
               frozenset({"claude-sonnet-5-5"}))
ours = TracerProvider(resource=resource("worker"))
ours.add_span_processor(SimpleSpanProcessor(AllowListExporter(OTLPSpanExporter(endpoint=OTLP), AllowList(cat),
                                                               resource("worker"))))
obs.install(ours)
cli = TracerProvider(resource=Resource({"service.name": "claude-code", "service.version": "2.1.330"}))
cli.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=OTLP)))
t, c = ours.get_tracer("reader"), cli.get_tracer("com.anthropic.claude_code.tracing")
LESSON = %(lesson)r
root = t.start_span("job tutor.answer", context=ctx.Context(), kind=trace.SpanKind.CONSUMER, attributes={
    "reader.job.kind": "tutor.answer", "reader.job.id": 7, "reader.lesson.id": LESSON, "reader.job.outcome": "succeeded"})
with trace.use_span(root):
    spans.session_marker("reader.lesson.id", LESSON)  # exported first: the trace's session (FR-OBS-12 e)
    time.sleep(1.5)
    call = t.start_span("agent.call", attributes={"reader.task": "tutor", "gen_ai.request.model": "claude-sonnet-5-5",
        "gen_ai.operation.name": "invoke_agent", "gen_ai.usage.input_tokens": 38050, "gen_ai.usage.output_tokens": 4000,
        "reader.cost_usd": 0.12, "reader.cost_input_usd": 0.05})
    with trace.use_span(call):
        session = str(uuid.uuid4())
        common = {"session.id": session, "gen_ai.conversation.id": session}
        inter = c.start_span("claude_code.interaction", attributes={**common, "span.type": "claude_code.interaction",
            "gen_ai.provider.name": "anthropic"})
        with trace.use_span(inter):
            for model, provider in (("claude-haiku-4-5", True), ("claude-sonnet-5-5", False)):
                a = {**common, "span.type": "claude_code.llm_request", "model": model, "gen_ai.request.model": model,
                     "input_tokens": 50, "output_tokens": 4000, "cache_read_tokens": 30000, "cache_creation_tokens": 8000}
                if provider:
                    a.update({"gen_ai.provider.name": "anthropic", "gen_ai.system": "anthropic"})
                c.start_span("claude_code.llm_request", attributes=a).end()
        inter.end()
    call.end()
root.end()
ours.shutdown(); cli.shutdown()
print(json.dumps({"trace": format(root.get_span_context().trace_id, "032x"), "ended": time.time()}))
"""

GET = """
import json, sys, time, urllib.request, urllib.error
deadline, out = time.time() + 30, None
while time.time() < deadline:
    try:
        url = "http://mlflow:5000/ajax-api/3.0/mlflow/traces/get?trace_id=tr-" + %(trace)r
        with urllib.request.urlopen(url, timeout=5) as r:
            body = json.load(r)
        spans = body.get("trace", {}).get("spans", [])
        if len(spans) >= %(spans)d:
            out = body; break
    except urllib.error.HTTPError:
        pass
    time.sleep(1)
print(json.dumps({"found": time.time() if out else None, "trace": out}))
"""


def _value(v: Any) -> Any:
    """An OTLP AnyValue as MLflow 3.18's get returns it: a kvlist {values: [{key, value}]} becomes a dict."""
    if isinstance(v, dict) and set(v) == {"values"} and isinstance(v["values"], list):
        return {item["key"]: _value(next(iter(item["value"].values()))) for item in v["values"]}
    if isinstance(v, str) and v[:1] in "{[\"":
        return json.loads(v)
    return v


def _attrs(span: dict[str, Any]) -> dict[str, Any]:
    raw = span.get("attributes") or {}
    if isinstance(raw, list):
        raw = {a["key"]: next(iter(a["value"].values())) for a in raw}
    return {k: _value(v) for k, v in raw.items()}


def test_ledger_cost_session_and_name_through_real_mlflow(prefix: str, canary_image: str, mlflow_image: str) -> None:
    _backend(prefix, mlflow_image)
    env = json.loads(sh("docker", "inspect", "--format", "{{json .Config.Env}}", f"{prefix}-mlflow"))
    assert "MLFLOW_MODEL_CATALOG_URI=" in env  # set and empty: no remote price fetch
    sent = json.loads(_src_client(prefix, canary_image, "otel", SEND % {"lesson": LESSON}))
    got = json.loads(_client(prefix, canary_image, "obs", GET % {"trace": sent["trace"], "spans": 6}))
    assert got["found"] is not None, "the trace never reached MLflow"
    assert got["found"] - sent["ended"] <= 30  # NFR-OBS-1
    info = got["trace"]["trace"]["trace_info"]
    meta, tags = info.get("trace_metadata", {}), info.get("tags", {})
    # The marker reached MLflow first, before the CLI's spans and the root: the session is the lesson, and the
    # root's later batch still set the trace's name, tags and cost (FR-OBS-12 e).
    assert meta["mlflow.trace.session"] == LESSON
    assert tags["mlflow.traceName"] == "job tutor.answer"
    assert tags["reader.lesson.id"] == LESSON
    assert json.loads(meta["mlflow.trace.cost"])["total_cost"] == 0.12
    by_name: dict[str, list[dict[str, Any]]] = {}
    for span in got["trace"]["trace"]["spans"]:
        by_name.setdefault(span["name"], []).append(_attrs(span))
    assert by_name["agent.call"][0]["mlflow.llm.cost"] == {"input_cost": 0.05, "output_cost": 0.07, "total_cost": 0.12}
    llm = by_name["claude_code.llm_request"]
    assert len(llm) == 2  # a model absent from pricing.yaml and a span with no provider key both cost 0
    assert all(s["mlflow.llm.cost"]["total_cost"] == 0.0 for s in llm), llm
    metric = json.loads(_client(prefix, canary_image, "obs", (
        "import json, urllib.request\n"
        "body = json.dumps({'experiment_ids': ['0'], 'view_type': 'SPANS', 'metric_name': 'total_cost',"
        " 'aggregations': [{'aggregation_type': 'SUM'}]}).encode()\n"
        "r = urllib.request.Request('http://mlflow:5000/ajax-api/3.0/mlflow/traces/metrics', data=body,"
        " headers={'Content-Type': 'application/json'})\n"
        "print(urllib.request.urlopen(r, timeout=30).read().decode())\n")))
    total = sum(p["values"]["SUM"] for p in metric.get("data_points", []))
    assert abs(total - 0.12) < 1e-9  # the span cost metric equals the ledger amount: no double count


# --- FR-OBS-19: Jaeger ---

JAEGER_GET = """
import json, sys, time, urllib.request
deadline = time.time() + %(limit)d
while time.time() < deadline:
    try:
        with urllib.request.urlopen("http://jaeger:16686/api/traces/" + %(trace)r, timeout=5) as r:
            data = json.load(r).get("data") or []
            if data and len(data[0].get("spans", [])) >= %(spans)d:
                print(json.dumps({"found": time.time(), "spans": len(data[0]["spans"])})); break
    except Exception:
        pass
    time.sleep(1)
else:
    print(json.dumps({"found": None}))
"""


def _hashes(html: str) -> list[str]:
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", html)
    return [f"'sha256-{base64.b64encode(hashlib.sha256(s.encode()).digest()).decode()}'" for s in scripts]


def test_jaeger_receives_and_its_csp_hashes_hold(prefix: str, canary_image: str, mlflow_image: str) -> None:
    from .test_images import _send

    _backend(prefix, mlflow_image)
    sent = _send(prefix, canary_image)
    got = json.loads(_client(prefix, canary_image, "jaeger-ui", JAEGER_GET % {"trace": sent["trace"], "spans": 2,
                                                                                "limit": 30}))
    assert got["found"] is not None, "the trace never reached Jaeger"
    html = _client(prefix, canary_image, "jaeger-ui",
                   "import urllib.request; print(urllib.request.urlopen('http://jaeger:16686/search').read().decode())")
    caddy = (REPO / "deploy/Caddyfile").read_text(encoding="utf-8")
    site = re.search(r"\nlocalhost:8444 \{\n(.*?)\n\}", caddy, re.S)
    assert site is not None
    configured = re.findall(r"'sha256-[A-Za-z0-9+/=]+'", site.group(1))
    assert sorted(_hashes(html)) == sorted(configured)  # an image bump that changes the inline scripts fails


FILL = """
import json, time, uuid
from opentelemetry import context as ctx, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
p = TracerProvider(resource=Resource({"service.name": "claude-code", "service.version": "2.1.330"}))
p.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint="http://rotel:4318/v1/traces"),
                                        max_queue_size=40000, max_export_batch_size=512))
t = p.get_tracer("cc")
# VEC-OBS-2's full llm_request key set, with plausible values (ids, counts, enums).
KEYS = {"anthropic.custom_base_url": "false", "anthropic.provider": "firstParty", "attempt": 1,
        "cache_creation_tokens": 8000, "cache_read_tokens": 30000, "duration_ms": 900, "effort": "medium",
        "first_content_ms": 300, "gen_ai.provider.name": "anthropic", "gen_ai.request.model": "claude-sonnet-5-5",
        "gen_ai.response.finish_reasons": "end_turn", "gen_ai.system": "anthropic", "input_tokens": 50,
        "llm_request.context": "main", "model": "claude-sonnet-5-5", "output_tokens": 4000, "query_source_safe": "sdk",
        "span.type": "claude_code.llm_request", "speed": "normal", "stop_reason": "end_turn", "success": True,
        "terminal.type": "none", "ttft_ms": 300, "user.id": "u" * 64}
started, last = time.time(), None
for i in range(%(traces)d):
    s = str(uuid.uuid4())
    root = t.start_span("claude_code.interaction", context=ctx.Context(), attributes={**KEYS, "session.id": s,
                        "gen_ai.conversation.id": s})
    with trace.use_span(root):
        for _ in range(%(per)d - 1):
            t.start_span("claude_code.llm_request", attributes={**KEYS, "session.id": s}).end()
    root.end()
    last = format(root.get_span_context().trace_id, "032x")
p.force_flush(); p.shutdown()
print(json.dumps({"last": last, "seconds": time.time() - started}))
"""


def test_jaeger_holds_max_traces_of_72_spans_without_oom(prefix: str, canary_image: str, mlflow_image: str) -> None:
    from ruamel.yaml import YAML

    conf = YAML(typ="safe").load((REPO / "deploy/otel/jaeger.yaml").read_text(encoding="utf-8"))
    max_traces = conf["extensions"]["jaeger_storage"]["backends"]["mem"]["memory"]["max_traces"]
    _backend(prefix, mlflow_image)
    filled = json.loads(_client(prefix, canary_image, "otel", FILL % {"traces": max_traces, "per": 72},
                                timeout=1200))
    flushed = time.time()
    got = json.loads(_client(prefix, canary_image, "jaeger-ui",
                             JAEGER_GET % {"trace": filled["last"], "spans": 72, "limit": 300}, timeout=400))
    stats = sh("docker", "stats", "--no-stream", "--format", "{{.CPUPerc}} {{.MemUsage}}", f"{prefix}-jaeger")
    state = sh("docker", "inspect", "--format", "{{.State.Status}} {{.State.OOMKilled}}", f"{prefix}-jaeger")
    report = {"spans": max_traces * 72, "fill_s": round(filled["seconds"], 1), "jaeger_stats": stats.strip(),
              "last_trace_s": None if got["found"] is None else round(got["found"] - flushed, 1)}
    print(json.dumps(report))  # recorded in the test log: wall time, CPU, memory (ADR-0033)
    assert state.split() == ["running", "false"], state
    assert got["found"] is not None, f"Jaeger never returned the last trace: {report}"
    used = stats.split()[1]
    assert used.endswith("MiB") and float(used[:-3]) < 256, stats


def test_mlflow_delivery_with_jaeger_stopped(prefix: str, canary_image: str, mlflow_image: str) -> None:
    from .test_images import _get, _send

    _backend(prefix, mlflow_image)
    sh("docker", "stop", f"{prefix}-jaeger")
    sent = _send(prefix, canary_image)
    got = _get(prefix, canary_image, sent["trace"])
    assert got["found"] is not None and got["found"] - sent["ended"] <= 30  # NFR-OBS-1 holds without Jaeger


# --- FR-OBS-13, -14, -15 against real MLflow (code review round 1): the shipped code, from the server image ---


@pytest.fixture(scope="module")
def server_image() -> str:
    return ti.build_id("-f", str(REPO / "deploy/images/server.Dockerfile"), str(REPO))


def _server_client(prefix: str, image: str, net: str, script: str, timeout: float = 300) -> str:
    return sh("docker", "run", "--rm", "--network", f"{prefix}-{net}", "--entrypoint", "python", image, "-c", script,
              timeout=timeout)


JOB_ID, JOB_KEY = 4711, "tutor.answer:" + "e" * 64

CHILDREN = """
import json
from opentelemetry import context as ctx, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
import reader.obs as obs
from reader.obs import spans
from reader.obs.attributes import AllowList, Catalogs
from reader.obs.export import AllowListExporter, resource
from reader.obs.ids import JobIdGenerator, derive_trace_id, next_trace_id
cat = Catalogs(frozenset(), frozenset({"tutor.answer"}), frozenset(), frozenset({"tutor"}), frozenset())
ours = TracerProvider(resource=resource("worker"), id_generator=JobIdGenerator())
ours.add_span_processor(SimpleSpanProcessor(AllowListExporter(
    OTLPSpanExporter(endpoint="http://rotel:4318/v1/traces"), AllowList(cat), resource("worker"))))
obs.install(ours)
with next_trace_id(derive_trace_id(%(job)d, %(key)r)):
    root = ours.get_tracer("reader").start_span("job tutor.answer", context=ctx.Context(),
        kind=trace.SpanKind.CONSUMER, attributes={"reader.job.kind": "tutor.answer", "reader.job.id": %(job)d})
with trace.use_span(root):
    with spans.child_span("tutor.retrieve") as r:
        r.set({"reader.retrieval.hits": 8, "reader.retrieval.candidates": 40})
    with spans.child_span("tutor.citations") as c:
        c.set({"reader.citations.total": 4, "reader.citations.valid": 3})
root.end()
ours.shutdown()
print(json.dumps({"trace": format(root.get_span_context().trace_id, "032x")}))
"""

WRITES = """
import asyncio, json, time, urllib.request
import httpx
from reader.obs import feedback, prompts
TRACE = %(trace)r
def get():
    with urllib.request.urlopen("http://mlflow:5000/ajax-api/3.0/mlflow/traces/get?trace_id=tr-" + TRACE,
                                timeout=5) as r:
        return json.load(r)["trace"]
deadline, trace = time.time() + 30, None
while time.time() < deadline:
    try:
        trace = get()
        if len(trace.get("spans", [])) >= 3:
            break
    except Exception:
        pass
    time.sleep(1)
async def main():
    args = feedback.FeedbackArgs(job_id=%(job)d, key=%(key)r, name="tutor.citations_valid", value=False,
                                 metadata={"total": 4, "valid": 3})
    async with httpx.AsyncClient(trust_env=False) as client:
        first = await feedback.post(args, client, last=True)
        again = await feedback.post(args, client, last=True)
        v1 = await prompts.register(client, "reader.tutor", "0.1.0")
        v2 = await prompts.register(client, "reader.tutor", "0.1.1")
    return first, again, v1, v2
first, again, v1, v2 = asyncio.run(main())
after = get()
print(json.dumps({"spans": trace["spans"] if trace else [], "first": first, "again": again, "v1": v1, "v2": v2,
                  "assessments": after["trace_info"].get("assessments", [])}))
"""


def test_feedback_prompts_and_child_spans_against_real_mlflow(
    prefix: str, server_image: str, mlflow_image: str
) -> None:
    _backend(prefix, mlflow_image)
    sent = json.loads(_server_client(prefix, server_image, "otel", CHILDREN % {"job": JOB_ID, "key": JOB_KEY}))
    got = json.loads(_server_client(prefix, server_image, "obs",
                                    WRITES % {"trace": sent["trace"], "job": JOB_ID, "key": JOB_KEY}))
    by_name = {s["name"]: _attrs(s) for s in got["spans"]}
    # FR-OBS-13: the child spans reach MLflow with their counts and their derived span type.
    assert by_name["tutor.retrieve"]["reader.retrieval.hits"] == 8
    assert by_name["tutor.retrieve"]["reader.retrieval.candidates"] == 40
    assert by_name["tutor.citations"]["reader.citations.valid"] == 3
    assert by_name["tutor.retrieve"]["mlflow.spanType"] == "RETRIEVER"
    # FR-OBS-14: one assessment from the catalogue, idempotent on a second post.
    assert (got["first"], got["again"]) == (True, False)
    (assessment,) = [a for a in got["assessments"] if a.get("assessment_name") == "tutor.citations_valid"]
    assert assessment["source"]["source_type"] == "CODE"
    assert assessment["source"]["source_id"] == "reader.tutor.citations"
    assert assessment["feedback"]["value"] is False
    assert assessment.get("metadata") == {"total": "4", "valid": "3"}
    assert "rationale" not in assessment
    # FR-OBS-15: registration creates one version and finds it again by sha (another package version, same file).
    assert got["v1"] == got["v2"] == 1
