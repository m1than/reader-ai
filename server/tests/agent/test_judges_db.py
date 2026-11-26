# @trace FR-AGENT-20, INV-AGENT-9
"""[db] Recorded judge outputs: owner-scoped insert-or-ignore, replay, retention, forget, and the trigger."""

from __future__ import annotations

import asyncio
import random
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from .conftest import Pg, need, rows, scalar, world

pytestmark = pytest.mark.anyio
JUDGE = "judge.find@000000000000"
OUT = {"keep": ["spn:a"], "drop": ["spn:b"], "sufficiency": 0.5}


async def _record(engine: Any, owner: uuid.UUID, h: str = "h1", judge: str = JUDGE, **kw: Any) -> bool:
    judges = need("reader.agent.judges")
    async with engine.begin() as conn:
        inserted: bool = await judges.record(conn, owner, h, judge, kw.pop("output", OUT), **kw)
    return inserted


async def test_record_is_insert_or_ignore(pg: Pg, tmp_path: Path) -> None:
    judges = need("reader.agent.judges")
    w = await world(pg, tmp_path)
    owner = w.users["ivan"]
    assert await _record(w.engine, owner, pack_ids=("p1",)) is True
    assert await _record(w.engine, owner, pack_ids=("p1",), output={"keep": [], "drop": [], "sufficiency": 0}) is False
    assert await scalar(w.engine, "SELECT count(*) FROM judge_outputs") == 1
    async with w.engine.connect() as conn:
        assert await judges.recorded(conn, owner, "h1", JUDGE) == OUT
        assert await judges.recorded(conn, owner, "h2", JUDGE) is None
        assert await judges.recorded(conn, owner, "h1", "judge.find@111111111111") is None


@pytest.mark.parametrize(
    "sql", ["UPDATE judge_outputs SET output = '{}'", "DELETE FROM judge_outputs", "TRUNCATE judge_outputs"]
)
async def test_trigger_refuses_change(pg: Pg, tmp_path: Path, sql: str) -> None:
    from sqlalchemy.exc import DBAPIError

    w = await world(pg, tmp_path)
    await _record(w.engine, w.users["ivan"], pack_ids=("p1",))
    with pytest.raises(DBAPIError):
        async with w.engine.begin() as conn:
            await conn.execute(text(sql))
    assert await scalar(w.engine, "SELECT count(*) FROM judge_outputs") == 1


async def test_owners_are_isolated(pg: Pg, tmp_path: Path) -> None:
    judges = need("reader.agent.judges")
    errors = need("reader.agent.errors")
    w = await world(pg, tmp_path)
    a, b = w.users["ivan"], w.users["anna"]
    other = {"keep": ["spn:b"], "drop": ["spn:a"], "sufficiency": 0.9}
    assert await _record(w.engine, a, pack_ids=("p1",), overlay_id=None)
    assert await _record(w.engine, b, pack_ids=("p1",), output=other)
    assert await scalar(w.engine, "SELECT count(*) FROM judge_outputs") == 2
    async with w.engine.connect() as conn:
        assert await judges.recorded(conn, a, "h1", JUDGE) == OUT
        assert await judges.recorded(conn, b, "h1", JUDGE) == other
    assert await judges.ReplayProvider(w.engine, a, "h1").run(JUDGE) == OUT
    assert await judges.ReplayProvider(w.engine, b, "h1").run(JUDGE) == other
    with pytest.raises(errors.AgentError) as miss:
        await judges.ReplayProvider(w.engine, w.users["lea"], "h1").run(JUDGE)
    assert miss.value.code == "replay_miss"
    async with w.engine.begin() as conn:
        assert await judges.forget_pack(conn, a, "p1") == 1
    async with w.engine.connect() as conn:
        assert await judges.recorded(conn, a, "h1", JUDGE) is None
        assert await judges.recorded(conn, b, "h1", JUDGE) == other


async def test_forget_pack_and_overlay(pg: Pg, tmp_path: Path) -> None:
    judges = need("reader.agent.judges")
    w = await world(pg, tmp_path)
    a, b = w.users["ivan"], w.users["anna"]
    await _record(w.engine, a, "h1", pack_ids=("p1", "p2"))
    await _record(w.engine, a, "h2", pack_ids=("p3",))
    await _record(w.engine, a, "h3", "ingest.context_prefix@000000000000", overlay_id="ov1")
    await _record(w.engine, a, "h4", "ingest.context_prefix@000000000000", overlay_id="ov2")
    await _record(w.engine, b, "h5", "ingest.context_prefix@000000000000", overlay_id="ov1")
    async with w.engine.begin() as conn:
        assert await judges.forget_pack(conn, a, "p2") == 1
        assert await judges.forget_overlay(conn, a, "ov1") == 1
    left = {r[0] for r in await rows(w.engine, "SELECT input_sha256 FROM judge_outputs")}
    assert left == {"h2", "h4", "h5"}
    # the delete permission does not outlive the call
    from sqlalchemy.exc import DBAPIError

    async with w.engine.connect() as conn:
        await judges.forget_pack(conn, a, "p3")
        with pytest.raises(DBAPIError):
            await conn.execute(text("DELETE FROM judge_outputs"))
        await conn.rollback()


async def test_expire_deletes_old_judge_find_rows_only(pg: Pg, tmp_path: Path) -> None:
    judges = need("reader.agent.judges")
    w = await world(pg, tmp_path)
    a = w.users["ivan"]
    await _record(w.engine, a, "old", pack_ids=("p1",))
    await _record(w.engine, a, "new", pack_ids=("p1",))
    await _record(w.engine, a, "old-prefix", "ingest.context_prefix@000000000000", overlay_id="ov1")
    async with w.engine.begin() as conn:  # age two rows past the retention (the test owns the trigger bypass)
        await conn.execute(text("SELECT set_config('reader.judge_delete', 'on', true)"))
        await conn.execute(text("ALTER TABLE judge_outputs DISABLE TRIGGER USER"))
        await conn.execute(text(
            "UPDATE judge_outputs SET created_at = now() - interval '31 days' WHERE input_sha256 LIKE 'old%'"))
        await conn.execute(text("ALTER TABLE judge_outputs ENABLE TRIGGER USER"))
    async with w.engine.begin() as conn:
        assert await judges.expire(conn) == 1
    left = {r[0] for r in await rows(w.engine, "SELECT input_sha256 FROM judge_outputs")}
    assert left == {"new", "old-prefix"}


async def test_interleaved_records_keep_one_row_per_owner(pg: Pg, tmp_path: Path) -> None:
    """INV-AGENT-9 over seeded random interleavings of two owners' concurrent records."""
    judges = need("reader.agent.judges")
    w = await world(pg, tmp_path)
    owners = [w.users["ivan"], w.users["anna"]]
    rnd = random.Random(20261126)  # noqa: S311
    for round_ in range(6):
        h = f"h{round_}"
        writers = [rnd.choice(owners) for _ in range(rnd.randint(2, 8))]
        outputs = [{"keep": [f"spn:{i}"], "drop": [], "sufficiency": i / 10} for i in range(len(writers))]
        results = await asyncio.gather(
            *(_record(w.engine, o, h, pack_ids=("p1",), output=out) for o, out in zip(writers, outputs, strict=True))
        )
        for owner in set(writers):
            assert sum(r for r, o in zip(results, writers, strict=True) if o == owner) == 1
            n = await scalar(w.engine, "SELECT count(*) FROM judge_outputs WHERE owner_id = :o AND input_sha256 = :h",
                             o=owner, h=h)
            assert n == 1
            first = next(out for r, o, out in zip(results, writers, outputs, strict=True) if o == owner and r)
            assert await judges.ReplayProvider(w.engine, owner, h).run(JUDGE) == first
