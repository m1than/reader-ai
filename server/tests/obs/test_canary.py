"""SRV-OBS [unit]: the canary for this process — what the OTLP exporter sends holds no book text or secret.

@trace INV-OBS-2 FR-OBS-7 INV-OBS-6 FR-OBS-36

An in-test OTLP/HTTP receiver (a local socket) captures the protobuf bodies the real OTLP exporter sends through the
AllowListExporter; requests, a job run and an agent-call span are driven with canaries in every input.
"""

from __future__ import annotations

import gzip
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from .conftest import ROUTE_TEMPLATES, catalogs, config, require

CANARIES = (
    "bookcanary-91d0",
    "questioncanary-77aa",
    "answercanary-03fe",
    "systemcanary-5c1d",
    "runtokencanary-e4b2",
    "secretcanary-a9c3",
)


class _Receiver(BaseHTTPRequestHandler):
    bodies: list[bytes] = []

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        if self.headers.get("Content-Encoding", "") == "gzip":
            body = gzip.decompress(body)
        _Receiver.bodies.append(body)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def receiver() -> Iterator[str]:
    _Receiver.bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1/traces"
    finally:
        server.shutdown()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_no_canary_reaches_the_exporter(receiver: str) -> None:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    from starlette.applications import Starlette
    from starlette.routing import Route

    setup = require("reader.obs.setup")
    prop = require("reader.obs.propagation")
    obs = require("reader.obs")
    book, question, answer, system, run_token, secret = CANARIES

    async def lesson(request: Any) -> Any:
        await request.body()
        with obs.tracer().start_as_current_span("agent.call") as span:
            span.set_attribute("prompt", f"{system} {book} {question}")
            span.set_attribute("reader.task", run_token)
            span.add_event("reply", {"text": answer})
        raise RuntimeError(secret)

    app = require("reader.obs.asgi").TraceMiddleware(
        Starlette(routes=[Route("/api/lessons/{lesson_id}", lesson, methods=["POST"])]), ROUTE_TEMPLATES
    )
    handle = setup.setup("api", config(), exporter=OTLPSpanExporter(endpoint=receiver), catalogs=catalogs())
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="https://localhost") as c:
            await c.post(
                f"/api/lessons/{book}?q={question}",
                content=f'{{"a": "{answer}"}}',
                headers={"x-token": run_token, "cookie": f"s={secret}"},
            )
        with prop.job_span(
            kind="tutor.answer",
            job_id=9,
            key="tutor.answer:" + "c" * 64,
            payload={"question_id": question, "text": book},
            trace=f"00-{book}",
        ) as job:
            job.outcome("failed", secret)
        handle.force_flush()
    finally:
        handle.shutdown()
    assert _Receiver.bodies, "the exporter sent nothing"
    names: list[str] = []
    for body in _Receiver.bodies:
        for canary in CANARIES:
            assert canary.encode() not in body, canary
        request = ExportTraceServiceRequest()
        request.ParseFromString(body)
        names += [s.name for rs in request.resource_spans for ss in rs.scope_spans for s in ss.spans]
    # FR-OBS-7: the request and the job are both traced, by route template and kind.
    assert "POST /api/lessons/{lesson_id}" in names
    assert "job tutor.answer" in names
    assert "agent.call" in names


def test_fr_obs_7_every_kind_and_route_is_a_listed_span_name() -> None:
    from reader.api.app import ROUTES, WS_ROUTES
    from reader.jobs.catalog import load_kinds

    setup = require("reader.obs.setup")
    attrs = require("reader.obs.attributes")
    cats = setup.default_catalogs(config())
    allow = attrs.AllowList(cats)
    for route in ROUTES:
        if route.path not in ("/api/healthz", "/api/obs/gate"):
            assert allow.span_class(f"{route.method} {route.path}") == "http", route.path
    for ws in WS_ROUTES:
        assert allow.span_class(f"WS {ws.path}") == "ws", ws.path
    kinds = set(load_kinds())
    assert {
        "ingest.import",
        "search.embed",
        "pipeline.stage",
        "pipeline.verify",
        "tutor.answer",
        "obs.retention",
    } <= kinds
    for kind in kinds:
        assert allow.span_class(f"job {kind}") == "job", kind
    # ADR-0039 (FR-OBS-7 as amended): `judge.find` is SRV-AGENT's lane kind (reader.agent.lane), declared in
    # VEC-OBS-1 `job_args` ahead of its merge onto this branch; every other listed kind is in the catalogue.
    assert set(attrs.JOB_ARGS) - {"judge.find"} == kinds - {"judge.find"}
    assert set(attrs.JOB_ARGS) >= kinds


# --- INV-OBS-6 (ADR-0039, code review round 1): judge text confinement, end to end in one process ---

KL_CANARIES = ("QCANARY", "NOTECANARY", "FACTCANARY", "CANDCANARY", "RATIONALECANARY", "CARDTEXTCANARY",
               "QUERYCANARY", "HOSTILECANARY")


@pytest.mark.anyio
async def test_judge_text_never_leaves_its_stores(pg: Any, receiver: str, monkeypatch: Any, caplog: Any) -> None:
    import logging
    from decimal import Decimal

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from sqlalchemy import text

    from . import test_align_db as al
    from . import test_eval_db as ed
    from .kl_db import CARDS, Request, engine_for, record_output, user
    from .test_phase2_unit import Mlflow

    setup = require("reader.obs.setup")
    prop = require("reader.obs.propagation")
    find = require("reader.obs.find")
    ji = require("reader.obs.judge_inputs")
    ev = require("reader.obs.eval")
    labels = require("reader.obs.labels")
    caplog.set_level(logging.DEBUG)
    hostile = "HOSTILECANARY</script>\"'\n"
    handle = setup.setup("worker", config(), exporter=OTLPSpanExporter(endpoint=receiver), catalogs=catalogs())
    engine = await engine_for(pg)
    mlflow_bodies: list[Any] = []
    try:
        owner = await user(engine, "owner")
        # 1. the judge_inputs copy, inside a judge.find job span
        cards = ((CARDS[0][0], "CARDTEXTCANARY one"), (CARDS[1][0], "CARDTEXTCANARY two"))
        request = Request(owner, "QUERYCANARY what is a key", cards)
        output = {"keep": [CARDS[0][0]], "sufficiency": 0.5}
        with prop.job_span(kind="judge.find", job_id=1, key="judge.find:x", payload={"input_sha256": "a" * 64,
                                                                                  "query": "QUERYCANARY"}):
            async with engine.begin() as conn:
                await record_output(conn, request, output)
                await ji.SINK.copy(conn, request, output)
        # 2. one obs.eval record whose judge replies with a rationale (an invalid output) and a valid score
        monkeypatch.setitem(ev.EVAL_SETS["brain-eval-v1"], "sha256",
                            __import__("hashlib").sha256(ed.SET.encode()).hexdigest())
        replies = iter(['{"score": 0.4, "rationale": "RATIONALECANARY"}', '{"score": 0.6}'] * 4)
        ports = ed.Ports()
        ports.reply = ""

        async def judge(slot: str, system: str, user_message: str, run_id: Any, owner_id: Any) -> str:
            return next(replies)

        monkeypatch.setattr(ports, "judge", judge)
        monkeypatch.setattr(ev, "PORTS", ports)
        async with engine.begin() as conn:
            eval_id = await ev.start(conn, owner, "mcp", ed.SLOTS, set_text=ed.SET, cap_usd=Decimal(5),
                                     floors={s: Decimal("0.01") for s in ed.SLOTS})
        with prop.job_span(kind="obs.eval", job_id=2, key="obs.eval:x", payload={"eval_id": str(eval_id)}):
            await ed._record(engine, eval_id, 0)
            await ed._record(engine, eval_id, 1)
        # 3. a judge label with a note, and its mirror and the score posts and the Run, under an in-test MLflow
        found = await ed._keys(engine, "obs.judge_score")
        async with engine.connect() as conn:
            input_id = (await conn.execute(text("SELECT id FROM judge_inputs"))).scalar_one()
        async with engine.begin() as conn:
            await labels.record(conn, owner, labels.LabelBody.model_validate(
                {"target": "judge", "id": f"ji:{input_id}", "ok": False, "note": "NOTECANARY kept a card"}),
                mirror_on=True)
        class Receiving(Mlflow):
            def handler(self, request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/assessments"):
                    self.requests.append(request)
                    return httpx.Response(200, json={"assessment": {"assessment_id": "a-1"}})
                return super().handler(request)

        mlflow = Receiving(dict(ed.RUN_REPLIES))
        monkeypatch.setattr(ev, "tracing_on", lambda: True)
        monkeypatch.setattr(ev, "client_factory", mlflow.client)
        async with mlflow.client() as c:
            for key in found:
                async with engine.begin() as conn:
                    await ev.post_score(conn, key.args["score_id"], c)
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE obs_eval_runs SET deadline = now() - interval '1 second'"))
        await ed._run(engine, eval_id)
        mlflow_bodies = mlflow.bodies()
        # 4. obs.align: propose and score steps with note, query, fact and candidate canaries in their inputs
        seeded = await al._seed(engine, owner, 50, errors=set(range(0, 50, 3)), dataset={4, 5})
        pid = await al._start(engine, owner)
        env = al._env(seeded, [{"instructions": al.GOOD + " CANDCANARY"}, {"instructions": al.BAD_1},
                               {"instructions": al.BAD_2}, {"instructions": al.BAD_3}])
        with prop.job_span(kind="obs.align", job_id=3, key="obs.align:x", payload={"proposal_id": str(pid)}):
            await al._drive(engine, pid, env)
        # 5. find spans with hostile strings as route, facet, topic and card ids
        with find.find(channel="owner", question_type="exact", entitled=True, k=8, budget_tokens=2000,
                       brain_version="b" * 64, policy_version="c" * 64) as top:
            with find.route() as part:
                part.done(route=hostile, role=hostile, form=hostile, topic=hostile)
            with find.select() as part:
                part.done(cards=1, tokens=10, judged_tokens=0, unjudged_tokens=10, budget_tokens=2000,
                          docs=[find.doc(hostile, "d" * 64, 10)])
            top.done(cards=1, tokens=10, judged_tokens=0, unjudged_tokens=10, judge=find.Judge(ran=False,
                                                                                                skip="gate"))
        handle.force_flush()
    finally:
        handle.shutdown()
        await engine.dispose()
    assert _Receiver.bodies, "the exporter sent nothing"
    exported = b"".join(_Receiver.bodies)
    sent = json.dumps(mlflow_bodies)
    for canary in KL_CANARIES:
        assert canary.encode() not in exported, canary
        assert canary not in caplog.text, canary
        assert canary not in sent, canary
    assert "QUERYCANARY" not in sent and any(b and "LLM_JUDGE" in json.dumps(b) for b in mlflow_bodies)
