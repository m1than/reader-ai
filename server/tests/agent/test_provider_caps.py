# @trace FR-AGENT-6
"""[unit] A provider without an engine (the freshness agent [image] check, whose ledger is in memory) reads its
caps from reader.yaml alone: no database, so no overrides (SRV-AGENT note, phase 2 code review round 2)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from .conftest import need, write_config

pytestmark = pytest.mark.anyio


async def test_caps_without_an_engine_are_the_files(tmp_path: Path) -> None:
    from reader.config import load_config
    from reader.config.effective import file_settings

    provider = need("reader.agent.provider")
    config = load_config(write_config(tmp_path))
    call = provider.StageCall(
        owner_id=uuid.uuid4(), handle="ivan", run_id=uuid.uuid4(), lesson_id=None, stage="fact_check",
        task="fact_check", attempt=0, system="S", prompt="P", max_output_tokens=100, floor_usd=Decimal("0.25"),
        run_cap_usd=Decimal("8.00"), daily_budget_usd=Decimal("2.00"),
    )
    caps = await provider.AgentProvider(None, config, None)._caps(call)  # type: ignore[arg-type]
    want = file_settings(config)
    assert (caps.task_budget_usd, caps.monthly_usd, caps.warn_at) == (
        want.task_budgets.get("fact_check"), want.monthly_usd, want.warn_at)
    assert (caps.run_cap_usd, caps.daily_task_usd) == (Decimal("8.00"), Decimal("2.00"))


def test_reserve_for_is_the_one_reserve() -> None:
    """FR-AGENT-6 (ADR-0039 revision): max(floor, worst case + searches_max × the search fee)."""
    ledger = need("reader.agent.ledger")
    pricing = need("reader.agent.pricing")
    fee = pricing.load_pricing().web_search_usd
    for model, chars, out, floor, custom, searches in (
        ("claude-sonnet-5-5", 20000, 2000, Decimal("0.01"), True, 0),
        ("claude-haiku-5-5", 7500, 512, Decimal("0.002"), True, 0),
        ("claude-haiku-5-5", 4000, 4000, Decimal("0.01"), True, 5),
        ("claude-opus-5-5", 10, 10, Decimal("0.61"), False, 0),
    ):
        want = max(floor, ledger.worst_case(model, chars, out, custom_system=custom) + searches * fee)
        got = ledger.reserve_for(model, chars, out, floor, custom_system=custom, searches_max=searches)
        assert got == want
        if searches == 0:
            assert got == ledger.reserve_amount(model, chars, out, floor, custom_system=custom)
