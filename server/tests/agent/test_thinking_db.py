# @trace FR-AGENT-9, VEC-AGENT-1
"""[db] The settle row records the SDK's output_tokens_details.thinking_tokens in `thinking` (agent_0002), not
priced (it is inside `output`), and null when the SDK gives no details."""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from .conftest import FakeCli, FakeProxyClient, Pg, Script, need, rows, sdk_usage, world

pytestmark = pytest.mark.anyio


async def test_settle_records_thinking_tokens(pg: Pg, tmp_path: Path) -> None:
    provider = need("reader.agent.provider")
    w = await world(pg, tmp_path)
    with_details = {**sdk_usage(output=120), "output_tokens_details": {"thinking_tokens": 37}}
    cli = FakeCli([Script(usage=with_details), Script(usage=sdk_usage(output=120))])
    p = provider.AgentProvider(w.engine, w.config, FakeProxyClient(cli), cli=cli, home_root=tmp_path)
    runs = []
    for _ in range(2):
        run = uuid.uuid4()
        runs.append(run)
        result = await p.run(
            provider.StageCall(
                owner_id=w.users["ivan"], handle="ivan", run_id=run, lesson_id=None, stage="tutor", task="fact_check",
                attempt=0, system="S", prompt="P", max_output_tokens=2000, floor_usd=Decimal("0.10"),
                run_cap_usd=Decimal("1.00"),
            )
        )
        assert result.usage.output == 120
    found = [
        (await rows(w.engine, "SELECT output, thinking FROM agent_spend WHERE run_id = :r AND kind = 'settle'", r=r))[0]
        for r in runs
    ]
    assert found == [(120, 37), (120, None)]
