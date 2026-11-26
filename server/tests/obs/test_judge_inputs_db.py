"""SRV-OBS FR-OBS-38 [db]: `reader.obs.judge_inputs.SINK.copy(conn, request_row, output)` inside the lane's
output transaction — the consent rule, a skipped copy that never aborts the recorded output, line `eval` never
copied, retention by `retention_days`, `purge_pack` and `forget_queries`.

@trace FR-OBS-38 INV-OBS-6
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from sqlalchemy import text

from .conftest import require
from .kl_db import CARDS, JUDGE, Request, engine_for, record_output, user

pytestmark = pytest.mark.anyio
OK_OUTPUT = {"keep": [CARDS[1][0], CARDS[0][0]], "drop": [CARDS[2][0]], "sufficiency": 0.8}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _copy(engine: Any, request: Request, output: dict[str, Any]) -> None:
    ji = require("reader.obs.judge_inputs")
    async with engine.begin() as conn:
        await record_output(conn, request, output)
        await ji.SINK.copy(conn, request, output)


async def _rows(engine: Any) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(text("SELECT * FROM judge_inputs ORDER BY created_at"))).all())


@pytest.mark.parametrize(("role", "consent", "stored"), [("owner", False, True), ("learner", True, True),
                                                          ("learner", False, False)])
async def test_the_copy_stores_the_query_only_with_consent(pg: Any, role: str, consent: bool, stored: bool) -> None:
    engine = await engine_for(pg)
    try:
        caller = await user(engine, role, consent)
        await _copy(engine, Request(caller, "QUERY-CANARY what is BIP-32", CARDS), OK_OUTPUT)
        (row,) = await _rows(engine)
        assert row.query == ("QUERY-CANARY what is BIP-32" if stored else None)
        assert row.labelable is stored
        assert list(row.card_ids) == [c[0] for c in CARDS]
        assert row.output == {"keep": [2, 1], "drop": [3], "sufficiency": 0.8}
        assert row.judge == JUDGE and row.slot == "judge.find"
        assert "card one text" not in repr(tuple(row))  # card text is never stored
    finally:
        await engine.dispose()


async def test_an_invalid_output_skips_the_copy_and_keeps_the_recorded_output(pg: Any, caplog: Any) -> None:
    engine = await engine_for(pg)
    try:
        caller = await user(engine, "owner")
        with caplog.at_level(logging.INFO, logger="reader.obs"):
            await _copy(engine, Request(caller, "q", CARDS), {"keep": ["spn:unknown"], "sufficiency": 0.5})
        assert await _rows(engine) == []
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM judge_outputs"))).scalar_one() == 1
        assert "obs judge_inputs_skipped code=unknown_card" in caplog.text
        assert "spn:unknown" not in caplog.text
    finally:
        await engine.dispose()


async def test_a_database_error_inside_the_copy_never_aborts_the_output_insert(pg: Any) -> None:
    engine = await engine_for(pg)
    try:
        caller = await user(engine, "owner")
        request = Request(caller, "q", CARDS)
        ji = require("reader.obs.judge_inputs")
        async with engine.begin() as conn:
            await record_output(conn, request, OK_OUTPUT)
            await ji.SINK.copy(conn, request, OK_OUTPUT)
            await ji.SINK.copy(conn, request, OK_OUTPUT)  # a second copy hits the unique key inside its savepoint
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM judge_outputs"))).scalar_one() == 1
            assert (await conn.execute(text("SELECT count(*) FROM judge_inputs"))).scalar_one() == 1
    finally:
        await engine.dispose()


async def test_an_eval_line_request_is_never_copied(pg: Any) -> None:
    engine = await engine_for(pg)
    try:
        caller = await user(engine, "owner")
        await _copy(engine, Request(caller, "q", CARDS, line="eval"), OK_OUTPUT)
        assert await _rows(engine) == []
    finally:
        await engine.dispose()


async def test_retention_purge_and_consent_withdrawal(pg: Any) -> None:
    ji = require("reader.obs.judge_inputs")
    engine = await engine_for(pg)
    try:
        caller = await user(engine, "learner", consent=True)
        await _copy(engine, Request(caller, "old", CARDS, pack_ids=("a" * 64,)), OK_OUTPUT)
        await _copy(engine, Request(caller, "new", CARDS, pack_ids=("b" * 64,)), OK_OUTPUT)
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE judge_inputs SET created_at = now() - interval '31 days'"
                                    " WHERE query = 'old'"))
            assert await ji.delete_expired(conn, 30) == 1
        assert [r.query for r in await _rows(engine)] == ["new"]
        async with engine.begin() as conn:
            assert await ji.forget_queries(conn, caller) == 1
        (row,) = await _rows(engine)
        assert row.query is None and row.labelable is False
        async with engine.begin() as conn:
            assert await ji.purge_pack(conn, "c" * 64) == 0
            assert await ji.purge_pack(conn, "b" * 64) == 1
        assert await _rows(engine) == []
    finally:
        await engine.dispose()
