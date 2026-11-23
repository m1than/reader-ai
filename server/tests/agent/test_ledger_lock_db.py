# @trace FR-AGENT-9, FR-AGENT-12
"""[db] The per-user advisory lock makes check-then-reserve atomic: concurrent reserves of one owner with room for
exactly one give one reserve row and refusals for the rest (FR-AGENT-9), under the daily scope (FR-AGENT-12) and
under the run cap. Eight callers, so an unlocked check-then-insert lets more than one through."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from .conftest import Pg, need, scalar, world

pytestmark = pytest.mark.anyio
D = Decimal
CALLERS = 8


async def _race(w: Any, scopes: list[Any], caps: Any) -> tuple[int, list[str]]:
    ledger = need("reader.agent.ledger")
    errors = need("reader.agent.errors")

    async def one(scope: Any) -> str:
        try:
            await ledger.reserve(w.engine, scope, "claude-sonnet-5-5", D("0.10"), caps)
        except errors.BudgetRefused as refused:
            return refused.code
        return "reserved"

    results = await asyncio.gather(*(one(s) for s in scopes))
    return results.count("reserved"), sorted(r for r in results if r != "reserved")


def _scope(w: Any, run_id: uuid.UUID) -> Any:
    ledger = need("reader.agent.ledger")
    return ledger.Scope(w.users["ivan"], run_id, None, "tutor", "tutor", 1)


async def test_concurrent_reserves_under_the_daily_scope(pg: Pg, tmp_path: Path) -> None:
    ledger = need("reader.agent.ledger")
    w = await world(pg, tmp_path)
    caps = ledger.Caps(D("1.00"), None, None, None, daily_task_usd=D("0.15"))
    scopes = [_scope(w, uuid.uuid4()) for _ in range(CALLERS)]  # one run each: only the day is shared
    reserved, refusals = await _race(w, scopes, caps)
    assert reserved == 1
    assert refusals == ["daily_budget_exhausted"] * (CALLERS - 1)
    assert await scalar(w.engine, "SELECT count(*) FROM agent_spend WHERE kind = 'reserve'") == 1
    assert await ledger.spent(w.engine, owner_id=w.users["ivan"], task="tutor", day=True) == D("0.10")
    assert await ledger.spent(w.engine, owner_id=w.users["ivan"], task="draft", day=True) == D(0)


async def test_concurrent_reserves_under_the_run_cap(pg: Pg, tmp_path: Path) -> None:
    ledger = need("reader.agent.ledger")
    w = await world(pg, tmp_path)
    caps = ledger.Caps(D("0.15"), None, None, None)
    run = uuid.uuid4()
    reserved, refusals = await _race(w, [_scope(w, run) for _ in range(CALLERS)], caps)
    assert reserved == 1
    assert refusals == ["budget_exhausted"] * (CALLERS - 1)
    assert await scalar(w.engine, "SELECT count(*) FROM agent_spend WHERE run_id = :r", r=run) == 1
