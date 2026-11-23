# @trace FR-PIPELINE-17
"""`GET /api/compiles`: the owner's open runs for the lesson list's run state (WEB-UI FR-UI-15). [db]

Finished runs are written as rows (their state is derived from the row alone, FR-PIPELINE-6); the stage_failed and
stalled runs are real. No credit is spent: the provider is the fake CLI.
"""

from __future__ import annotations

import copy
import datetime as dt
import uuid
from pathlib import Path
from typing import Any

import pytest

from .conftest import BY_NAME_HINT, MATERIAL, Pg, client, pipeline_env, scripts_for, synthetic_spans

pytestmark = pytest.mark.anyio

BAD_FACT = {"stage": "fact_check", "cost": 0.24, "invalid": "summary_only"}


def five_lessons() -> dict[str, Any]:
    return {
        "course": {"id": "open-runs", "title": "Open runs", "material_sha256": MATERIAL},
        "lessons": [
            {"id": name, "title": f"Lesson {name}", "chapter": 1, "pages": {"pdf": [page, page]}}
            for name, page in (("A", 61), ("B", 62), ("C", 63), ("D", 64), ("E", 65))
        ],
    }


async def _finished_run(
    env: Any, owner: uuid.UUID, lesson: uuid.UUID, age: dt.timedelta, *, refused: str | None
) -> None:
    from sqlalchemy import text

    async with env.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO compile_runs (owner_id, lesson_id, cap_usd, task_budgets, budget_refused, g2,"
                " created_at, finished_at) VALUES (:o, :l, 8.00, '{}'::jsonb, :b, CAST(:g AS jsonb),"
                " now() - make_interval(secs => :a), now() - make_interval(secs => :a))"
            ),
            {"o": owner, "l": lesson, "b": refused, "g": '{"blocking_open": 0}', "a": age.total_seconds()},
        )


async def _other_owner_run(env: Any) -> None:
    """A budget-stopped run under a second owner id (anna's own course and lesson)."""
    from sqlalchemy import text

    anna = env.users["anna"]
    async with env.engine.begin() as conn:
        course = (
            await conn.execute(
                text("INSERT INTO courses (owner_id, title) VALUES (:o, 'Other course') RETURNING id"), {"o": anna}
            )
        ).scalar_one()
        lesson = (
            await conn.execute(
                text(
                    "INSERT INTO lessons (owner_id, course_id, ordinal, title) VALUES (:o, :c, 1, 'Other')"
                    " RETURNING id"
                ),
                {"o": anna, "c": course},
            )
        ).scalar_one()
    await _finished_run(env, anna, lesson, dt.timedelta(minutes=1), refused="budget_exhausted")


async def test_open_runs(pg: Pg, tmp_path: Path) -> None:
    from sqlalchemy import text

    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        env.spans.spans = synthetic_spans(15)  # pages 61 to 65
        async with client(env) as c:
            r = await c.post("/api/pipeline/courses", json=copy.deepcopy(five_lessons()))
            assert r.status_code == 200, r.text
            ids = {x["plan_lesson_id"]: x["lesson_id"] for x in r.json()["lessons"]}
            ivan = env.users["ivan"]

            # E: a real stage_failed run (fact-check output invalid three times)
            env.cli.scripts[:] = scripts_for([{"stage": "draft", "cost": 0.6}, BAD_FACT, BAD_FACT, BAD_FACT], "pass")
            e_run = (await c.post(f"/api/lessons/{ids['E']}/compile")).json()["run_id"]
            for _ in range(4):
                await env.work()
            assert (await c.get(f"/api/compiles/{e_run}")).json()["state"] == "stage_failed"

            # B: an older budget-stopped run under a newer converged one; C: budget-stopped; D: converged
            await _finished_run(env, ivan, uuid.UUID(ids["B"]), dt.timedelta(hours=2), refused="budget_exhausted")
            await _finished_run(env, ivan, uuid.UUID(ids["B"]), dt.timedelta(hours=1), refused=None)
            await _finished_run(env, ivan, uuid.UUID(ids["C"]), dt.timedelta(minutes=30), refused="budget_exhausted")
            await _finished_run(env, ivan, uuid.UUID(ids["D"]), dt.timedelta(minutes=20), refused=None)
            await _other_owner_run(env)

            # A: a run whose enqueue was lost (stalled); the read re-enqueues it (ensure)
            a_run = (await c.post(f"/api/lessons/{ids['A']}/compile")).json()["run_id"]
            await env.app.job_manager.connector.execute_query_async(
                "DELETE FROM procrastinate_jobs WHERE status = 'todo'"
            )
            async with env.engine.begin() as conn:
                await conn.execute(text("DELETE FROM job_keys WHERE args->>'run_id' = :r"), {"r": a_run})

            r = await c.get("/api/compiles")
            assert r.status_code == 200, r.text
            listed = r.json()["runs"]
            assert [x["lesson_id"] for x in listed] == [ids["A"], ids["E"], ids["C"]]
            by_lesson = {x["lesson_id"]: x for x in listed}
            assert by_lesson[ids["A"]]["state"] == "running"  # re-enqueued by this read
            assert by_lesson[ids["E"]]["state"] == "stage_failed"
            assert by_lesson[ids["C"]]["state"] == "budget_exhausted"
            assert by_lesson[ids["C"]]["refusals"] == ["budget_exhausted"]
            single = (await c.get(f"/api/compiles/{a_run}")).json()
            assert set(by_lesson[ids["A"]]) == set(single)  # each entry is the FR-PIPELINE-11 status, with `error`
            assert "error" in single
            queued = await env.scalar("SELECT count(*) FROM job_keys WHERE args->>'run_id' = :r", r=a_run)
            assert queued == 1
        async with client(env, "anna") as c:
            r = await c.get("/api/compiles")
            assert (r.status_code, r.json()) == (404, {"error": "not_found"})


async def test_open_runs_are_bounded_newest_first(pg: Pg, tmp_path: Path) -> None:
    """bounds.open_runs_max = 100: 101 budget-refused runs of 101 lessons give 100 entries, the oldest cut."""
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        manifest = {
            "course": {"id": "many", "title": "Many", "material_sha256": MATERIAL},
            "lessons": [
                {"id": f"M{i:03d}", "title": f"M{i}", "chapter": 1, "pages": {"pdf": [61, 61]}} for i in range(101)
            ],
        }
        async with client(env) as c:
            r = await c.post("/api/pipeline/courses", json=manifest)
            assert r.status_code == 200, r.text
            ids = [x["lesson_id"] for x in r.json()["lessons"]]
            for i, lesson in enumerate(ids):  # M000 is the oldest
                await _finished_run(
                    env, env.users["ivan"], uuid.UUID(lesson), dt.timedelta(minutes=200 - i), refused="budget_exhausted"
                )
            listed = (await c.get("/api/compiles")).json()["runs"]
            assert len(listed) == 100
            assert [x["lesson_id"] for x in listed] == list(reversed(ids[1:]))
