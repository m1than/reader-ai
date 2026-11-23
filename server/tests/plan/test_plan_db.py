# @trace FR-PLAN-6, FR-PLAN-9, FR-PLAN-11, FR-PLAN-12, INV-PLAN-4, INV-PLAN-5, NFR-PLAN-1
"""Preview, confirm and the stored plan through the ASGI app, on a migrated Postgres; idempotency and races. [db]"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from .conftest import CANARY, MATERIAL_ID, PLAN_SHA, Pg, body, client, need, pipeline_env, plan_env, today

CAP = Decimal("8.00")

pytestmark = pytest.mark.anyio
PLAN_COURSE = f"plan-{PLAN_SHA[:12]}"


async def _preview(c: Any, **kw: Any) -> dict[str, Any]:
    r = await c.post("/api/plans/preview", json=body(**kw))
    assert r.status_code == 200, r.text
    data: dict[str, Any] = r.json()
    return data


async def _confirm(c: Any, sha: str, compile_first: bool = False, **kw: Any) -> Any:
    return await c.post("/api/plans/confirm", json=body(plan_sha256=sha, compile_first=compile_first, **kw))


async def test_preview_writes_nothing_and_validates(
    pg: Pg, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    async with plan_env(pg, tmp_path) as env:
        async with client(env) as c:
            p = await _preview(c)
            assert set(p) == {"plan_sha256", "inputs", "lessons", "summary", "estimate"}
            assert p["inputs"]["rate"] == "2.64"
            assert p["inputs"]["material_id"] == MATERIAL_ID
            assert p["inputs"]["start_date"] == p["inputs"]["today"] == today()
            # 28 pages with text over 14 days → 14 lessons; every page is planned exactly once
            assert p["summary"]["new_count"] == 14
            assert p["summary"]["fits"] is True
            pages = [pg for x in p["lessons"] for pg in range(x["pages"][0], x["pages"][1] + 1)]
            assert pages == list(range(1, 31))
            assert p["lessons"][0]["title"].startswith("Chapter one")
            assert {"id", "pages", "minutes", "title", "fixed"} <= set(p["lessons"][0])
            assert await env.scalar("SELECT count(*) FROM study_plans") == 0
            assert (
                await env.scalar("SELECT count(*) FROM course_plans WHERE plan_course_id = :c", c=PLAN_COURSE)
                == 0
            )
            # same inputs, same plan
            assert (await _preview(c))["plan_sha256"] == p["plan_sha256"]
            assert (await _preview(c, rate="3.00"))["inputs"]["rate"] == "3.00"
            for bad, code in (
                ({"total_weeks": 0}, "total_weeks"),
                ({"total_weeks": True}, "total_weeks"),
                ({"minutes_per_day": 62}, "minutes_per_day"),
                ({"rate": "1.234"}, "rate"),
                ({"rate": "20.01"}, "rate"),
                ({"today": "2020-01-01"}, "today"),
                ({"today": "not a date"}, "today"),
                ({"material_id": 5}, "material_id"),
            ):
                r = await c.post("/api/plans/preview", json={**body(), **bad})
                assert (r.status_code, r.json()) == (400, {"error": code}), bad
            for missing in ("unknown", "never-imported"):
                r = await c.post("/api/plans/preview", json=body(material_id=missing))
                assert (r.status_code, r.json()) == (404, {"error": "not_found"})
        async with client(env, "anna") as c:
            r = await c.post("/api/plans/preview", json=body())
            assert (r.status_code, r.json()) == (403, {"error": "owner_only"})
    logs = [r.getMessage() for r in caplog.records if r.name == "reader.plan"]
    assert any(m.startswith("plan.preview ") and f"material={PLAN_SHA[:12]}" in m for m in logs)
    assert all(CANARY not in r.getMessage() for r in caplog.records)


async def test_confirm_imports_the_plan_and_replays(
    pg: Pg, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    async with plan_env(pg, tmp_path) as env, client(env) as c:
        p = await _preview(c)
        r = await _confirm(c, "0" * 64)
        assert r.status_code == 409
        assert r.json()["error"] == "plan_changed"
        changed = r.json()["plan"]
        assert changed["plan_sha256"] == p["plan_sha256"]
        assert CANARY not in r.text  # INV-PLAN-4: no titles in an error body
        r = await _confirm(c, p["plan_sha256"])
        assert r.status_code == 200, r.text
        first = r.json()
        assert (first["replayed"], first["compile"], first["created"], first["removed"]) == (
            False,
            None,
            14,
            0,
        )
        assert first["plan_course_id"] == PLAN_COURSE
        assert [x["ordinal"] for x in first["lessons"]] == list(range(1, 15))
        assert [x["plan_lesson_id"] for x in first["lessons"]] == [x["id"] for x in p["lessons"]]
        origins = await env.rows(
            "SELECT DISTINCT origin FROM course_plans WHERE plan_course_id = :c", c=PLAN_COURSE
        )
        assert origins == [("study_plan",)]
        row = await env.rows(
            "SELECT total_weeks, minutes_per_day, rate_centi, start_date, plan_sha256 FROM study_plans"
        )
        assert row == [(2, 60, 264, dt.date.fromisoformat(today()), p["plan_sha256"])]
        title = await env.scalar(
            "SELECT title FROM courses WHERE id = :c", c=uuid.UUID(first["course_id"])
        )
        assert title == MATERIAL_ID
        # INV-PLAN-5 (a): the same body again replays
        again = (await _confirm(c, p["plan_sha256"])).json()
        assert again["replayed"] is True
        assert again["lessons"] == first["lessons"]
        assert await env.scalar("SELECT count(*) FROM study_plans") == 1
        # GET /api/plans
        got = await c.get("/api/plans", params={"material_id": MATERIAL_ID})
        assert got.status_code == 200
        assert got.json()["plan_sha256"] == p["plan_sha256"]
        assert got.json()["started"] == 0
        assert got.json()["inputs"]["total_weeks"] == 2
        assert (await c.get("/api/plans")).status_code == 400
        assert (await c.get("/api/plans?material_id=a&material_id=b")).status_code == 400
        r = await c.get("/api/plans", params={"material_id": "never-imported"})
        assert (r.status_code, r.json()) == (404, {"error": "not_found"})
        # does_not_fit
        tight = await _preview(c, weeks=1, minutes=10)
        assert tight["summary"]["fits"] is False
        r = await _confirm(c, tight["plan_sha256"], weeks=1, minutes=10)
        assert (r.status_code, r.json()) == (422, {"error": "does_not_fit"})
    logs = [r.getMessage() for r in caplog.records if r.name == "reader.plan"]
    assert any(m.startswith("plan.confirm ") and "replayed=True" in m for m in logs)
    assert all(CANARY not in r.getMessage() for r in caplog.records)


async def test_compile_first_and_lost_response_retry(pg: Pg, tmp_path: Path) -> None:
    """INV-PLAN-5 (b): a retry of a committed confirm with compile_first creates no second run."""
    async with plan_env(pg, tmp_path) as env, client(env) as c:
        p = await _preview(c)
        first = (await _confirm(c, p["plan_sha256"], compile_first=True)).json()
        assert set(first["compile"]) == {"run_id"}
        lesson_one = first["lessons"][0]["lesson_id"]
        assert await env.scalar("SELECT lesson_id::text FROM compile_runs") == lesson_one
        retry = (await _confirm(c, p["plan_sha256"], compile_first=True)).json()
        assert retry["replayed"] is True
        assert retry["lessons"] == first["lessons"]
        assert retry["compile"] == {"error": "already_started"}
        assert await env.scalar("SELECT count(*) FROM compile_runs") == 1
        # retry after midnight: today moved, the stored sha still replays
        late = body(plan_sha256=p["plan_sha256"], compile_first=False)
        late["today"] = (dt.date.fromisoformat(today()) + dt.timedelta(days=5)).isoformat()
        r = await c.post("/api/plans/confirm", json=late)
        assert r.status_code == 200
        assert r.json()["replayed"] is True


async def test_replan_keeps_started_lessons_and_plan_order(pg: Pg, tmp_path: Path) -> None:
    """FR-PLAN-6 and INV-PLAN-5 (d): a started lesson stays; the rest is re-packed; ordinals follow the plan."""
    async with plan_env(pg, tmp_path) as env, client(env) as c:
        p = await _preview(c)
        first = (await _confirm(c, p["plan_sha256"], compile_first=True)).json()
        started = first["lessons"][0]
        replan = await _preview(c, weeks=3)
        assert replan["summary"]["fixed_count"] == 1
        assert replan["lessons"][0]["fixed"] is True
        assert replan["lessons"][0]["id"] == started["plan_lesson_id"]
        done = (await _confirm(c, replan["plan_sha256"], weeks=3)).json()
        assert done["lessons"][0]["lesson_id"] == started["lesson_id"]
        assert done["removed"] >= 1
        rows = await env.rows(
            "SELECT p.pdf_first, l.ordinal FROM course_plans p JOIN lessons l ON l.id = p.lesson_id"
            " WHERE p.plan_course_id = :c ORDER BY p.pdf_first",
            c=PLAN_COURSE,
        )
        assert [o for _, o in rows] == list(range(1, len(rows) + 1))
        assert len(rows) == len(replan["lessons"])
        got = (await c.get("/api/plans", params={"material_id": MATERIAL_ID})).json()
        assert got["started"] == 1
        assert got["inputs"]["total_weeks"] == 3
        # the stored start date is kept (owner decision 4)
        assert got["inputs"]["start_date"] == today()


async def test_race_confirm_against_create_run(pg: Pg, tmp_path: Path) -> None:
    """INV-PLAN-5 (c): a removal and a run on the same lesson never both happen."""
    from sqlalchemy import text

    courses = need("reader.pipeline.courses")
    runs = need("reader.pipeline.runs")
    errors = need("reader.pipeline.errors")
    async with plan_env(pg, tmp_path) as env:
        owner = env.users["ivan"]
        async with client(env) as c:
            p = await _preview(c)
            first = (await _confirm(c, p["plan_sha256"])).json()
        victim = uuid.UUID(first["lessons"][-1]["lesson_id"])
        last = p["lessons"][-1]
        keep = [x for x in p["lessons"] if x["id"] != last["id"]]
        plan = courses.Plan(
            PLAN_COURSE,
            MATERIAL_ID,
            PLAN_SHA,
            [courses.PlanLesson(x["id"], "t", x["pages"][0], x["pages"][1]) for x in keep[:-1]]
            + [courses.PlanLesson("merged", "t", keep[-1]["pages"][0], last["pages"][1])],
        )
        # 1) the confirm holds the course lock and removes the lesson; create_run waits, then finds no lesson
        async with env.engine.connect() as conn:
            tx = await conn.begin()
            await courses.lock_course(conn, owner, PLAN_COURSE)
            await courses.import_course_plan(conn, owner, plan, origin="study_plan")
            task = asyncio.create_task(runs.create_run(env.engine, env.jobs, owner, victim, CAP))
            for _ in range(100):
                waiting = await env.scalar(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                )
                if waiting:
                    break
                await asyncio.sleep(0.05)
            assert waiting, "create_run did not wait on the course lock"
            await tx.commit()
        with pytest.raises(errors.PipelineError) as raised:
            await task
        assert raised.value.code == "lesson_unknown"
        assert await env.scalar("SELECT count(*) FROM compile_runs WHERE lesson_id = :l", l=victim) == 0
        assert await env.scalar("SELECT count(*) FROM lessons WHERE id = :l", l=victim) == 0
        # 2) the run comes first (sequential, not concurrent: the run commits before the confirm starts): the
        #    confirm of a plan that drops that lesson is refused
        async with client(env) as c:
            p2 = await _preview(c, weeks=3)
            target = uuid.UUID(
                await env.scalar(
                    "SELECT lesson_id::text FROM course_plans WHERE plan_course_id = :c ORDER BY pdf_first DESC LIMIT 1",
                    c=PLAN_COURSE,
                )
            )
            await runs.create_run(env.engine, env.jobs, owner, target, CAP)
            r = await _confirm(c, p2["plan_sha256"], weeks=3)
            assert r.status_code == 409
            assert r.json()["error"] == "plan_changed"
            assert await env.scalar("SELECT count(*) FROM lessons WHERE id = :l", l=target) == 1
        async with env.engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM compile_runs"))).scalar() == 1


async def test_table_and_migration(pg: Pg, tmp_path: Path) -> None:
    """FR-PLAN-11: study_plans with its keys and checks; the downgrade drops only this table."""
    import importlib.util

    async with plan_env(pg, tmp_path) as env:
        cols = {
            r[0]
            for r in await env.rows(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'study_plans'"
            )
        }
        assert cols == {
            "course_id",
            "owner_id",
            "plan_course_id",
            "material_sha256",
            "ingest_id",
            "total_weeks",
            "minutes_per_day",
            "rate_centi",
            "start_date",
            "plan_sha256",
            "confirmed_at",
        }
    path = Path(need("reader.plan").__file__).parent / "migrations" / "plan_0001.py"
    spec = importlib.util.spec_from_file_location("plan_0001", path)
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.branch_labels == ("plan",)
    assert migration.DOWNGRADE == ("DROP TABLE study_plans",)


async def test_preview_latency_on_a_pilot_sized_material(pg: Pg, tmp_path: Path) -> None:
    """NFR-PLAN-1 (bench): 935 pages and 494 outline entries; p95 of 20 previews ≤ 500 ms, with suggestions."""
    from .conftest import import_plan_material

    async with pipeline_env(pg, tmp_path) as env:
        outline = tuple((1 + i % 3, 1 + (i * 2) % 935, f"Entry {i}") for i in range(494))
        await import_plan_material(
            env.engine, env.users["ivan"], tuple(2000 + (i * 37) % 900 for i in range(935)), outline
        )
        async with client(env) as c:
            times = []
            for _ in range(20):
                t0 = time.perf_counter()
                r = await c.post("/api/plans/preview", json=body(weeks=1, minutes=10))
                times.append((time.perf_counter() - t0) * 1000)
                assert r.status_code == 200
                assert r.json()["summary"]["fits"] is False
            times.sort()
            assert times[18] <= 500, times
