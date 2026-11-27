"""SRV-OBS FR-OBS-39 [db]: `start` in the caller's transaction (rows and outbox jobs), one record through stub
ports (find, the eval-line judge, B8 card text), scores and their `obs.judge_score` keys, the cap, the last
record's check, the deadline check that fails stuck records and closes the run, a record of a closed run, sample
mode, and an LLM_JUDGE post made once across a retry.

@trace FR-OBS-39 FR-OBS-14 INV-OBS-6
"""

from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from .conftest import require
from .kl_db import CARDS, Request, engine_for, record_output, user

pytestmark = pytest.mark.anyio
SET = (
    "questions:\n"
    "  - {id: e01, question: What is a hardened key?, question_type: exact, chapter: 3,"
    " expected_facts: [FACT-CANARY derivation uses the private parent key]}\n"
    "  - {id: e02, question: How are wallets organised?, question_type: thematic, chapter: 4}\n"
)
SLOTS = ("judge.eval.relevance", "judge.eval.sufficiency")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def pinned(monkeypatch: Any) -> None:
    ev = require("reader.obs.eval")
    monkeypatch.setitem(ev.EVAL_SETS["brain-eval-v1"], "sha256", hashlib.sha256(SET.encode()).hexdigest())


class Ports:
    """Stub ports: a fixed find, a judge that records what it was sent, B8 card text by an entitled set."""

    def __init__(self, reply: str = '{"score": 0.7}', cap_after: int | None = None) -> None:
        self.reply, self.cap_after, self.calls = reply, cap_after, []  # type: list[dict[str, Any]]

    async def find(self, conn: Any, owner: uuid.UUID, question: str, profile: str) -> Any:
        ev = require("reader.obs.eval")
        return ev.FindResult(card_ids=[c[0] for c in CARDS], pack_ids=["p" * 64], tokens=900, judged_tokens=600,
                             unjudged_tokens=300, judge_ran=True, judge_skip=None, gate="not_separated",
                             find_ms=120)

    async def cards(self, conn: Any, owner: uuid.UUID, card_ids: list[str]) -> list[tuple[str, str | None]]:
        return [(c, None if c == CARDS[2][0] else f"text of {c}") for c in card_ids]  # card 3: not entitled

    async def judge(self, slot: str, system: str, user_message: str, run_id: uuid.UUID, owner: uuid.UUID) -> str:
        ev = require("reader.obs.eval")
        if self.cap_after is not None and len(self.calls) >= self.cap_after:
            raise ev.EvalCapReached()
        self.calls.append({"slot": slot, "system": system, "user": user_message, "run_id": run_id})
        return self.reply

    def model(self, slot: str) -> str:
        return "claude-opus-5-5"

    def versions(self) -> tuple[str | None, str | None, str | None]:
        return "b" * 64, "c" * 64, "d" * 12


class Ctx:
    def __init__(self, job_id: int, key: str) -> None:
        self.job_id, self.key, self.attempts = job_id, key, 0


async def _start(engine: Any, owner: uuid.UUID, cap: str = "5") -> uuid.UUID:
    ev = require("reader.obs.eval")
    async with engine.begin() as conn:
        return await ev.start(conn, owner, "mcp", SLOTS, set_text=SET, cap_usd=Decimal(cap),
                              floors={s: Decimal("0.01") for s in SLOTS})


async def _keys(engine: Any, kind: str) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(text("SELECT key, args FROM job_keys WHERE kind = :k ORDER BY key"),
                                        {"k": kind})).all())


async def _record(engine: Any, eval_id: uuid.UUID, n: int) -> None:
    ev = require("reader.obs.eval")
    async with engine.begin() as conn:
        await ev.handle_record(ev.EvalArgs(eval_id=str(eval_id), record_no=n), conn, Ctx(100 + n, f"obs.eval:{n}"))


async def test_start_writes_rows_and_outbox_jobs_in_one_transaction(pg: Any, pinned: None) -> None:
    engine = await engine_for(pg)
    try:
        owner = await user(engine, "owner")
        eval_id = await _start(engine, owner)
        async with engine.connect() as conn:
            run = (await conn.execute(text("SELECT * FROM obs_eval_runs WHERE id = :i"), {"i": eval_id})).one()
            recs = (await conn.execute(text("SELECT record_id, state FROM obs_eval_records ORDER BY record_id"))).all()
        assert (run.mode, run.caller, run.records, run.state) == ("dataset", "none", 2, "queued")
        assert [(r.record_id, r.state) for r in recs] == [("e01", "queued"), ("e02", "queued")]
        assert len(await _keys(engine, "obs.eval")) == 2
        (check,) = await _keys(engine, "obs.eval_run")
        assert check.args == {"eval_id": str(eval_id), "check": 1}
        for kind in ("obs.eval", "obs.eval_run"):
            assert "FACT-CANARY" not in json.dumps([k.args for k in await _keys(engine, kind)])
    finally:
        await engine.dispose()


async def test_records_scores_and_the_last_record_check(pg: Any, pinned: None, monkeypatch: Any) -> None:
    ev = require("reader.obs.eval")
    engine = await engine_for(pg)
    try:
        owner = await user(engine, "owner")
        ports = Ports()
        monkeypatch.setattr(ev, "PORTS", ports)
        eval_id = await _start(engine, owner)
        await _record(engine, eval_id, 0)
        assert await _keys(engine, "obs.eval_run") != [] and len(await _keys(engine, "obs.eval_run")) == 1
        await _record(engine, eval_id, 1)
        checks = [k.args["check"] for k in await _keys(engine, "obs.eval_run")]
        assert sorted(checks) == [0, 1]  # the last record staged check 0 once
        assert len(ports.calls) == 4 and {c["run_id"] for c in ports.calls} == {eval_id}
        sent = json.dumps(ports.calls, default=str)
        assert "text of " + CARDS[0][0] in sent and "text of " + CARDS[2][0] not in sent  # B8: card 3 not sent
        assert "FACT-CANARY" in ports.calls[1]["user"] or "FACT-CANARY" in ports.calls[0]["user"]
        async with engine.connect() as conn:
            recs = (await conn.execute(text("SELECT * FROM obs_eval_records ORDER BY record_id"))).all()
            scores = (await conn.execute(text("SELECT * FROM obs_eval_scores"))).all()
        assert [r.state for r in recs] == ["done", "done"]
        assert list(recs[0].card_ids) == [c[0] for c in CARDS] and recs[0].skipped_not_entitled == 1
        assert len(scores) == 4 and {s.value for s in scores} == {0.7}
        from reader.obs.ids import derive_trace_id

        assert recs[0].trace_id == f"{derive_trace_id(100, 'obs.eval:0'):032x}"
        assert len(await _keys(engine, "obs.judge_score")) == 4
        async with engine.connect() as conn:
            rows = json.dumps([tuple(map(str, r)) for r in recs] + [tuple(map(str, s)) for s in scores])
        assert "FACT-CANARY" not in rows and "What is a hardened key" not in rows
    finally:
        await engine.dispose()


async def test_an_unparsable_reply_writes_no_score(pg: Any, pinned: None, monkeypatch: Any) -> None:
    ev = require("reader.obs.eval")
    engine = await engine_for(pg)
    try:
        owner = await user(engine, "owner")
        monkeypatch.setattr(ev, "PORTS", Ports(reply='{"score": 0.7, "rationale": "RATIONALE-CANARY"}'))
        eval_id = await _start(engine, owner)
        await _record(engine, eval_id, 0)
        async with engine.connect() as conn:
            rec = (await conn.execute(text("SELECT * FROM obs_eval_records WHERE record_id = 'e01'"))).one()
            assert (await conn.execute(text("SELECT count(*) FROM obs_eval_scores"))).scalar_one() == 0
        assert rec.invalid_outputs == 2 and rec.state == "done"
    finally:
        await engine.dispose()


async def test_the_cap_stops_judge_calls_and_caps_the_run(pg: Any, pinned: None, monkeypatch: Any) -> None:
    ev = require("reader.obs.eval")
    engine = await engine_for(pg)
    try:
        owner = await user(engine, "owner")
        ports = Ports(cap_after=1)
        monkeypatch.setattr(ev, "PORTS", ports)
        eval_id = await _start(engine, owner)
        await _record(engine, eval_id, 0)
        await _record(engine, eval_id, 1)  # the run is capped: no call
        assert len(ports.calls) == 1
        async with engine.connect() as conn:
            states = [r.state for r in (await conn.execute(text(
                "SELECT state FROM obs_eval_records ORDER BY record_id"))).all()]
            run = (await conn.execute(text("SELECT state FROM obs_eval_runs"))).scalar_one()
        assert states == ["capped", "capped"] and run == "capped"
    finally:
        await engine.dispose()


async def test_the_deadline_check_fails_stuck_records_and_closes_the_run(pg: Any, pinned: None,
                                                                         monkeypatch: Any) -> None:
    ev = require("reader.obs.eval")
    engine = await engine_for(pg)
    try:
        owner = await user(engine, "owner")
        ports = Ports()
        monkeypatch.setattr(ev, "PORTS", ports)
        monkeypatch.setattr(ev, "tracing_on", lambda: False)
        eval_id = await _start(engine, owner)
        await _record(engine, eval_id, 0)  # e02's job died: it stays queued
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE obs_eval_runs SET deadline = now() - interval '1 second'"))
        async with engine.connect() as conn:  # the close commits in its own transaction
            await ev.handle_run(ev.RunArgs(eval_id=str(eval_id), check=1), conn, Ctx(200, "obs.eval_run:1"))
        async with engine.connect() as conn:
            states = [r.state for r in (await conn.execute(text(
                "SELECT state FROM obs_eval_records ORDER BY record_id"))).all()]
            run = (await conn.execute(text("SELECT state, finished_at FROM obs_eval_runs"))).one()
        assert states == ["done", "failed"] and run.state == "done" and run.finished_at is not None
        calls = len(ports.calls)
        await _record(engine, eval_id, 1)  # a record of a closed run makes no call
        assert len(ports.calls) == calls
    finally:
        await engine.dispose()


async def test_sample_mode_attaches_scores_to_the_find_trace(pg: Any, monkeypatch: Any) -> None:
    ev = require("reader.obs.eval")
    ji = require("reader.obs.judge_inputs")
    engine = await engine_for(pg)
    try:
        owner = await user(engine, "owner")
        request = Request(owner, "sampled question", CARDS)
        output = {"keep": [CARDS[0][0]], "sufficiency": 0.5}
        async with engine.begin() as conn:
            await record_output(conn, request, output)
            await ji.SINK.copy(conn, request, output)
            row = (await conn.execute(text("SELECT id FROM judge_inputs"))).one()
            await conn.execute(text("UPDATE judge_inputs SET find_trace_id = :t"), {"t": "ef" * 16})
        monkeypatch.setattr(ev, "PORTS", Ports())
        async with engine.begin() as conn:
            eval_id = await ev.start(conn, owner, "mcp", SLOTS, sample_ids=[row.id], cap_usd=Decimal(5),
                                     floors={s: Decimal("0.01") for s in SLOTS})
        await _record(engine, eval_id, 0)
        async with engine.connect() as conn:
            scores = (await conn.execute(text("SELECT judge_input_id, trace_id FROM obs_eval_scores"))).all()
        assert {(str(s.judge_input_id), s.trace_id) for s in scores} == {(str(row.id), "ef" * 16)}
    finally:
        await engine.dispose()


async def test_a_judge_score_posts_once_across_a_retry(pg: Any, pinned: None, monkeypatch: Any) -> None:
    ev = require("reader.obs.eval")
    engine = await engine_for(pg)
    try:
        owner = await user(engine, "owner")
        monkeypatch.setattr(ev, "PORTS", Ports())
        eval_id = await _start(engine, owner)
        await _record(engine, eval_id, 0)
        async with engine.connect() as conn:
            score = (await conn.execute(text("SELECT id, judge FROM obs_eval_scores LIMIT 1"))).one()
        stored: list[dict[str, Any]] = []

        def reply(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={"trace": {"trace_info": {"assessments": stored}}})
            body = json.loads(request.content)["assessment"]
            stored.append({**body, "assessment_id": "a-9"})
            return httpx.Response(200, json={"assessment": {"assessment_id": "a-9"}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply), base_url="http://mlflow:5000") as c:
            for _ in range(2):
                async with engine.begin() as conn:
                    await ev.post_score(conn, str(score.id), c)
        assert len(stored) == 1
        sent = stored[0]
        assert sent["source"] == {"source_type": "LLM_JUDGE", "source_id": score.judge}
        assert sent["metadata"] == {"score_id": str(score.id)} and "rationale" not in sent
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT assessment_id FROM obs_eval_scores WHERE id = :i"),
                                       {"i": score.id})).scalar_one() == "a-9"
    finally:
        await engine.dispose()


# --- obs.eval_run with tracing on (code review round 1): close first, then the Run and the dataset ---

RUN_REPLIES: dict[tuple[str, str], Any] = {
    ("POST", "/api/2.0/mlflow/runs/search"): {"runs": []},
    ("POST", "/api/2.0/mlflow/runs/create"): {"run": {"info": {"run_id": "r1"}}},
    ("POST", "/ajax-api/3.0/mlflow/traces/search"): {"traces": []},
    ("GET", "/api/2.0/mlflow/experiments/get-by-name"): {"experiment": {"experiment_id": "7"}},
    ("POST", "/api/3.0/mlflow/datasets/search"): {"datasets": []},
    ("POST", "/api/3.0/mlflow/datasets/create"): {"dataset": {"dataset_id": "d1"}},
}


async def _closable(engine: Any, monkeypatch: Any) -> uuid.UUID:
    """A two-record run with one record scored and one stuck, its deadline past."""
    ev = require("reader.obs.eval")
    owner = await user(engine, "owner")
    monkeypatch.setattr(ev, "PORTS", Ports())
    eval_id = await _start(engine, owner)
    await _record(engine, eval_id, 0)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE obs_eval_runs SET deadline = now() - interval '1 second'"))
    return eval_id


async def _run(engine: Any, eval_id: uuid.UUID) -> None:
    ev = require("reader.obs.eval")
    async with engine.connect() as conn:
        await ev.handle_run(ev.RunArgs(eval_id=str(eval_id), check=1), conn, Ctx(300, "obs.eval_run:1"))


async def test_the_run_and_dataset_bodies_with_tracing_on(pg: Any, pinned: None, monkeypatch: Any) -> None:
    from .test_phase2_unit import VEC5, Mlflow

    ev = require("reader.obs.eval")
    engine = await engine_for(pg)
    try:
        eval_id = await _closable(engine, monkeypatch)
        mlflow = Mlflow(dict(RUN_REPLIES))
        monkeypatch.setattr(ev, "tracing_on", lambda: True)
        monkeypatch.setattr(ev, "client_factory", mlflow.client)
        await _run(engine, eval_id)
        declared = {(w["method"], w["path"]) for w in VEC5["writes"] if w["kind"] == "obs.eval_run"}
        import re

        for request in mlflow.requests:
            path = re.sub(r"/datasets/[^/]+/records$", "/datasets/{id}/records", request.url.path)
            assert (request.method, path) in declared, (request.method, path)
        sent = json.dumps(mlflow.bodies())
        assert "FACT-CANARY" not in sent and "expected_facts" not in sent
        (records,) = [json.loads(b["records"]) for p, b in zip(mlflow.paths(), mlflow.bodies(), strict=True)
                      if p.endswith("/records")]
        assert records[0]["inputs"] == {"question_id": "e01", "question": "What is a hardened key?",
                                        "question_type": "exact", "chapter": 3}
        assert "expectations" not in records[0]  # no gold pages in the set: none invented
        (made,) = [b for p, b in zip(mlflow.paths(), mlflow.bodies(), strict=True) if p.endswith("/runs/create")]
        assert made["tags"] == [{"key": "reader.kind", "value": "eval"}, {"key": "reader.eval_id",
                                                                         "value": str(eval_id)}]
        # a retry finds the run closed and the Run complete: no second Run is created
        done = {"info": {"run_id": "r1"}, "data": {"params": [], "tags": [{"key": "reader.ends", "value": "1"},
                                                                          {"key": "reader.complete", "value": "true"}]}}
        again = Mlflow({**RUN_REPLIES, ("POST", "/api/2.0/mlflow/runs/search"): {"runs": [done]},
                        ("POST", "/api/3.0/mlflow/datasets/search"): {"datasets": [{"dataset_id": "d1"}]}})
        monkeypatch.setattr(ev, "client_factory", again.client)
        await _run(engine, eval_id)
        assert "POST /api/2.0/mlflow/runs/create" not in again.paths()
        assert "POST /api/3.0/mlflow/datasets/create" not in again.paths()
    finally:
        await engine.dispose()


async def test_a_refused_dataset_input_falls_back_to_both_experiments(pg: Any, pinned: None,
                                                                      monkeypatch: Any) -> None:
    from .test_phase2_unit import Mlflow

    ev = require("reader.obs.eval")
    engine = await engine_for(pg)
    try:
        eval_id = await _closable(engine, monkeypatch)
        calls = {"n": 0}

        def log_inputs(request: httpx.Request) -> tuple[int, dict[str, Any]]:
            calls["n"] += 1
            return (400, {}) if calls["n"] == 1 else (200, {})

        mlflow = Mlflow({**RUN_REPLIES, ("POST", "/api/2.0/mlflow/runs/log-inputs"): log_inputs})
        monkeypatch.setattr(ev, "tracing_on", lambda: True)
        monkeypatch.setattr(ev, "client_factory", mlflow.client)
        await _run(engine, eval_id)
        creates = [b for p, b in zip(mlflow.paths(), mlflow.bodies(), strict=True) if p.endswith("datasets/create")]
        assert [c["experiment_ids"] for c in creates] == [["7"], ["0", "7"]] and calls["n"] == 2
    finally:
        await engine.dispose()


class FallbackWorld:
    """An MLflow that refuses a bench-only dataset as an eval Run input (400) and accepts the fallback one;
    datasets are kept by experiment, the Run's completeness and its logged inputs are remembered, and a list of
    injected failures (`fail[path]`: a status or an exception, used once each) stands for a dying job."""

    def __init__(self) -> None:
        from .test_phase2_unit import Mlflow

        self.datasets: list[dict[str, Any]] = []
        self.complete = False
        self.inputs: list[str] = []  # dataset ids logged as the Run's input
        self.fail: dict[str, list[Any]] = {}

        def injected(path: str) -> Any:
            queue = self.fail.get(path) or []
            if not queue:
                return None
            failure = queue.pop(0)
            if isinstance(failure, BaseException):
                raise failure
            return failure, {}

        def search(request: httpx.Request) -> tuple[int, dict[str, Any]]:
            if (hit := injected("datasets/search")) is not None:
                return hit  # type: ignore[no-any-return]
            wanted = set(json.loads(request.content)["experiment_ids"])
            return 200, {"datasets": [d for d in self.datasets if wanted & set(d["experiment_ids"])]}

        def create(request: httpx.Request) -> tuple[int, dict[str, Any]]:
            body = json.loads(request.content)
            made = {"dataset_id": f"d{len(self.datasets) + 1}", "experiment_ids": body["experiment_ids"]}
            self.datasets.append(made)
            return 200, {"dataset": made}

        def log_inputs(request: httpx.Request) -> tuple[int, dict[str, Any]]:
            if (hit := injected("log-inputs")) is not None:
                return hit  # type: ignore[no-any-return]
            used = json.loads(json.loads(request.content)["datasets"][0]["dataset"]["source"])["dataset_id"]
            ok = any(d["dataset_id"] == used and "0" in d["experiment_ids"] for d in self.datasets)
            if ok:
                self.inputs.append(used)
            return (200, {}) if ok else (400, {})

        def runs_search(request: httpx.Request) -> tuple[int, dict[str, Any]]:
            if not self.complete:
                return 200, {"runs": []}
            tags = [{"key": "reader.complete", "value": "true"}, {"key": "reader.ends", "value": "1"}]
            inputs = {"dataset_inputs": [{"dataset": {"name": "brain-eval-v1", "digest": i}} for i in self.inputs]}
            return 200, {"runs": [{"info": {"run_id": "r1"}, "data": {"params": [], "tags": tags},
                                   "inputs": inputs}]}

        def set_tag(request: httpx.Request) -> tuple[int, dict[str, Any]]:
            if json.loads(request.content).get("key") == "reader.complete":
                self.complete = True
            return 200, {}

        self.mlflow = Mlflow({**RUN_REPLIES, ("POST", "/api/3.0/mlflow/datasets/search"): search,
                              ("POST", "/api/3.0/mlflow/datasets/create"): create,
                              ("POST", "/api/2.0/mlflow/runs/log-inputs"): log_inputs,
                              ("POST", "/api/2.0/mlflow/runs/search"): runs_search,
                              ("POST", "/api/2.0/mlflow/runs/set-tag"): set_tag})


async def test_two_executions_in_the_fallback_world_make_one_fallback_dataset(pg: Any, pinned: None,
                                                                             monkeypatch: Any) -> None:
    """Code review rounds 2 and 3: the job dies after FR-OBS-20's steps (the Run complete, its input not yet
    logged); the next execution logs the input exactly once with one fallback dataset, and the one after writes
    nothing."""
    ev = require("reader.obs.eval")
    engine = await engine_for(pg)
    try:
        eval_id = await _closable(engine, monkeypatch)
        world = FallbackWorld()
        monkeypatch.setattr(ev, "tracing_on", lambda: True)
        monkeypatch.setattr(ev, "client_factory", world.mlflow.client)
        failures = ({"log-inputs": [httpx.ConnectError("down")]}, {"log-inputs": [400, 503]},
                    {"datasets/search": [503]}, {"datasets/search": [httpx.ConnectError("down")]})
        for fail in failures:  # each: a first execution dies after sync, a second recovers, a third is a no-op
            world.datasets.clear()
            world.inputs.clear()
            world.complete = False
            world.fail = {k: list(v) for k, v in fail.items()}
            with pytest.raises((httpx.ConnectError, httpx.HTTPStatusError)):
                await _run(engine, eval_id)
            assert world.complete and world.inputs == []  # the Run is complete, its input missing
            await _run(engine, eval_id)
            fallbacks = [d for d in world.datasets if "0" in d["experiment_ids"]]
            assert len(fallbacks) == 1 and len(world.inputs) == 1, fail
            assert world.inputs == [fallbacks[0]["dataset_id"]]
            creates = [p for p in world.mlflow.paths() if p.endswith("/runs/create")]
            writes_before = len(world.mlflow.writes())
            await _run(engine, eval_id)  # complete with its input: no write at all
            assert len(world.mlflow.writes()) == writes_before and len(world.inputs) == 1
            assert len([p for p in world.mlflow.paths() if p.endswith("/runs/create")]) == len(creates)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("status", [403, 409])
async def test_another_4xx_on_the_dataset_input_raises_without_a_fallback(pg: Any, pinned: None,
                                                                          monkeypatch: Any, status: int) -> None:
    ev = require("reader.obs.eval")
    runs = require("reader.obs.runs")
    engine = await engine_for(pg)
    try:
        eval_id = await _closable(engine, monkeypatch)
        world = FallbackWorld()
        world.fail = {"log-inputs": [status]}
        monkeypatch.setattr(ev, "tracing_on", lambda: True)
        monkeypatch.setattr(ev, "client_factory", world.mlflow.client)
        with pytest.raises(runs.ObsRunsRefused):
            await _run(engine, eval_id)
        assert [d["experiment_ids"] for d in world.datasets] == [["7"]]  # no [0, bench] dataset
    finally:
        await engine.dispose()


async def test_a_5xx_on_the_dataset_input_does_not_fall_back(pg: Any, pinned: None, monkeypatch: Any) -> None:
    ev = require("reader.obs.eval")
    engine = await engine_for(pg)
    try:
        eval_id = await _closable(engine, monkeypatch)
        world = FallbackWorld()
        world.mlflow.replies[("POST", "/api/2.0/mlflow/runs/log-inputs")] = (503, {})
        monkeypatch.setattr(ev, "tracing_on", lambda: True)
        monkeypatch.setattr(ev, "client_factory", world.mlflow.client)
        with pytest.raises(httpx.HTTPStatusError):
            await _run(engine, eval_id)
        assert [d["experiment_ids"] for d in world.datasets] == [["7"]]  # no fallback dataset
    finally:
        await engine.dispose()


async def test_a_provider_failure_is_not_an_invalid_output(pg: Any, pinned: None, monkeypatch: Any) -> None:
    ev = require("reader.obs.eval")
    engine = await engine_for(pg)
    try:
        owner = await user(engine, "owner")
        ports = Ports()

        async def failing(*args: Any) -> str:
            raise RuntimeError("provider down")

        monkeypatch.setattr(ports, "judge", failing)
        monkeypatch.setattr(ev, "PORTS", ports)
        eval_id = await _start(engine, owner)
        await _record(engine, eval_id, 0)
        async with engine.connect() as conn:
            rec = (await conn.execute(text("SELECT * FROM obs_eval_records WHERE record_id = 'e01'"))).one()
            assert (await conn.execute(text("SELECT count(*) FROM obs_eval_scores"))).scalar_one() == 0
        assert rec.invalid_outputs == 0 and rec.state == "done"
    finally:
        await engine.dispose()


async def test_the_sampler_skips_scored_and_queued_inputs(pg: Any, monkeypatch: Any) -> None:
    ev = require("reader.obs.eval")
    ji = require("reader.obs.judge_inputs")
    engine = await engine_for(pg)
    try:
        owner = await user(engine, "owner")
        request = Request(owner, "owner q", CARDS)
        output = {"keep": [CARDS[0][0]], "sufficiency": 0.5}
        async with engine.begin() as conn:
            await record_output(conn, request, output)
            await ji.SINK.copy(conn, request, output)
        monkeypatch.setattr(ev, "SAMPLE_POLICY", ev.SamplePolicy(
            judges=SLOTS, cap_usd=Decimal(5), floors={s: Decimal("0.01") for s in SLOTS}))
        args = ev.SampleArgs(slot="2026-11-26T12:00")
        for _ in range(2):  # the second pass finds the input already queued in a sample run
            async with engine.begin() as conn:
                await ev.handle_sample(args, conn, Ctx(400, "obs.eval_sample:x"))
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM obs_eval_runs"))).scalar_one() == 1
        monkeypatch.setattr(ev, "PORTS", Ports())
        async with engine.connect() as conn:
            eval_id = (await conn.execute(text("SELECT id FROM obs_eval_runs"))).scalar_one()
        await _record(engine, eval_id, 0)  # scored now
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM obs_eval_records"))  # not queued any more, but scored
            await ev.handle_sample(args, conn, Ctx(401, "obs.eval_sample:y"))
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM obs_eval_runs"))).scalar_one() == 1
    finally:
        await engine.dispose()


@pytest.mark.parametrize("status", [400, 503])
async def test_an_mlflow_failure_leaves_the_run_closed(pg: Any, pinned: None, monkeypatch: Any,
                                                       status: int) -> None:
    from .test_phase2_unit import Mlflow

    ev = require("reader.obs.eval")
    runs = require("reader.obs.runs")
    engine = await engine_for(pg)
    try:
        eval_id = await _closable(engine, monkeypatch)
        mlflow = Mlflow({**RUN_REPLIES, ("POST", "/api/2.0/mlflow/runs/create"): (status, {})})
        monkeypatch.setattr(ev, "tracing_on", lambda: True)
        monkeypatch.setattr(ev, "client_factory", mlflow.client)
        with pytest.raises(runs.ObsRunsRefused if status == 400 else httpx.HTTPStatusError):
            await _run(engine, eval_id)
        async with engine.connect() as conn:
            run = (await conn.execute(text("SELECT state, finished_at FROM obs_eval_runs"))).one()
            states = [r.state for r in (await conn.execute(text(
                "SELECT state FROM obs_eval_records ORDER BY record_id"))).all()]
        assert run.state == "done" and run.finished_at is not None and states == ["done", "failed"]
    finally:
        await engine.dispose()


# --- obs.eval_sample ---


async def test_the_sampler(pg: Any, monkeypatch: Any, caplog: Any) -> None:
    import logging

    ev = require("reader.obs.eval")
    ji = require("reader.obs.judge_inputs")
    engine = await engine_for(pg)
    try:
        owner = await user(engine, "owner")
        consenting = await user(engine, "learner", consent=True)
        silent = await user(engine, "learner", consent=False)
        for caller, query in ((owner, "owner q"), (consenting, "consenting q"), (silent, "silent q")):
            request = Request(caller, query, CARDS)
            output = {"keep": [CARDS[0][0]], "sufficiency": 0.5}
            async with engine.begin() as conn:
                await record_output(conn, request, output)
                await ji.SINK.copy(conn, request, output)
        args = ev.SampleArgs(slot="2026-11-26T12:00")

        async def sample() -> None:
            async with engine.begin() as conn:
                await ev.handle_sample(args, conn, Ctx(400, "obs.eval_sample:x"))

        await sample()  # off by default: no judges in the policy
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM obs_eval_runs"))).scalar_one() == 0
        monkeypatch.setattr(ev, "SAMPLE_POLICY", ev.SamplePolicy(judges=SLOTS, cap_usd=None))
        with caplog.at_level(logging.INFO, logger="reader.obs"):
            await sample()
        assert "obs eval_sample_refused code=cap_missing" in caplog.text
        monkeypatch.setattr(ev, "SAMPLE_POLICY", ev.SamplePolicy(
            judges=SLOTS, cap_usd=Decimal(5), floors={s: Decimal("0.01") for s in SLOTS}))
        await sample()
        async with engine.connect() as conn:
            run = (await conn.execute(text("SELECT mode, records FROM obs_eval_runs"))).one()
            queries = {r.query for r in (await conn.execute(text(
                "SELECT ji.query FROM obs_eval_records e JOIN judge_inputs ji ON ji.id = e.judge_input_id"))).all()}
        assert (run.mode, run.records) == ("sample", 2) and queries == {"owner q", "consenting q"}
    finally:
        await engine.dispose()
