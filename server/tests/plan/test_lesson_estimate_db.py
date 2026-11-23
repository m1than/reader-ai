# @trace FR-PLAN-13
"""The per-lesson estimate route for the owner's Compile confirm (WEB-UI FR-UI-15), on a migrated Postgres. [db]

Read-only: it writes nothing and starts nothing; the fake provider is never called.
"""

from __future__ import annotations

import uuid
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pytest

from .conftest import PAGE_CHARS, Pg, body, client, plan_env

pytestmark = pytest.mark.anyio

L02_LOW = Decimal("1.2587")
L02_HIGH = Decimal("2.9714")
L02_KCHARS = Decimal("58.938")
CAP = Decimal("8.00")
KEYS = {"lesson_id", "kchars", "low_usd", "high_usd", "run_cap_usd", "basis", "basis_kchars", "extrapolated"}


def _cents(x: Decimal) -> str:
    return str(x.quantize(Decimal("0.01"), ROUND_HALF_UP))


def expected(lesson_id: str, chars: int) -> dict[str, Any]:
    kchars = Decimal(chars) / 1000
    return {
        "lesson_id": lesson_id,
        "kchars": str(kchars.quantize(Decimal("0.001"), ROUND_HALF_UP)),
        "low_usd": _cents(kchars * L02_LOW / L02_KCHARS),
        "high_usd": _cents(min(kchars * L02_HIGH / L02_KCHARS, CAP)),
        "run_cap_usd": "8.00",
        "basis": "L02",
        "basis_kchars": "58.938",
        "extrapolated": not Decimal("0.5") * L02_KCHARS <= kchars.quantize(Decimal("0.001")) <= 2 * L02_KCHARS,
    }


async def _counts(env: Any) -> tuple[int, ...]:
    return tuple(
        [
            await env.scalar(f"SELECT count(*) FROM {t}")  # noqa: S608 (fixed table names)
            for t in ("compile_runs", "compile_steps", "lessons", "course_plans", "study_plans", "lesson_revisions")
        ]
    )


async def test_estimate_of_planned_lessons_matches_the_plan(pg: Pg, tmp_path: Path) -> None:
    async with plan_env(pg, tmp_path) as env, client(env) as c:
        preview = (await c.post("/api/plans/preview", json=body())).json()
        confirmed = (
            await c.post("/api/plans/confirm", json=body(plan_sha256=preview["plan_sha256"], compile_first=False))
        ).json()
        by_plan = {x["plan_lesson_id"]: x["lesson_id"] for x in confirmed["lessons"]}
        before = await _counts(env)
        for i, planned in enumerate(preview["lessons"]):
            first, last = planned["pages"]
            chars = sum(PAGE_CHARS[first - 1 : last])
            lesson_id = by_plan[planned["id"]]
            r = await c.get(f"/api/lessons/{lesson_id}/estimate")
            assert r.status_code == 200, r.text
            got = r.json()
            assert set(got) == KEYS
            assert got == expected(lesson_id, chars), planned["id"]
            if i == 0:  # the plan's own first-lesson estimate (FR-PLAN-8)
                first_lesson = preview["estimate"]["first_lesson"]
                assert (got["low_usd"], got["high_usd"]) == (first_lesson["low_usd"], first_lesson["high_usd"])
        assert await _counts(env) == before  # read-only


async def test_estimate_of_a_manifest_lesson(pg: Pg, tmp_path: Path) -> None:
    from pipeline.conftest import plan as manifest_plan
    from pipeline.conftest import synthetic_spans

    async with plan_env(pg, tmp_path) as env, client(env) as c:
        r = await c.post("/api/pipeline/courses", json=manifest_plan())
        assert r.status_code == 200, r.text
        ids = {x["plan_lesson_id"]: x["lesson_id"] for x in r.json()["lessons"]}
        chars = sum(len(s.text) for s in synthetic_spans() if 61 <= s.page <= 62)
        got = (await c.get(f"/api/lessons/{ids['L02']}/estimate")).json()
        assert got == expected(ids["L02"], chars)
        empty = (await c.get(f"/api/lessons/{ids['L01']}/estimate")).json()  # page 60 has no spans
        assert (empty["kchars"], empty["low_usd"], empty["high_usd"], empty["extrapolated"]) == (
            "0.000",
            "0.00",
            "0.00",
            True,
        )


async def test_estimate_refusals(pg: Pg, tmp_path: Path) -> None:
    async with plan_env(pg, tmp_path) as env:
        async with client(env) as c:
            preview = (await c.post("/api/plans/preview", json=body())).json()
            confirmed = (
                await c.post(
                    "/api/plans/confirm", json=body(plan_sha256=preview["plan_sha256"], compile_first=False)
                )
            ).json()
            lesson_id = confirmed["lessons"][0]["lesson_id"]
            for bad in ("not-a-uuid", str(uuid.uuid4())):
                r = await c.get(f"/api/lessons/{bad}/estimate")
                assert (r.status_code, r.json()) == (404, {"error": "not_found"}), bad
            # a lesson without a course_plans row
            from sqlalchemy import text

            async with env.engine.begin() as conn:
                await conn.execute(text("DELETE FROM course_plans WHERE lesson_id = :l"), {"l": uuid.UUID(lesson_id)})
            r = await c.get(f"/api/lessons/{lesson_id}/estimate")
            assert (r.status_code, r.json()) == (404, {"error": "not_found"})
        async with client(env, "anna") as c:
            r = await c.get(f"/api/lessons/{confirmed['lessons'][1]['lesson_id']}/estimate")
            assert (r.status_code, r.json()) == (403, {"error": "owner_only"})
