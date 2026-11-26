# @trace FR-AGENT-9, INV-AGENT-4
"""The spend ledger against Postgres: reservations, settlements, voids, run/task/month caps, the warn line, the
append-only trigger and the void CLI (SRV-AGENT FR-AGENT-9, INV-AGENT-4). [db]"""

from __future__ import annotations

import logging
import random
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from .conftest import Pg, need, rows, scalar, world

pytestmark = pytest.mark.anyio
D = Decimal


def _caps(
    run: str = "8.00", task: str | None = "4.00", month: str | None = "200", warn: str | None = "0.8"
) -> Any:
    ledger = need("reader.agent.ledger")
    return ledger.Caps(
        run_cap_usd=D(run),
        task_budget_usd=None if task is None else D(task),
        monthly_usd=None if month is None else D(month),
        warn_at=None if warn is None else D(warn),
    )


def _scope(
    owner: uuid.UUID, run: uuid.UUID, task: str = "draft", stage: str = "draft", attempt: int = 0
) -> Any:
    ledger = need("reader.agent.ledger")
    return ledger.Scope(owner, run, None, stage, task, attempt)


async def test_reserve_settle_and_spent(pg: Pg, tmp_path: Path) -> None:
    ledger = need("reader.agent.ledger")
    w = await world(pg, tmp_path)
    owner, run = w.users["ivan"], uuid.uuid4()
    rid = await ledger.reserve(w.engine, _scope(owner, run), "claude-opus-5-5", D("0.94"), _caps())
    assert await ledger.spent(w.engine, run_id=run) == D("0.94")  # an open reservation counts its reserve
    record = ledger.UpstreamRecord(statuses=(200,), refused=())
    await ledger.settle(w.engine, rid, D("0.6"), D("0.6"), D("0.598"), None, record)
    assert await ledger.spent(w.engine, run_id=run) == D("0.6")
    assert await ledger.spent(w.engine, run_id=run, task="draft") == D("0.6")
    assert await ledger.spent(w.engine, run_id=run, task="review") == D("0")
    assert await ledger.spent(w.engine, owner_id=owner, month=True) == D("0.6")
    kinds = [r[0] for r in await rows(w.engine, "SELECT kind FROM agent_spend ORDER BY id")]
    assert kinds == ["reserve", "settle"]
    upstream = await scalar(w.engine, "SELECT upstream FROM agent_spend WHERE kind = 'settle'")
    assert upstream == {"statuses": [200], "refused": [], "any_2xx": True, "in_flight": 0, "failed": 0}


async def test_refusals_come_before_any_row(pg: Pg, tmp_path: Path) -> None:
    ledger = need("reader.agent.ledger")
    errors = need("reader.agent.errors")
    w = await world(pg, tmp_path)
    owner, run = w.users["ivan"], uuid.uuid4()
    await ledger.reserve(w.engine, _scope(owner, run), "claude-opus-5-5", D("1.3"), _caps(run="2.4"))
    with pytest.raises(errors.BudgetRefused) as refused:
        await ledger.reserve(
            w.engine,
            _scope(owner, run, "review", "adversarial_review"),
            "claude-opus-5-5",
            D("1.14"),
            _caps(run="2.4"),
        )
    assert refused.value.code == "budget_exhausted"
    with pytest.raises(errors.BudgetRefused) as refused:  # task budget: per run (a recompile starts fresh)
        await ledger.reserve(w.engine, _scope(owner, run), "claude-opus-5-5", D("0.8"), _caps(task="2.0"))
    assert refused.value.code == "task_budget_exhausted"
    other_run = uuid.uuid4()
    await ledger.reserve(w.engine, _scope(owner, other_run), "claude-opus-5-5", D("0.8"), _caps(task="2.0"))
    with pytest.raises(errors.BudgetRefused) as refused:
        await ledger.reserve(
            w.engine, _scope(owner, uuid.uuid4()), "claude-opus-5-5", D("0.5"), _caps(month="2.5")
        )
    assert refused.value.code == "monthly_budget_exhausted"
    assert await scalar(w.engine, "SELECT count(*) FROM agent_spend") == 2


async def test_budget_warn_logged_once(pg: Pg, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    ledger = need("reader.agent.ledger")
    w = await world(pg, tmp_path)
    caplog.set_level(logging.WARNING)
    owner = w.users["ivan"]
    for _ in range(3):
        await ledger.reserve(
            w.engine, _scope(owner, uuid.uuid4()), "claude-opus-5-5", D("3"), _caps(month="10")
        )
    warns = [r for r in caplog.records if "budget_warn" in r.getMessage()]
    assert len(warns) == 1  # spent 6 → 9 crosses 0.8 of 10 once


async def test_ledger_is_append_only(pg: Pg, tmp_path: Path) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    ledger = need("reader.agent.ledger")
    w = await world(pg, tmp_path)
    await ledger.reserve(w.engine, _scope(w.users["ivan"], uuid.uuid4()), "claude-opus-5-5", D("1"), _caps())
    for sql in ("UPDATE agent_spend SET usd = 0", "DELETE FROM agent_spend", "TRUNCATE agent_spend"):
        with pytest.raises(DBAPIError):
            async with w.engine.begin() as conn:
                await conn.execute(text(sql))
    assert await scalar(w.engine, "SELECT count(*) FROM agent_spend") == 1


async def test_void_removes_an_unsettled_reservation_only(pg: Pg, tmp_path: Path) -> None:
    ledger = need("reader.agent.ledger")
    errors = need("reader.agent.errors")
    w = await world(pg, tmp_path)
    owner, run = w.users["ivan"], uuid.uuid4()
    crashed = await ledger.reserve(w.engine, _scope(owner, run), "claude-opus-5-5", D("0.94"), _caps())
    settled = await ledger.reserve(w.engine, _scope(owner, run), "claude-opus-5-5", D("0.5"), _caps())
    await ledger.settle(
        w.engine,
        settled,
        D("0.3"),
        D("0.3"),
        D("0.3"),
        None,
        ledger.UpstreamRecord(statuses=(200,), refused=()),
    )
    await ledger.void(w.engine, crashed)
    assert await ledger.spent(w.engine, run_id=run) == D("0.3")
    with pytest.raises(errors.AgentError) as refused:
        await ledger.void(w.engine, settled)
    assert refused.value.code == "void_refused"
    with pytest.raises(errors.AgentError):
        await ledger.void(w.engine, crashed)  # twice


def test_void_cli_usage() -> None:
    need("reader.agent.void")
    result = subprocess.run(
        [sys.executable, "-I", "-m", "reader.agent.void"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "usage" in result.stderr


async def test_spent_never_exceeds_a_cap_by_more_than_one_reserve(pg: Pg, tmp_path: Path) -> None:
    """INV-AGENT-4 over seeded random reserve/settle/void/crash sequences."""
    ledger = need("reader.agent.ledger")
    errors = need("reader.agent.errors")
    w = await world(pg, tmp_path)
    owner = w.users["ivan"]
    rnd = random.Random(20261122)  # noqa: S311
    for _ in range(12):
        run = uuid.uuid4()
        cap = D(str(rnd.choice(["1.0", "2.4", "4.35"])))
        expected_settled = D("0")
        open_reserves: dict[int, D] = {}
        for _step in range(10):
            reserve = D(str(rnd.choice(["0.38", "0.75", "0.94", "1.14"])))
            task = rnd.choice(["draft", "fact_check", "review"])
            try:
                rid = await ledger.reserve(
                    w.engine,
                    _scope(owner, run, task),
                    "claude-opus-5-5",
                    reserve,
                    _caps(run=str(cap), task="2.0", month=None),
                )
            except errors.BudgetRefused:
                continue
            action = rnd.choice(["settle", "settle", "crash", "void", "unknown"])
            if action == "crash":
                open_reserves[rid] = reserve
                continue
            if action == "void":
                await ledger.void(w.engine, rid)
                continue
            sdk = D(str(rnd.choice(["0", "0.2", "0.6", "1.2"])))
            record = (
                None
                if action == "unknown"
                else ledger.UpstreamRecord(
                    statuses=tuple(rnd.choice([[200], [529, 200], [429], [200, 200]])), refused=()
                )
            )
            amount, _ = ledger.settle_amount(reserve, record, sdk)
            await ledger.settle(w.engine, rid, amount, sdk, sdk, None, record)
            expected_settled += amount
        spent = await ledger.spent(w.engine, run_id=run)
        assert spent == expected_settled + sum(open_reserves.values(), D("0"))
        settled_sum = await scalar(
            w.engine,
            "SELECT coalesce(sum(settled_usd), 0) FROM agent_spend WHERE run_id = :r AND kind = 'settle'",
            r=run,
        )
        assert settled_sum == expected_settled
        assert spent <= cap + D("1.2")  # at most one call's settled amount above its reserve (sdk ≤ 1.2)


async def test_line_caps_and_the_tutor_day_hold_over_random_sequences(pg: Pg, tmp_path: Path) -> None:
    """INV-AGENT-4 extended (ADR-0039): index and eval line caps refuse with their own code, tutor-line judge rows
    count in the tutor day, and no scope ends above its cap by more than one call's overshoot."""
    ledger = need("reader.agent.ledger")
    errors = need("reader.agent.errors")
    w = await world(pg, tmp_path)
    owner = w.users["ivan"]
    rnd = random.Random(20261126)  # noqa: S311
    day_cap = D("3.0")
    for round_ in range(8):
        line = rnd.choice(["index", "eval"])
        task = "ingest.context_prefix" if line == "index" else "judge.align"
        code = f"{line}_budget_exhausted"
        run, cap = uuid.uuid4(), D(str(rnd.choice(["0.5", "1.0"])))
        for _step in range(8):
            reserve = D(str(rnd.choice(["0.1", "0.2", "0.3"])))
            caps = ledger.Caps(cap, None, None, None, run_code=code)
            try:
                rid = await ledger.reserve(w.engine, ledger.Scope(owner, run, None, task, task, 1, line=line),
                                           "claude-haiku-5-5", reserve, caps)
            except errors.BudgetRefused as refused:
                assert refused.code == code
                continue
            sdk = D(str(rnd.choice(["0.05", "0.1", "0.4"])))
            record = ledger.UpstreamRecord(statuses=(200,), refused=())
            amount, _ = ledger.settle_amount(reserve, record, sdk)
            await ledger.settle(w.engine, rid, amount, sdk, sdk, None, record)
        assert await ledger.spent(w.engine, run_id=run) <= cap + D("0.4")
        # the tutor day: a tutor call or a tutor-line judge, alternately
        judge = round_ % 2 == 1
        scope = ledger.Scope(owner, uuid.uuid4(), None, "judge.find" if judge else "tutor",
                             "judge.find" if judge else "tutor", 1, line="tutor" if judge else None)
        try:
            rid = await ledger.reserve(w.engine, scope, "claude-haiku-5-5", D("0.5"),
                                       ledger.Caps(D("10"), None, None, None, daily_task_usd=day_cap))
        except errors.BudgetRefused as refused:
            assert refused.code == "daily_budget_exhausted"
            continue
        await ledger.settle(w.engine, rid, D("0.6"), D("0.6"), D("0.6"), None,
                            ledger.UpstreamRecord(statuses=(200,), refused=()))
    tutor_day = await ledger.spent(w.engine, owner_id=owner, task="tutor", day=True)
    assert D("0") < tutor_day <= day_cap + D("0.1")  # 0.6 settled against a 0.5 reserve: 0.1 overshoot per call
    assert await ledger.spent(w.engine, owner_id=owner, line="index") + await ledger.spent(
        w.engine, owner_id=owner, line="eval") + tutor_day == await ledger.spent(w.engine, owner_id=owner, month=True)
