# @trace FR-PLAN-9, FR-PIPELINE-14
"""Code review of e244aa4: both import routes wait for the course lock; owner-only on every plan route; the store's
reads are filtered by owner. [db]"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from pipeline.conftest import plan as manifest_plan

from .conftest import MATERIAL_ID, PLAN_SHA, Env, Pg, body, client, import_plan_material, need, plan_env

pytestmark = pytest.mark.anyio
PLAN_COURSE = f"plan-{PLAN_SHA[:12]}"


async def _waits_for_lock(env: Env, owner: Any, course: str, request: Any) -> Any:
    """Hold lock_course on a second connection; the request must wait (an ungranted advisory lock) until commit."""
    courses = need("reader.pipeline.courses")
    async with env.engine.connect() as conn:
        tx = await conn.begin()
        await courses.lock_course(conn, owner, course)
        task = asyncio.create_task(request)
        waiting = 0
        for _ in range(100):
            waiting = await env.scalar("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
            if waiting:
                break
            await asyncio.sleep(0.05)
        assert waiting, "the request did not wait on the course lock"
        await asyncio.sleep(0.2)
        assert not task.done(), "the request finished while the course lock was held"
        await tx.commit()
    return await task


async def test_confirm_and_the_manifest_route_take_the_course_lock(pg: Pg, tmp_path: Path) -> None:
    async with plan_env(pg, tmp_path) as env:
        owner = env.users["ivan"]
        async with client(env) as c:
            p = (await c.post("/api/plans/preview", json=body())).json()
            r = await _waits_for_lock(
                env, owner, PLAN_COURSE,
                c.post("/api/plans/confirm", json=body(plan_sha256=p["plan_sha256"], compile_first=False)),
            )
            assert r.status_code == 200, r.text
            r = await _waits_for_lock(env, owner, "synthetic", c.post("/api/pipeline/courses", json=manifest_plan()))
            assert r.status_code == 200, r.text


async def test_owner_only_on_confirm_and_get_writes_nothing(pg: Pg, tmp_path: Path) -> None:
    async with plan_env(pg, tmp_path) as env:
        async with client(env) as c:
            sha = (await c.post("/api/plans/preview", json=body())).json()["plan_sha256"]
        async with client(env, "anna") as c:
            r = await c.post("/api/plans/confirm", json=body(plan_sha256=sha, compile_first=True))
            assert (r.status_code, r.json()) == (403, {"error": "owner_only"})
            r = await c.get("/api/plans", params={"material_id": MATERIAL_ID})
            assert (r.status_code, r.json()) == (403, {"error": "owner_only"})
        for table in ("study_plans", "course_plans", "compile_runs"):
            assert await env.scalar(f"SELECT count(*) FROM {table}") == 0, table  # noqa: S608


async def test_store_reads_only_the_owners_rows(pg: Pg, tmp_path: Path) -> None:
    store = need("reader.plan.store")
    async with plan_env(pg, tmp_path) as env:
        ivan, anna = env.users["ivan"], env.users["anna"]
        # the same material sha256 imported by another user, with other page characters
        await import_plan_material(env.engine, anna, page_chars=(7, 7, 7), outline=((1, 1, "Other user's entry"),))
        async with env.engine.connect() as conn:
            mine = await store.page_chars(conn, ivan, PLAN_SHA)
            theirs = await store.page_chars(conn, anna, PLAN_SHA)
            assert mine is not None
            assert theirs is not None
            assert len(mine[1]) == 30
            assert theirs[1:] == ((7, 7, 7), (1, 1, 1))
            assert await store.outline_entries(conn, ivan, theirs[0]) == ()
        async with client(env) as c:
            p = (await c.post("/api/plans/preview", json=body())).json()
            r = await c.post("/api/plans/confirm", json=body(plan_sha256=p["plan_sha256"], compile_first=False))
            assert r.status_code == 200
        async with env.engine.connect() as conn:
            assert await store.stored(conn, ivan, PLAN_COURSE) is not None
            assert await store.stored(conn, anna, PLAN_COURSE) is None
            assert await store.fixed(conn, anna, PLAN_COURSE) == ()
