"""Lock order against compile runs: races of deletes and cascades with `create_run`, and the rename cost. [db]

@trace INV-LESSON-23 FR-LESSON-26 FR-LESSON-20

Runs SRV-PIPELINE's environment (a real job queue and a fake provider: no credit). Compiles are only created, never
worked, so each iteration's run is closed by hand before the next.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest

from pipeline.conftest import MATERIAL, client, pipeline_env, plan

from .conftest import Pg, need

pytestmark = pytest.mark.anyio
TIMEOUT_S = 10


async def _sql(env: Any, sql: str, **params: Any) -> None:
    from sqlalchemy import text

    async with env.engine.begin() as conn:
        await conn.execute(text(sql), params)


async def _course_of(env: Any, lesson: uuid.UUID) -> uuid.UUID:
    course: uuid.UUID = await env.scalar("SELECT course_id FROM lessons WHERE id = :l", l=lesson)
    return course


async def _positions(env: Any) -> list[int]:
    rows = await env.rows(
        "SELECT position FROM courses WHERE owner_id = :o AND position IS NOT NULL ORDER BY position",
        o=env.users["ivan"],
    )
    return [int(r[0]) for r in rows]


def _second_plan() -> dict[str, Any]:
    raw = plan()
    raw["course"] = {"id": "synthetic-2", "title": "Second course", "material_sha256": MATERIAL}
    return raw


async def test_delete_and_compile_of_one_lesson_race(pg: Pg, tmp_path: Path) -> None:
    async with pipeline_env(pg, tmp_path) as env:
        lesson = await env.import_plan()
        async with client(env) as c:
            for i in range(50):
                compile_r, delete_r = await asyncio.wait_for(
                    asyncio.gather(
                        c.post(f"/api/lessons/{lesson}/compile", json={}),
                        c.request("DELETE", f"/api/lessons/{lesson}", json={}),
                    ),
                    TIMEOUT_S,
                )
                outcome = (compile_r.status_code, delete_r.status_code)
                assert outcome in ((202, 409), (404, 200)), (i, compile_r.text, delete_r.text)
                if outcome == (202, 409):
                    assert delete_r.json()["error"] == "compile_running"
                    assert await env.scalar("SELECT deleted_at FROM lessons WHERE id = :l", l=lesson) is None
                    await _sql(
                        env,
                        "UPDATE compile_runs SET finished_at = now(), cancelled_at = now() WHERE lesson_id = :l"
                        " AND finished_at IS NULL",
                        l=lesson,
                    )
                else:
                    assert await env.scalar(
                        "SELECT count(*) FROM compile_runs WHERE lesson_id = :l AND finished_at IS NULL", l=lesson
                    ) == 0
                    assert (await c.post(f"/api/lessons/{lesson}/restore", json={})).status_code == 200


async def test_material_cascade_and_compile_race(pg: Pg, tmp_path: Path) -> None:
    manage = need("reader.lessons.manage")
    async with pipeline_env(pg, tmp_path) as env:
        first = await env.import_plan()
        courses = need("reader.pipeline.courses")
        async with env.engine.begin() as conn:
            await courses.import_plan(conn, env.users["ivan"], _second_plan())
        second = await env.scalar(
            "SELECT lesson_id FROM course_plans WHERE plan_course_id = 'synthetic-2' AND plan_lesson_id = 'L02'"
        )
        ids = [await _course_of(env, first), await _course_of(env, second)]
        ivan = env.users["ivan"]

        async def cascade() -> str:
            try:
                async with env.engine.begin() as conn:
                    await manage.delete_courses(conn, ivan, ids, by_material="synthetic-1", actor_id=ivan)
            except manage.ManageError as e:
                return str(e.code)
            return "deleted"

        async with client(env) as c:
            for i in range(50):
                cascaded, compiled = await asyncio.wait_for(
                    asyncio.gather(cascade(), c.post(f"/api/lessons/{second}/compile", json={})), TIMEOUT_S
                )
                deleted = await env.scalar(
                    "SELECT count(*) FROM courses WHERE id = ANY(CAST(:ids AS uuid[])) AND deleted_at IS NOT NULL",
                    ids=ids,
                )
                if cascaded == "compile_running":
                    assert compiled.status_code == 202 and deleted == 0, i
                    await _sql(
                        env,
                        "UPDATE compile_runs SET finished_at = now(), cancelled_at = now() WHERE finished_at IS NULL",
                    )
                else:
                    assert (cascaded, compiled.status_code, deleted) == ("deleted", 404, 2), (i, compiled.text)
                    async with env.engine.begin() as conn:
                        restored = await manage.restore_courses_by_material(conn, ivan, "synthetic-1", ivan)
                    assert sorted(restored) == sorted(ids)
                assert await _positions(env) == [1, 2]


async def test_material_cascade_and_course_order_race(pg: Pg, tmp_path: Path) -> None:
    manage = need("reader.lessons.manage")
    store = need("reader.lessons.store")
    async with pipeline_env(pg, tmp_path) as env:
        first = await env.import_plan()
        courses = need("reader.pipeline.courses")
        async with env.engine.begin() as conn:
            await courses.import_plan(conn, env.users["ivan"], _second_plan())
            await store.create_course(conn, env.users["ivan"], "Unsourced", None)
        second = await env.scalar(
            "SELECT lesson_id FROM course_plans WHERE plan_course_id = 'synthetic-2' AND plan_lesson_id = 'L02'"
        )
        ids = [await _course_of(env, first), await _course_of(env, second)]
        ivan = env.users["ivan"]
        async with client(env) as c:
            for i in range(50):
                active = [
                    x["course_id"] for x in (await c.get("/api/courses")).json()["courses"]
                ]

                async def cascade() -> None:
                    async with env.engine.begin() as conn:
                        await manage.delete_courses(conn, ivan, ids, by_material="synthetic-1", actor_id=ivan)

                _, ordered = await asyncio.wait_for(
                    asyncio.gather(cascade(), c.post("/api/courses/order", json={"course_ids": active[::-1]})),
                    TIMEOUT_S,
                )
                assert ordered.status_code in (200, 409), i
                assert await _positions(env) == [1], i
                async with env.engine.begin() as conn:
                    await manage.restore_courses_by_material(conn, ivan, "synthetic-1", ivan)
                assert await _positions(env) == [1, 2, 3], i


async def test_rename_changes_the_draft_input(pg: Pg, tmp_path: Path) -> None:
    runs = need("reader.pipeline.runs")
    async with pipeline_env(pg, tmp_path) as env:
        lesson = await env.import_plan()
        ivan = env.users["ivan"]

        async def draft_input() -> str:
            async with env.engine.connect() as conn:
                loaded = await runs.load_lesson(conn, env.runtime, ivan, lesson)
            value: str = runs.step_input(env.runtime, loaded, [], "draft")
            return value

        before = await draft_input()
        async with client(env) as c:
            r = await c.post(f"/api/lessons/{lesson}", json={"version": 1, "title": "Renamed lesson"})
            assert r.status_code == 200, r.text
        after = await draft_input()
        assert after != before
        assert await env.scalar("SELECT count(*) FROM compile_steps WHERE input_sha256 = :h", h=after) == 0
