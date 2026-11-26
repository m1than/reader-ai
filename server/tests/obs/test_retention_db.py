"""SRV-OBS [db], ADR-0039: the retention job's Postgres deletes (FR-OBS-11 as amended), on their own committed
transaction before any MLflow request — `judge_inputs` past `retention_days` (FR-OBS-38), alignment rows still
running a day after their last step become `step_lost` with their candidate text cleared (FR-OBS-41), refused and
discarded proposals past `retention_days` go while proposed and exported ones stay (FR-OBS-31 as amended) — and
they stay done when the MLflow delete then fails.

@trace FR-OBS-11 FR-OBS-31 FR-OBS-38 FR-OBS-41
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text

from .conftest import require
from .kl_db import CARDS, Request, engine_for, record_output, user

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _proposal(conn: Any, owner: uuid.UUID, state: str, age: timedelta, at: int) -> uuid.UUID:
    pid = uuid.uuid4()
    running = state == "running"
    await conn.execute(text(
        "INSERT INTO judge_prompt_proposals (id, owner_id, slot, at_labels, base_prompt_sha256, frame_sha256, state,"
        " refusal, candidate_text, candidates_under_test, proposed_text, guard_version, updated_at)"
        " VALUES (:id, :o, 'judge.eval.sufficiency', :at, :b, :f, :s, :r, :c, :u, :p, 'g1', now() - CAST(:age AS interval))"),
        {"id": pid, "o": owner, "at": at, "b": "b" * 64, "f": "f" * 64, "s": state,
         "r": "too_few_labels" if state == "refused" else None,
         "c": "CANDIDATE-CANARY" if running else None, "u": ["CANDIDATE-CANARY"] if running else None,
         "p": "proposed text" if state in ("proposed", "exported") else None, "age": age})
    return pid


async def test_postgres_retention_deletes_and_marks(pg: Any) -> None:
    jobs = require("reader.obs.jobs")
    ji = require("reader.obs.judge_inputs")
    engine = await engine_for(pg)
    try:
        owner = await user(engine, "owner")
        async with engine.begin() as conn:
            for query in ("old", "new"):
                request = Request(owner, query, CARDS)
                output = {"keep": [CARDS[0][0]], "sufficiency": 0.5}
                await record_output(conn, request, output)
                await ji.SINK.copy(conn, request, output)
            await conn.execute(text("UPDATE judge_inputs SET created_at = now() - interval '8 days'"
                                    " WHERE query = 'old'"))
            lost = await _proposal(conn, owner, "running", timedelta(hours=25), 50)
            alive = await _proposal(conn, owner, "running", timedelta(hours=1), 100)
            await _proposal(conn, owner, "refused", timedelta(days=8), 150)
            kept = await _proposal(conn, owner, "proposed", timedelta(days=40), 200)
        from datetime import UTC, datetime

        async with engine.connect() as conn:
            await jobs.postgres_retention(conn, datetime.now(UTC), 7)
        async with engine.connect() as conn:
            assert [r.query for r in (await conn.execute(text("SELECT query FROM judge_inputs"))).all()] == ["new"]
            rows = {r.id: r for r in (await conn.execute(text("SELECT * FROM judge_prompt_proposals"))).all()}
        assert set(rows) == {lost, alive, kept}
        assert (rows[lost].state, rows[lost].refusal) == ("refused", "step_lost")
        assert rows[lost].candidate_text is None and rows[lost].candidates_under_test is None
        assert rows[alive].state == "running"
        assert rows[kept].state == "proposed"
    finally:
        await engine.dispose()
