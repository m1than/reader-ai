# @trace FR-AGENT-23
"""[db] The loop-fit check the compile loop rule reads (ADR-0005 amendment, ADR-0039 decision 22)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from .conftest import CONFIG, FIXTURES, FakeCli, FakeProxyClient, Pg, load_yaml, need, scalar, world

pytestmark = pytest.mark.anyio
D = Decimal
LIMITS = load_yaml(FIXTURES / "cost-vectors.yaml")["loop_limits"]


def _provider(w: Any, tmp: Path) -> Any:
    provider = need("reader.agent.provider")
    cli = FakeCli([])
    return provider.AgentProvider(w.engine, w.config, FakeProxyClient(cli), cli=cli, home_root=tmp)


def _planned(task: str, floor: str) -> Any:
    provider = need("reader.agent.provider")
    return provider.PlannedCall(task=task, prompt_chars=10, max_output_tokens=10, floor_usd=D(floor))


async def _open_reserve(w: Any, run: uuid.UUID, task: str, usd: str) -> None:
    ledger = need("reader.agent.ledger")
    scope = ledger.Scope(w.users["ivan"], run, None, task, task, 0)
    await ledger.reserve(w.engine, scope, "claude-opus-5-5", D(usd), ledger.Caps(D("100"), None, None, None))


async def test_run_cap(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    p, run, owner = _provider(w, tmp_path), uuid.uuid4(), w.users["ivan"]
    await _open_reserve(w, run, "draft", "1.00")  # an open reservation counts at its reserve
    loop = [_planned("draft", "1.00"), _planned("fact_check", "0.50"), _planned("review", "1.50")]
    before = await scalar(w.engine, "SELECT count(*) FROM agent_spend")
    assert await p.loop_fits(owner, run, D("4.00"), loop) is True  # 1.00 + 3.00 = 4.00 exactly
    assert await p.loop_fits(owner, run, D("3.99"), loop) is False  # each call alone fits, the sum does not
    assert await scalar(w.engine, "SELECT count(*) FROM agent_spend") == before  # nothing written


async def test_task_budget(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)  # fact_check budget_usd_per_lesson: 2.0
    p, run, owner = _provider(w, tmp_path), uuid.uuid4(), w.users["ivan"]
    await _open_reserve(w, run, "fact_check", "0.50")
    two = [_planned("fact_check", "0.75"), _planned("fact_check", "0.75")]
    assert await p.loop_fits(owner, run, D("8.00"), two) is True  # 0.50 + 1.50 = 2.00
    three = [*two, _planned("fact_check", "0.10")]
    assert await p.loop_fits(owner, run, D("8.00"), three) is False


async def test_monthly_budget(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path, CONFIG.replace("monthly_usd: 200", "monthly_usd: 5"))
    p, owner = _provider(w, tmp_path), w.users["ivan"]
    await _open_reserve(w, uuid.uuid4(), "draft", "3.00")  # another run, this month
    run = uuid.uuid4()
    loop = [_planned("draft", "1.00"), _planned("review", "1.00")]
    assert await p.loop_fits(owner, run, D("8.00"), loop) is True  # 3 + 2 = 5
    assert await p.loop_fits(owner, run, D("8.00"), [*loop, _planned("review", "0.01")]) is False


async def test_reserve_is_reserve_for(pg: Pg, tmp_path: Path) -> None:
    ledger = need("reader.agent.ledger")
    provider = need("reader.agent.provider")
    w = await world(pg, tmp_path)
    p, run, owner = _provider(w, tmp_path), uuid.uuid4(), w.users["ivan"]
    big = provider.PlannedCall(task="draft", prompt_chars=60034, max_output_tokens=32000, floor_usd=D("0.61"))
    want = ledger.reserve_for("claude-opus-5-5", 60034, 32000, D("0.61"))
    assert await p.loop_fits(owner, run, want, [big]) is True
    assert await p.loop_fits(owner, run, want - D("0.000001"), [big]) is False


@pytest.mark.parametrize("case", LIMITS, ids=lambda c: c["name"])
async def test_loop_fits_limit(pg: Pg, tmp_path: Path, case: dict[str, Any]) -> None:
    """FR-AGENT-23 revision: the first refusing limit in the order run, task, month; None when the loop fits."""
    w = await world(pg, tmp_path, CONFIG.replace("monthly_usd: 200", f"monthly_usd: {case['monthly']}"))
    p, run, owner = _provider(w, tmp_path), uuid.uuid4(), w.users["ivan"]
    for task, usd in case["open"].items():
        await _open_reserve(w, run, task, str(usd))
    if "month_other" in case:
        await _open_reserve(w, uuid.uuid4(), "draft", str(case["month_other"]))
    loop = [_planned(task, str(floor)) for task, floor in case["planned"]]
    cap = D(str(case["run_cap"]))
    assert await p.loop_fits_limit(owner, run, cap, loop) == case["limit"]
    assert await p.loop_fits(owner, run, cap, loop) is (case["limit"] is None)
