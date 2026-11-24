# @trace FR-PLAN-20, FR-PLAN-21, FR-PIPELINE-22
"""Re-plan from a course, plans of deleted and archived courses, tombstones and manual order in a re-plan. [db]"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from .conftest import MATERIAL_ID, PLAN_SHA, Env, Pg, body, client, need, plan_env

pytestmark = pytest.mark.anyio
COURSE = f"plan-{PLAN_SHA[:12]}"


def _plan(ranges: list[tuple[int, int]], course: str = COURSE) -> Any:
    courses = need("reader.pipeline.courses")
    return courses.Plan(
        course, MATERIAL_ID, PLAN_SHA, [courses.PlanLesson(f"p{a}-{b}", f"Lesson {a}", a, b) for a, b in ranges]
    )


async def _import(env: Env, ranges: list[tuple[int, int]], course: str = COURSE) -> dict[str, uuid.UUID]:
    courses = need("reader.pipeline.courses")
    async with env.engine.begin() as conn:
        await courses.lock_course(conn, env.users["ivan"], course)
        result = await courses.import_course_plan(conn, env.users["ivan"], _plan(ranges, course), origin="study_plan")
    return {p: lid for p, lid, _ in result.lessons}


async def _order(env: Env, course_id: uuid.UUID) -> list[uuid.UUID]:
    return [r[0] for r in await env.rows("SELECT id FROM lessons WHERE course_id = :c ORDER BY ordinal", c=course_id)]


async def _course(env: Env, lesson: uuid.UUID) -> uuid.UUID:
    course: uuid.UUID = await env.scalar("SELECT course_id FROM lessons WHERE id = :l", l=lesson)
    return course


async def test_manual_order_survives_a_replan(pg: Pg, tmp_path: Path) -> None:
    async with plan_env(pg, tmp_path) as env:
        ids = await _import(env, [(2, 10), (11, 20), (21, 30)])
        a, b, c3 = ids["p2-10"], ids["p11-20"], ids["p21-30"]
        course = await _course(env, a)
        async with client(env) as c:
            r = await c.post(f"/api/courses/{course}/lessons/order", json={"lesson_ids": [str(c3), str(a), str(b)]})
            assert r.status_code == 200, r.text
        # 0 new lessons: the manual order stays
        await _import(env, [(2, 10), (11, 20), (21, 30)])
        assert await _order(env, course) == [c3, a, b]
        # 1 new lesson, in front of the plan: no visible predecessor, so first
        n1 = (await _import(env, [(1, 1), (2, 10), (11, 20), (21, 30)]))["p1-1"]
        assert await _order(env, course) == [n1, c3, a, b]
        # 3 new lessons after A, and B (between two kept ones) removed
        new = await _import(env, [(1, 1), (2, 10), (12, 12), (13, 13), (14, 14), (21, 30)])
        n12, n13, n14 = new["p12-12"], new["p13-13"], new["p14-14"]
        assert await env.scalar("SELECT count(*) FROM lessons WHERE id = :l", l=b) == 0
        assert await _order(env, course) == [n1, c3, a, n12, n13, n14]
        # a new lesson whose plan predecessor is deleted lands after the nearest visible one
        async with client(env) as c:
            assert (await c.request("DELETE", f"/api/lessons/{n14}", json={})).status_code == 200
        n15 = (await _import(env, [(1, 1), (2, 10), (12, 12), (13, 13), (14, 14), (15, 15), (21, 30)]))["p15-15"]
        assert await _order(env, course) == [n1, c3, a, n12, n13, n15, n14]
        assert await env.scalar("SELECT deleted_at IS NOT NULL FROM lessons WHERE id = :l", l=n14) is True
        assert await env.scalar("SELECT order_source FROM courses WHERE id = :c", c=course) == "manual"


async def test_plan_order_course_and_tombstones(pg: Pg, tmp_path: Path) -> None:
    async with plan_env(pg, tmp_path) as env:
        ids = await _import(env, [(1, 10), (11, 20), (21, 30)], course="plan-other")
        a, b, c3 = ids["p1-10"], ids["p11-20"], ids["p21-30"]
        course = await _course(env, a)
        async with client(env) as c:
            assert (await c.post(f"/api/lessons/{a}/progress", json={"done": True})).status_code == 200  # started
            assert (await c.request("DELETE", f"/api/lessons/{a}", json={})).status_code == 200
            assert (await c.request("DELETE", f"/api/lessons/{b}", json={})).status_code == 200
            assert (await c.post(f"/api/lessons/{c3}/skip", json={})).status_code == 200
        # B (deleted, unstarted) dropped: removed with its tombstone; A (started) kept and still deleted
        new = await _import(env, [(1, 10), (11, 15), (16, 20), (21, 30)], course="plan-other")
        assert await env.scalar("SELECT count(*) FROM lessons WHERE id = :l", l=b) == 0
        assert await env.scalar("SELECT deleted_at IS NOT NULL FROM lessons WHERE id = :l", l=a) is True
        assert await env.scalar("SELECT skipped_at IS NOT NULL FROM lessons WHERE id = :l", l=c3) is True
        # a 'plan' course follows plan order
        assert await _order(env, course) == [a, new["p11-15"], new["p16-20"], c3]
        assert await env.scalar("SELECT order_source FROM courses WHERE id = :c", c=course) == "plan"


async def _confirmed(c: Any, compile_first: bool = False, **kw: Any) -> dict[str, Any]:
    p = (await c.post("/api/plans/preview", json=body(**kw))).json()
    r = await c.post("/api/plans/confirm", json=body(plan_sha256=p["plan_sha256"], compile_first=compile_first, **kw))
    assert r.status_code == 200, r.text
    data: dict[str, Any] = r.json()
    return data


async def test_replan_from_a_course_and_refusals(pg: Pg, tmp_path: Path) -> None:
    async with plan_env(pg, tmp_path) as env:
        async with client(env) as c:
            confirmed = await _confirmed(c)
            course = confirmed["course_id"]
            by_material = (await c.get(f"/api/plans?material_id={MATERIAL_ID}")).json()
            r = await c.get(f"/api/plans?course_id={course}")
            assert (r.status_code, r.json()) == (200, by_material)
            for bad in (str(uuid.uuid4()), "not-a-uuid"):
                assert (await c.get(f"/api/plans?course_id={bad}")).status_code == 404
            store = need("reader.lessons.store")
            async with env.engine.begin() as conn:
                unplanned = await store.create_course(conn, env.users["ivan"], "No study plan", None)
            assert (await c.get(f"/api/plans?course_id={unplanned}")).status_code == 404
            both = await c.get(f"/api/plans?course_id={course}&material_id={MATERIAL_ID}")
            assert both.status_code == 400
            # archived: preview and confirm refuse, the lookup still answers
            assert (await c.post(f"/api/courses/{course}/archive", json={})).status_code == 200
            for path in ("/api/plans/preview", "/api/plans/confirm"):
                r = await c.post(path, json=body(weeks=3, plan_sha256="0" * 64, compile_first=False))
                assert (r.status_code, r.json()) == (409, {"error": "course_archived"}), path
            assert (await c.get(f"/api/plans?course_id={course}")).status_code == 200
            await c.post(f"/api/courses/{course}/unarchive", json={})
            # deleted: every form answers course_deleted with its id and writes nothing
            title = await env.scalar("SELECT title FROM courses WHERE id = :c", c=uuid.UUID(course))
            assert (await c.request("DELETE", f"/api/courses/{course}", json={"confirm": title})).status_code == 200
            before = await env.rows(
                "SELECT (SELECT count(*) FROM courses), (SELECT count(*) FROM course_plans),"
                " (SELECT max(confirmed_at) FROM study_plans)"
            )
            expected = {"error": "course_deleted", "course_id": course}
            for r in (
                await c.get(f"/api/plans?course_id={course}"),
                await c.get(f"/api/plans?material_id={MATERIAL_ID}"),
                await c.post("/api/plans/preview", json=body(weeks=3)),
                await c.post("/api/plans/confirm", json=body(weeks=3, plan_sha256="0" * 64, compile_first=False)),
            ):
                assert (r.status_code, r.json()) == (409, expected)
            assert await env.rows(
                "SELECT (SELECT count(*) FROM courses), (SELECT count(*) FROM course_plans),"
                " (SELECT max(confirmed_at) FROM study_plans)"
            ) == before
        async with client(env, "anna") as c:
            r = await c.get(f"/api/plans?course_id={course}")
            assert (r.status_code, r.json()) == (403, {"error": "owner_only"})


async def test_compile_first_skips_inactive_lessons(pg: Pg, tmp_path: Path) -> None:
    async with plan_env(pg, tmp_path) as env, client(env) as c:
        confirmed = await _confirmed(c)
        lessons = sorted(confirmed["lessons"], key=lambda x: x["ordinal"])
        assert (await c.post(f"/api/lessons/{lessons[0]['lesson_id']}/skip", json={})).status_code == 200
        again = await _confirmed(c, compile_first=True)
        assert "run_id" in again["compile"], again
        started = await env.scalar("SELECT lesson_id FROM compile_runs")
        assert str(started) == lessons[1]["lesson_id"]


async def test_a_deleted_material_is_neither_planned_nor_imported(pg: Pg, tmp_path: Path) -> None:
    """FR-PLAN-20: preview and confirm answer 404 for a deleted material; FR-PIPELINE-22: `material_imported`
    is false for it (merge with the SRV-INGEST slice)."""
    source = need("reader.pipeline.source")
    async with plan_env(pg, tmp_path) as env:
        async with env.engine.begin() as conn:
            assert await source.material_imported(conn, env.users["ivan"], PLAN_SHA) is True
            await conn.exec_driver_sql("UPDATE materials SET deleted_at = now()")
            assert await source.material_imported(conn, env.users["ivan"], PLAN_SHA) is False
        async with client(env) as c:
            r = await c.post("/api/plans/preview", json=body(weeks=3))
            assert (r.status_code, r.json()) == (404, {"error": "not_found"})
            r = await c.post("/api/plans/confirm", json=body(weeks=3, plan_sha256="0" * 64, compile_first=False))
            assert (r.status_code, r.json()) == (404, {"error": "not_found"})
        assert await env.scalar("SELECT count(*) FROM courses") == 0
