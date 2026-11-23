# @trace FR-PIPELINE-14, FR-PIPELINE-15, FR-LESSON-15
"""The pipeline's course lock and study-plan import, run creation under that lock, and SRV-LESSON's removal and
renumbering of unstarted lessons. [db]"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from pipeline.conftest import plan as manifest_plan

from .conftest import MATERIAL_ID, PLAN_SHA, Env, Pg, need, plan_env

pytestmark = pytest.mark.anyio
COURSE = f"plan-{PLAN_SHA[:12]}"
CAP = Decimal("8.00")


def _plan(ranges: list[tuple[int, int]]) -> Any:
    courses = need("reader.pipeline.courses")
    return courses.Plan(
        COURSE,
        MATERIAL_ID,
        PLAN_SHA,
        [courses.PlanLesson(f"p{a}-{b}", f"Lesson {a}", a, b) for a, b in ranges],
    )


async def _import(env: Env, ranges: list[tuple[int, int]]) -> Any:
    courses = need("reader.pipeline.courses")
    async with env.engine.begin() as conn:
        await courses.lock_course(conn, env.users["ivan"], COURSE)
        return await courses.import_course_plan(conn, env.users["ivan"], _plan(ranges), origin="study_plan")


async def test_study_plan_import_removes_unstarted_and_renumbers(pg: Pg, tmp_path: Path) -> None:
    errors = need("reader.pipeline.errors")
    runs = need("reader.pipeline.runs")
    async with plan_env(pg, tmp_path) as env:
        first = await _import(env, [(1, 10), (11, 20), (21, 30)])
        assert (first.created, first.removed) == (3, 0)
        ids = {p: lid for p, lid, _ in first.lessons}
        assert [o for _, _, o in first.lessons] == [1, 2, 3]
        again = await _import(env, [(1, 10), (11, 20), (21, 30)])
        assert again.lessons == first.lessons
        assert (again.created, again.removed) == (0, 0)
        # lesson 2 starts; a re-plan that keeps it re-packs the rest and renumbers in plan order
        await runs.create_run(env.engine, env.jobs, env.users["ivan"], ids["p11-20"], CAP)
        second = await _import(env, [(1, 5), (6, 10), (11, 20), (21, 25), (26, 30)])
        assert (second.created, second.removed) == (4, 2)
        by_id = {p: (lid, o) for p, lid, o in second.lessons}
        assert by_id["p11-20"] == (ids["p11-20"], 3)
        assert [o for _, _, o in second.lessons] == [1, 2, 3, 4, 5]
        assert await env.scalar("SELECT count(*) FROM lessons WHERE id = :l", l=ids["p1-10"]) == 0
        # dropping or moving a started lesson rolls back
        for ranges in ([(1, 30)], [(1, 5), (6, 10), (11, 21), (22, 30)]):
            with pytest.raises(errors.PipelineError) as raised:
                await _import(env, ranges)
            assert raised.value.code == "started_lesson_dropped"
        assert await env.scalar("SELECT count(*) FROM course_plans WHERE plan_course_id = :c", c=COURSE) == 5


async def test_manifest_route_unchanged_and_origins_do_not_mix(pg: Pg, tmp_path: Path) -> None:
    courses = need("reader.pipeline.courses")
    errors = need("reader.pipeline.errors")
    from pipeline.conftest import client

    async with plan_env(pg, tmp_path) as env:
        async with client(env) as c:
            r = await c.post("/api/pipeline/courses", json=manifest_plan())
            assert r.status_code == 200
            assert set(r.json()) == {"course_id", "lessons"}
            assert [set(x) for x in r.json()["lessons"]] == [{"plan_lesson_id", "lesson_id"}] * 2
            again = await c.post("/api/pipeline/courses", json=manifest_plan())
            assert again.json() == r.json()
        origins = await env.rows(
            "SELECT DISTINCT origin FROM course_plans WHERE plan_course_id = 'synthetic'"
        )
        assert origins == [("manifest",)]
        mixed = courses.Plan("synthetic", "x", "ab" * 32, [courses.PlanLesson("L09", "t", 61, 62)])
        async with env.engine.begin() as conn:
            await courses.lock_course(conn, env.users["ivan"], "synthetic")
            with pytest.raises(errors.PipelineError) as raised:
                await courses.import_course_plan(conn, env.users["ivan"], mixed, origin="study_plan")
        assert raised.value.code == "origin_conflict"
        assert courses.COURSE_LOCK_NAMESPACE != need("reader.pipeline.runs").LOCK_NAMESPACE


async def test_create_run_takes_the_course_lock_and_can_require_an_unstarted_course(
    pg: Pg, tmp_path: Path
) -> None:
    courses = need("reader.pipeline.courses")
    runs = need("reader.pipeline.runs")
    errors = need("reader.pipeline.errors")
    async with plan_env(pg, tmp_path) as env:
        owner = env.users["ivan"]
        first = await _import(env, [(1, 10), (11, 20), (21, 30)])
        one, two = first.lessons[0][1], first.lessons[1][1]
        # the lock blocks run creation until the holder commits
        async with env.engine.connect() as conn:
            tx = await conn.begin()
            await courses.lock_course(conn, owner, COURSE)
            task = asyncio.create_task(runs.create_run(env.engine, env.jobs, owner, one, CAP))
            await asyncio.sleep(0.3)
            assert not task.done()
            await tx.commit()
        await task
        with pytest.raises(errors.PipelineError) as raised:
            await runs.create_run(env.engine, env.jobs, owner, two, CAP, require_course_unstarted=True)
        assert raised.value.code == "already_started"
        assert await env.scalar("SELECT count(*) FROM compile_runs") == 1
        # the default keeps FR-PIPELINE-11's behaviour
        await runs.create_run(env.engine, env.jobs, owner, two, CAP)
        assert await env.scalar("SELECT count(*) FROM compile_runs") == 2


async def test_lesson_store_started_delete_and_reorder(pg: Pg, tmp_path: Path) -> None:
    store = need("reader.lessons.store")
    schema = need("reader.lessons.schema")
    runs = need("reader.pipeline.runs")
    async with plan_env(pg, tmp_path) as env:
        owner = env.users["ivan"]
        first = await _import(env, [(1, 10), (11, 20), (21, 30)])
        lessons = [lid for _, lid, _ in first.lessons]
        course = await env.scalar("SELECT course_id FROM lessons WHERE id = :l", l=lessons[0])
        async with env.engine.begin() as conn:
            assert await store.started_lessons(conn, owner, course) == frozenset()
        await runs.create_run(env.engine, env.jobs, owner, lessons[0], CAP)
        async with env.engine.begin() as conn:
            assert await store.started_lessons(conn, owner, course) == frozenset({lessons[0]})
            for lid, code in ((lessons[0], "lesson_started"), (uuid.uuid4(), "unknown_lesson")):
                with pytest.raises(schema.LessonWriteError) as raised:
                    await store.delete_unstarted_lesson(conn, owner, lid)
                assert raised.value.code == code
            with pytest.raises(schema.LessonWriteError) as raised:
                await store.reorder_lessons(conn, owner, course, lessons[:2])
            assert raised.value.code == "lessons_mismatch"
            await store.reorder_lessons(conn, owner, course, list(reversed(lessons)))
        ordinals = await env.rows(
            "SELECT id, ordinal FROM lessons WHERE course_id = :c ORDER BY ordinal", c=course
        )
        assert [r[0] for r in ordinals] == list(reversed(lessons))
        # a course_plans row still present refers to the lesson: it counts as a reference only through the FK
        async with env.engine.begin() as conn:
            with pytest.raises(schema.LessonWriteError) as raised:
                await store.delete_unstarted_lesson(conn, owner, lessons[1])
            assert raised.value.code == "lesson_started"
        # every foreign key to lessons blocks the delete and is not deferrable
        rows = await env.rows(
            "SELECT conrelid::regclass::text, confdeltype::text, condeferrable FROM pg_constraint"
            " WHERE contype = 'f' AND confrelid = 'lessons'::regclass"
        )
        assert rows and all(t in ("r", "a") and not d for _, t, d in rows), rows
        assert {r[0] for r in rows} >= {"course_plans", "compile_runs"}
