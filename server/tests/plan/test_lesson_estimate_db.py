# @trace FR-PLAN-13
"""The per-lesson estimate route for the owner's Compile confirm (WEB-UI FR-UI-15), on a migrated Postgres. [db]

Pins the wiring only (the numbers are test_lesson_estimate.py's vectors): the route answers `lesson_estimate` of
`lesson_chars`, which sums the owner's own spans of the lesson's pages. Read-only; the fake provider is never called.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from .conftest import (
    PAGE_CHARS,
    PLAN_SHA,
    Pg,
    body,
    client,
    import_plan_material,
    need,
    pipeline_env,
    plan_env,
)

pytestmark = pytest.mark.anyio


async def _counts(env: Any) -> tuple[int, ...]:
    return tuple(
        [
            await env.scalar(f"SELECT count(*) FROM {t}")  # noqa: S608 (fixed table names)
            for t in ("compile_runs", "compile_steps", "lessons", "course_plans", "study_plans", "lesson_revisions")
        ]
    )


async def _confirmed(c: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    preview = (await c.post("/api/plans/preview", json=body())).json()
    confirmed = (
        await c.post("/api/plans/confirm", json=body(plan_sha256=preview["plan_sha256"], compile_first=False))
    ).json()
    return preview, confirmed


async def test_route_answers_lesson_estimate_of_the_lessons_own_characters(pg: Pg, tmp_path: Path) -> None:
    estimate = need("reader.plan.estimate")
    store = need("reader.plan.store")
    source = need("reader.pipeline.source")
    async with pipeline_env(pg, tmp_path) as env:
        # A second owner id with a material of the same sha256 and pages but other text lengths, inserted FIRST, so
        # it holds the first rows a query without the owner filter would meet: it must change nothing.
        await import_plan_material(env.engine, env.users["anna"], page_chars=tuple(7777 for _ in PAGE_CHARS))
        await import_plan_material(env.engine, env.users["ivan"])  # as plan_env, after the other owner's
        env.spans.spans = [
            source.Span(f"{PLAN_SHA}:{p}", p, "text", "x" * n) for p, n in enumerate(PAGE_CHARS, start=1) if n
        ]
        async with client(env) as c:
            preview, confirmed = await _confirmed(c)
            by_plan = {x["plan_lesson_id"]: x["lesson_id"] for x in confirmed["lessons"]}
            before = await _counts(env)
            assert hasattr(estimate, "lesson_estimate"), "reader.plan.estimate.lesson_estimate is missing"
            for i, planned in enumerate(preview["lessons"]):
                first, last = planned["pages"]
                chars = sum(PAGE_CHARS[first - 1 : last])
                lesson_id = by_plan[planned["id"]]
                r = await c.get(f"/api/lessons/{lesson_id}/estimate")
                assert r.status_code == 200, r.text
                assert r.json() == {"lesson_id": lesson_id, **estimate.lesson_estimate(chars)}, planned["id"]
                if i == 0:
                    first_lesson = preview["estimate"]["first_lesson"]
                    assert (r.json()["low_usd"], r.json()["high_usd"]) == (
                        first_lesson["low_usd"],
                        first_lesson["high_usd"],
                    )
            assert await _counts(env) == before  # read-only
        assert hasattr(store, "lesson_chars"), "reader.plan.store.lesson_chars is missing"
        lesson = uuid.UUID(confirmed["lessons"][0]["lesson_id"])
        first, last = preview["lessons"][0]["pages"]
        async with env.engine.connect() as conn:
            assert await store.lesson_chars(conn, env.users["ivan"], lesson) == sum(PAGE_CHARS[first - 1 : last])
            assert await store.lesson_chars(conn, env.users["anna"], lesson) is None


async def test_estimate_of_a_manifest_lesson(pg: Pg, tmp_path: Path) -> None:
    from pipeline.conftest import plan as manifest_plan
    from pipeline.conftest import synthetic_spans

    estimate = need("reader.plan.estimate")
    async with plan_env(pg, tmp_path) as env, client(env) as c:
        r = await c.post("/api/pipeline/courses", json=manifest_plan())
        assert r.status_code == 200, r.text
        ids = {x["plan_lesson_id"]: x["lesson_id"] for x in r.json()["lessons"]}
        chars = sum(len(s.text) for s in synthetic_spans() if 61 <= s.page <= 62)
        assert hasattr(estimate, "lesson_estimate"), "reader.plan.estimate.lesson_estimate is missing"
        got = (await c.get(f"/api/lessons/{ids['L02']}/estimate")).json()
        assert got == {"lesson_id": ids["L02"], **estimate.lesson_estimate(chars)}
        empty = (await c.get(f"/api/lessons/{ids['L01']}/estimate")).json()  # page 60 has no spans
        assert empty == {"lesson_id": ids["L01"], **estimate.lesson_estimate(0)}


async def test_estimate_refusals(pg: Pg, tmp_path: Path) -> None:
    from sqlalchemy import text

    async with plan_env(pg, tmp_path) as env:
        async with client(env) as c:
            _, confirmed = await _confirmed(c)
            lesson_id = confirmed["lessons"][0]["lesson_id"]
            for bad in ("not-a-uuid", str(uuid.uuid4())):
                r = await c.get(f"/api/lessons/{bad}/estimate")
                assert (r.status_code, r.json()) == (404, {"error": "not_found"}), bad
            async with env.engine.begin() as conn:  # a lesson without a course_plans row
                await conn.execute(text("DELETE FROM course_plans WHERE lesson_id = :l"), {"l": uuid.UUID(lesson_id)})
            r = await c.get(f"/api/lessons/{lesson_id}/estimate")
            assert (r.status_code, r.json()) == (404, {"error": "not_found"})
        async with client(env, "anna") as c:
            r = await c.get(f"/api/lessons/{confirmed['lessons'][1]['lesson_id']}/estimate")
            assert (r.status_code, r.json()) == (403, {"error": "owner_only"})
