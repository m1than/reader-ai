# @trace FR-AGENT-19, FR-AGENT-18, FR-AGENT-9, FR-AGENT-6
"""[db] Ledger lines, the line rule and the index and eval caps through the provider (ADR-0039 B7)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from .conftest import CONFIG_JUDGES, FakeCli, FakeProxyClient, Pg, Script, need, rows, scalar, world

pytestmark = pytest.mark.anyio
D = Decimal
CAPS = {"index_usd_per_pack": D("0.30"), "index_usd_per_pair_run": D("0.30"), "eval_usd_per_run": D("0.30")}


def _call(w: Any, task: str, run: uuid.UUID | None = None, **kw: Any) -> Any:
    provider = need("reader.agent.provider")
    base: dict[str, Any] = {
        "owner_id": w.users["ivan"], "handle": "ivan", "run_id": run or uuid.uuid4(), "lesson_id": None,
        "stage": task, "task": task, "attempt": 1, "system": "S", "prompt": "P", "max_output_tokens": 100,
        "floor_usd": D("0.20"), "run_cap_usd": D("8.00"),
    }
    base.update(kw)
    return provider.StageCall(**base)


def _provider(
    w: Any, tmp: Path, n: int = 4, caps: Mapping[str, Decimal | None] | None = None
) -> tuple[Any, FakeCli]:
    provider = need("reader.agent.provider")
    cli = FakeCli([Script(cost=0.01, statuses=(200,)) for _ in range(n)])
    table = CAPS if caps is None else caps
    p = provider.AgentProvider(
        w.engine, w.config, FakeProxyClient(cli), cli=cli, home_root=tmp, line_caps=lambda _config: table
    )
    return p, cli


async def _refused(p: Any, call: Any) -> str:
    errors = need("reader.agent.errors")
    with pytest.raises(errors.AgentError) as refused:
        await p.run(call)
    code: str = refused.value.code
    return code


async def test_stored_line_follows_task_lines(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path, CONFIG_JUDGES)
    p, _ = _provider(w, tmp_path, n=6)
    await p.run(_call(w, "fact_check"))
    await p.run(_call(w, "tutor"))
    await p.run(_call(w, "ingest.context_prefix"))
    for line in ("tutor", "compile", "mcp"):
        await p.run(_call(w, "judge.find", line=line))
    got = [(r[0], r[1], r[2]) for r in await rows(w.engine, "SELECT task, kind, line FROM agent_spend ORDER BY id")]
    want_lines = ["compile", "tutor", "index", "tutor", "compile", "mcp"]
    assert [g[2] for g in got if g[1] == "reserve"] == want_lines
    assert [g[2] for g in got if g[1] == "settle"] == want_lines  # the settle row copies the line


@pytest.mark.parametrize(
    ("task", "line"),
    [("judge.find", None), ("judge.find", "index"), ("draft", "tutor"), ("live", "index"),
     ("ingest.context_prefix", "eval")],
)
async def test_line_invalid_before_any_row(pg: Pg, tmp_path: Path, task: str, line: str | None) -> None:
    w = await world(pg, tmp_path, CONFIG_JUDGES.replace("  tutor: { model: sonnet }\n",
                                                        "  tutor: { model: sonnet }\n  live: { model: sonnet }\n"))
    p, cli = _provider(w, tmp_path)
    assert await _refused(p, _call(w, task, line=line)) == "line_invalid"
    assert await scalar(w.engine, "SELECT count(*) FROM agent_spend") == 0
    assert cli.requests == []


async def test_slot_missing_from_tasks_is_unknown_model(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)  # CONFIG has no judge slots
    p, _ = _provider(w, tmp_path)
    assert await _refused(p, _call(w, "judge.find", line="tutor")) == "unknown_model"
    assert await scalar(w.engine, "SELECT count(*) FROM agent_spend") == 0


@pytest.mark.parametrize(
    ("task", "line", "cap"),
    [("ingest.context_prefix", None, "index_usd_per_pack"), ("judge.link_type", None, "index_usd_per_pair_run"),
     ("judge.align", None, "eval_usd_per_run"), ("judge.find", "eval", "eval_usd_per_run")],
)
async def test_cap_missing(pg: Pg, tmp_path: Path, task: str, line: str | None, cap: str) -> None:
    w = await world(pg, tmp_path, CONFIG_JUDGES)
    p, cli = _provider(w, tmp_path, caps={**CAPS, cap: None})
    assert await _refused(p, _call(w, task, line=line)) == "cap_missing"
    assert await scalar(w.engine, "SELECT count(*) FROM agent_spend") == 0
    assert cli.requests == []


async def test_default_line_caps_are_missing_until_configured(pg: Pg, tmp_path: Path) -> None:
    judges = need("reader.agent.judges")
    w = await world(pg, tmp_path, CONFIG_JUDGES)
    assert dict(judges.line_caps(w.config)) == {
        "index_usd_per_pack": None, "index_usd_per_pair_run": None, "eval_usd_per_run": None}


@pytest.mark.parametrize(
    ("task", "line", "kind", "code"),
    [("ingest.context_prefix", None, "overlay", "index_budget_exhausted"),
     ("judge.link_type", None, "pair_run", "index_budget_exhausted"),
     ("judge.eval.relevance", None, "eval_run", "eval_budget_exhausted"),
     ("judge.find", "eval", "eval_run", "eval_budget_exhausted")],
)
async def test_line_cap_refuses_the_second_call_of_one_scope(
    pg: Pg, tmp_path: Path, task: str, line: str | None, kind: str, code: str
) -> None:
    judges = need("reader.agent.judges")
    w = await world(pg, tmp_path, CONFIG_JUDGES)
    p, _ = _provider(w, tmp_path)
    a, b = w.users["ivan"], w.users["anna"]
    run_a = judges.scope_run_id(a, kind, "version-1")
    await p.run(_call(w, task, run_a, line=line))  # reserve 0.20 ≤ 0.30, settles 0.01
    await p.run(_call(w, task, run_a, line=line, floor_usd=D("0.29")))  # 0.01 + 0.29 ≤ 0.30, settles 0.01
    third = _call(w, task, run_a, line=line, floor_usd=D("0.29"))
    assert await _refused(p, third) == code  # 0.02 + 0.29 > 0.30
    # another owner with the same version id has its own scope and cap
    run_b = judges.scope_run_id(b, kind, "version-1")
    assert run_b != run_a
    await p.run(_call(w, task, run_b, line=line, owner_id=b, handle="anna"))
    if task == "judge.find":  # obs.align's eval-line judge: in memory, nothing recorded
        assert await scalar(w.engine, "SELECT count(*) FROM judge_outputs") == 0


async def test_the_lower_call_cap_still_applies(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path, CONFIG_JUDGES)
    p, _ = _provider(w, tmp_path)
    call = _call(w, "ingest.context_prefix", run_cap_usd=D("0.10"))
    assert await _refused(p, call) == "index_budget_exhausted"  # min(0.10, 0.30) < reserve 0.20


async def test_spent_per_line_and_the_check_constraint(pg: Pg, tmp_path: Path) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    ledger = need("reader.agent.ledger")
    w = await world(pg, tmp_path, CONFIG_JUDGES)
    p, _ = _provider(w, tmp_path)
    await p.run(_call(w, "ingest.context_prefix"))
    await p.run(_call(w, "fact_check"))
    owner = w.users["ivan"]
    assert await ledger.spent(w.engine, owner_id=owner, line="index") == D("0.01")
    assert await ledger.spent(w.engine, owner_id=owner, line="compile") == D("0.01")
    assert await ledger.spent(w.engine, owner_id=owner, line="eval") == D("0")
    with pytest.raises(DBAPIError):
        async with w.engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO agent_spend (owner_id, run_id, stage, task, attempt, kind, model, usd, line)"
                     " VALUES (:o, :r, 's', 't', 1, 'reserve', 'm', 0, 'other')"),
                {"o": owner, "r": uuid.uuid4()},
            )
