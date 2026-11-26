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
