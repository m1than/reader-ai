# @trace FR-AGENT-12, FR-AGENT-13
"""SRV-TUTOR amendment: the daily per-user, per-task scope, thinking off, the custom system prompt's reserve, the stop
reason, abort on the first api_retry and the per-call deadline. [db] except the reserve formula."""

from __future__ import annotations

import math
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from .conftest import FakeCli, FakeProxyClient, Pg, Script, need, rows, spec_bounds, world

pytestmark = pytest.mark.anyio
D = Decimal


def _call(w: Any, **kw: Any) -> Any:
    provider = need("reader.agent.provider")
    base: dict[str, Any] = {
        "owner_id": w.users["ivan"],
        "handle": "ivan",
        "run_id": uuid.uuid4(),
        "lesson_id": None,
        "stage": "tutor",
        "task": "fact_check",
        "attempt": 0,
        "system": "S",
        "prompt": "P",
        "max_output_tokens": 2000,
        "floor_usd": D("0.10"),
        "run_cap_usd": D("1.00"),
    }
    base.update(kw)
    return provider.StageCall(**base)


def _provider(w: Any, scripts: list[Script], tmp: Path) -> tuple[Any, FakeCli, FakeProxyClient]:
    provider = need("reader.agent.provider")
    cli = FakeCli(scripts)
    proxy = FakeProxyClient(cli)
    return provider.AgentProvider(w.engine, w.config, proxy, cli=cli, home_root=tmp), cli, proxy


def test_custom_system_prompt_reserve_uses_the_overhead() -> None:
    ledger = need("reader.agent.ledger")
    pricing = need("reader.agent.pricing")
    b = spec_bounds()
    rates = pricing.load_pricing().models["claude-sonnet-5-5"]
    for custom, fixed in ((True, b["cli_overhead_tokens"]), (False, b["cli_system_tokens"])):
        tokens = fixed + math.ceil(20000 / b["chars_per_token_floor"])
        want = (D(tokens) * rates["cache_write_1h"] + D(2000) * rates["output"]) / D(10**6)
        got = ledger.worst_case("claude-sonnet-5-5", 20000, 2000, custom_system=custom)
        assert got == want.quantize(D("0.000001"))


async def test_daily_scope(pg: Pg, tmp_path: Path) -> None:
    errors = need("reader.agent.errors")
    w = await world(pg, tmp_path)
    p, _, _ = _provider(w, [Script(cost=0.06), Script(cost=0.06), Script()], tmp_path)
    await p.run(_call(w, daily_budget_usd=D("0.25")))  # reserve 0.10, settles 0.06
    await p.run(_call(w, daily_budget_usd=D("0.25")))  # 0.06 + 0.10 ≤ 0.25
    with pytest.raises(errors.BudgetRefused) as refused:  # 0.12 + 0.10 ≤ 0.25 … then 0.22 + 0.10 > 0.25
        await p.run(_call(w, daily_budget_usd=D("0.21")))
    assert refused.value.code == "daily_budget_exhausted"
    await p.run(_call(w, task="draft", daily_budget_usd=D("0.21")))  # another task has its own day


async def test_tutor_line_judges_count_in_the_tutor_day(pg: Pg, tmp_path: Path) -> None:
    """FR-AGENT-12 (ADR-0039 revision): a judge.find on line tutor counts in the tutor's daily scope; on line
    compile it does not."""
    from .conftest import CONFIG_JUDGES

    ledger = need("reader.agent.ledger")
    errors = need("reader.agent.errors")
    w = await world(pg, tmp_path, CONFIG_JUDGES)
    p, _, _ = _provider(w, [Script(cost=0.06) for _ in range(4)], tmp_path)
    owner = w.users["ivan"]
    await p.run(_call(w, task="tutor", stage="tutor", daily_budget_usd=D("0.25")))  # 0.06
    await p.run(_call(w, task="judge.find", stage="judge.find", line="tutor", daily_budget_usd=D("0.25")))  # 0.12
    await p.run(_call(w, task="judge.find", stage="judge.find", line="compile"))  # not in the tutor day
    assert await ledger.spent(w.engine, owner_id=owner, task="tutor", day=True) == D("0.12")
    assert await ledger.spent(w.engine, owner_id=owner, task="judge.find", day=True) == D("0")
    with pytest.raises(errors.BudgetRefused) as refused:  # 0.12 + 0.10 > 0.21
        await p.run(_call(w, task="tutor", stage="tutor", daily_budget_usd=D("0.21")))
    assert refused.value.code == "daily_budget_exhausted"
    with pytest.raises(errors.BudgetRefused):  # the judge on the tutor line sees the same day
        await p.run(_call(w, task="judge.find", stage="judge.find", line="tutor", daily_budget_usd=D("0.21")))


async def test_thinking_off_system_prompt_and_stop_reason(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    p, cli, _ = _provider(w, [Script(stop_reason="max_tokens"), Script()], tmp_path)
    result = await p.run(_call(w, thinking_off=True, system="TUTOR SYSTEM"))
    assert result.stop_reason == "max_tokens"
    assert cli.requests[0].env["MAX_THINKING_TOKENS"] == "0"
    assert cli.systems[0] == ("TUTOR SYSTEM", 0o600)
    result = await p.run(_call(w, system=None))
    assert result.stop_reason == "end_turn"
    assert "MAX_THINKING_TOKENS" not in cli.requests[1].env
    assert cli.requests[1].system_file is None
    provider = need("reader.agent.provider")
    assert provider.sdk_options(cli.requests[1]).system_prompt == {"type": "preset", "preset": "claude_code"}
    assert provider.sdk_options(cli.requests[0]).system_prompt == {
        "type": "file",
        "path": str(cli.requests[0].system_file),
    }
    assert "system_prompt" in _set_fields(provider.sdk_options(cli.requests[0]))


def _set_fields(options: Any) -> set[str]:
    import dataclasses

    return {
        f.name for f in dataclasses.fields(options) if getattr(options, f.name) not in (None, [], {}, False)
    }


async def test_abort_on_first_api_retry(pg: Pg, tmp_path: Path) -> None:
    errors = need("reader.agent.errors")
    w = await world(pg, tmp_path)
    p, cli, _ = _provider(w, [Script(api_retries=3, statuses=(429,)), Script(api_retries=2)], tmp_path)
    with pytest.raises(errors.TransientAgentError) as failure:
        await p.run(_call(w, abort_on_retry=True))
    assert failure.value.code == "upstream_retry"
    assert cli.closed == [True]
    settles = await rows(w.engine, "SELECT settled_usd FROM agent_spend WHERE kind = 'settle'")
    assert settles == [(D("0"),)]  # no upstream 2xx
    result = await p.run(_call(w))  # without the option the CLI's own retries are waited out
    assert result.text == "ok"


async def test_deadline_aborts_and_settles(pg: Pg, tmp_path: Path) -> None:
    errors = need("reader.agent.errors")
    w = await world(pg, tmp_path)
    p, cli, _ = _provider(w, [Script(sleep_s=5, statuses=(200,))], tmp_path)
    with pytest.raises(errors.TransientAgentError) as failure:
        await p.run(_call(w, deadline_s=0.2))
    assert failure.value.code == "deadline_exceeded"
    assert cli.closed == [True]
    settles = await rows(w.engine, "SELECT settled_usd FROM agent_spend WHERE kind = 'settle'")
    assert settles == [(D("0.100000"),)]  # a 2xx without an SDK cost: the reserve
