# @trace FR-CONFIG-26, FR-PIPELINE-6
"""The owner's task budget overrides through `create_run` and a running compile (SRV-CONFIG FR-CONFIG-26, paired).
[db]

`compile_runs.task_budgets` is the run's display snapshot of the effective budgets at its creation; enforcement
reads the live effective value per call (SRV-AGENT's reserve), so an override lowered during a run applies to it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from .conftest import FIXTURES, Pg, load_yaml, pipeline_env

pytestmark = pytest.mark.anyio
DEFAULTS = load_yaml(FIXTURES / "loop-cases.yaml")["defaults"]
UPSERT = (
    "INSERT INTO settings_overrides (key, value, updated_by, version) VALUES (:k, CAST(:v AS jsonb), :u, 1)"
    " ON CONFLICT (key) DO UPDATE SET value = excluded.value, version = settings_overrides.version + 1"
)


async def _override(env: Any, key: str, value: str) -> None:
    from sqlalchemy import text

    async with env.engine.begin() as conn:
        await conn.execute(text(UPSERT), {"k": key, "v": value, "u": env.users["ivan"]})


async def test_a_task_budget_override_is_snapshotted_and_enforced_live(pg: Pg, tmp_path: Path) -> None:
    async with pipeline_env(pg, tmp_path, floors=DEFAULTS["reserves"]) as env:
        await env.import_plan()
        await _override(env, "tasks.draft.budget_usd_per_lesson", "3.5")
        run = await env.compile()
        snapshot = await env.scalar("SELECT task_budgets FROM compile_runs WHERE id = :r", r=run)
        assert snapshot == {"draft": "3.5", "fact_check": "2.0", "review": "4.0"}
        # Lowered below the draft's reserve (0.94) after the run was created: the live value refuses the call.
        await _override(env, "tasks.draft.budget_usd_per_lesson", "0.5")
        for _ in range(4):
            await env.work()
        status = await env.state(run)
        assert status["refusals"] == ["task_budget_exhausted"]
        assert status["task_budgets"] == {"draft": "3.5", "fact_check": "2.0", "review": "4.0"}  # display only
        assert env.cli.requests == []
        assert await env.scalar("SELECT count(*) FROM agent_spend") == 0
