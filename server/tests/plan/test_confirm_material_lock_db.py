# @trace FR-PLAN-20
"""Confirm cannot interleave with a material delete: it reads the material row FOR SHARE under the owner's position
lock "RCPO" before its import (SRV-PLAN FR-PLAN-20, SRV-INGEST FR-INGEST-31). [db]

Deterministic: a test transaction plays the material delete — it holds RCPO and has marked the material deleted —
and the confirm is shown waiting on RCPO in `pg_locks` before the test commits.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from .conftest import PLAN_SHA, Pg, body, client, plan_env

pytestmark = pytest.mark.anyio
RCPO = 0x5243504F


async def test_confirm_waits_for_a_material_delete_holding_rcpo_then_writes_no_course(
    pg: Pg, tmp_path: Path
) -> None:
    from sqlalchemy import text

    from reader.lessons.store import lock_positions

    async with plan_env(pg, tmp_path) as env:
        owner = env.users["ivan"]
        async with client(env) as c:
            sha = (await c.post("/api/plans/preview", json=body())).json()["plan_sha256"]
            async with env.engine.connect() as deleting:
                await lock_positions(deleting, owner)
                await deleting.execute(
                    text("UPDATE materials SET deleted_at = now() WHERE owner_id = :o AND sha256 = :s"),
                    {"o": owner, "s": PLAN_SHA},
                )
                task = asyncio.create_task(
                    c.post("/api/plans/confirm", json=body(plan_sha256=sha, compile_first=False))
                )
                waiting = 0
                for _ in range(200):
                    waiting = await env.scalar(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                        " AND classid::bigint = :ns",
                        ns=RCPO,
                    )
                    if waiting or task.done():
                        break
                    await asyncio.sleep(0.02)
                assert waiting == 1, "the confirm did not wait on the position lock"
                assert not task.done()
                await deleting.commit()
            r = await asyncio.wait_for(task, 10)
        assert (r.status_code, r.json()) == (404, {"error": "not_found"})
        for table in ("courses", "study_plans", "course_plans"):
            assert await env.scalar(f"SELECT count(*) FROM {table}") == 0, table  # noqa: S608
