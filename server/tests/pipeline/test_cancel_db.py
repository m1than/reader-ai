# @trace FR-PIPELINE-20, FR-PIPELINE-21, FR-PIPELINE-22, FR-LESSON-30, INV-LESSON-22
"""Cancel a run, the run history, inactive lessons, and the pipeline reads that hide tombstones. [db]

The provider is the fake CLI (no credit). A step's model call and lab run are wrapped so that a cancel (and, once, an
SRV-LESSON revert) lands while the step is between its two cancel reads.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from .conftest import BY_NAME_HINT, Pg, client, need, pipeline_env, plan, scripts_for

pytestmark = pytest.mark.anyio
CLEAN = [
    {"stage": "draft", "cost": 0.6},
    {"stage": "fact_check", "cost": 0.24, "unsupported": 0, "contradicted": 0},
    {"stage": "adversarial_review", "cost": 0.5, "blocking": 0, "nonblocking": 1},
]
F01 = (Path(__file__).resolve().parents[3] / "spec" / "fixtures" / "SRV-LESSON" / "F01.ipynb").read_bytes()
F01_SHA = hashlib.sha256(F01).hexdigest()


async def _sql(env: Any, sql: str, **params: Any) -> None:
    from sqlalchemy import text

    async with env.engine.begin() as conn:
        await conn.execute(text(sql), params)


async def _keys(env: Any, run: str) -> int:
    return int(await env.scalar("SELECT count(*) FROM job_keys WHERE args->>'run_id' = :r", r=run))


async def test_cancel_between_steps_twice_finished_and_resume(pg: Pg, tmp_path: Path) -> None:
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        lesson = await env.import_plan()
        env.cli.scripts[:] = scripts_for(CLEAN, "pass")
        async with client(env) as c:
            run = (await c.post(f"/api/lessons/{lesson}/compile", json={})).json()["run_id"]
            keys = await _keys(env, run)
            r = await c.post(f"/api/compiles/{run}/cancel", json={})
            assert r.status_code == 200, r.text
            assert r.json()["state"] == "cancelled" and r.json()["run_id"] == run
            assert (await c.post(f"/api/compiles/{run}/cancel", json={})).json() == r.json()
            assert await _keys(env, run) == keys  # nothing enqueued by the cancel
            await env.work()
            assert await env.scalar("SELECT count(*) FROM agent_spend") == 0  # the queued step did nothing
            assert await env.scalar("SELECT count(*) FROM compile_steps") == 0
            assert (await c.get(f"/api/compiles/{run}")).json()["state"] == "cancelled"
            resumed = await c.post(f"/api/compiles/{run}/resume")
            assert (resumed.status_code, resumed.json()["error"]) == (409, "not_failed")
            # a cancelled run is not running: a new compile starts
            run2 = (await c.post(f"/api/lessons/{lesson}/compile", json={})).json()["run_id"]
            for _ in range(3):
                await env.work()
            assert (await c.get(f"/api/compiles/{run2}")).json()["state"] == "converged"
            finished = await c.post(f"/api/compiles/{run2}/cancel", json={})
            assert (finished.status_code, finished.json()["error"]) == (409, "not_running")
            open_runs = (await c.get("/api/compiles")).json()["runs"]
            assert run not in {x["run_id"] for x in open_runs}
        async with client(env, "anna") as c:
            assert (await c.post(f"/api/compiles/{run}/cancel", json={})).status_code == 404
    assert await env.scalar("SELECT count(*) FROM audit_log WHERE action = 'compile.cancel'") == 1


async def test_cancel_during_a_model_call_then_revert(pg: Pg, tmp_path: Path, monkeypatch: Any) -> None:
    steps = need("reader.pipeline.steps")
    runs = need("reader.pipeline.runs")
    store = need("reader.lessons.store")
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        lesson = await env.import_plan()
        ivan = env.users["ivan"]
        doc = json.loads(F01)
        doc["cells"][0]["source"] = ["variant"]
        for data in (F01, json.dumps(doc).encode()):
            async with env.engine.begin() as conn:
                await store.write_revision(conn, env.blobs, ivan, lesson, data, "fixture", {})
        env.cli.scripts[:] = scripts_for(CLEAN, "pass")
        real = steps._model_step
        holder: dict[str, Any] = {}

        async def cancelling(*args: Any, **kwargs: Any) -> Any:
            result = await real(*args, **kwargs)  # the call settles in the ledger
            async with client(env) as c:
                assert (await c.post(f"/api/compiles/{holder['run']}/cancel", json={})).status_code == 200
                r = await c.post(f"/api/lessons/{lesson}/revert", json={"sha256": F01_SHA})
                assert r.json() == {"applied": True, "latest_sha256": F01_SHA}
            return result

        monkeypatch.setattr(steps, "_model_step", cancelling)
        async with client(env) as c:
            holder["run"] = run = (await c.post(f"/api/lessons/{lesson}/compile", json={})).json()["run_id"]
        keys = await _keys(env, run)
        revisions = await env.scalar("SELECT count(*) FROM lesson_revisions WHERE lesson_id = :l", l=lesson)
        await env.work()
        assert await env.scalar("SELECT count(*) FROM agent_spend WHERE kind = 'settle'") == 1
        assert await env.scalar("SELECT count(*) FROM compile_steps") == 0
        assert await env.scalar("SELECT count(*) FROM lesson_revisions WHERE lesson_id = :l", l=lesson) == revisions
        assert await _keys(env, run) == keys
        async with env.engine.connect() as conn:
            assert await store.current_sha256(conn, lesson) == F01_SHA  # the reverted revision stays current
        assert (await runs.status(env.engine, env.jobs, ivan, run))["state"] == "cancelled"


async def test_cancel_during_a_lab_run(pg: Pg, tmp_path: Path, monkeypatch: Any) -> None:
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        lesson = await env.import_plan()
        env.cli.scripts[:] = scripts_for(CLEAN, "pass")
        real = env.verify.run
        holder: dict[str, Any] = {}

        async def cancelling(notebook: Any) -> Any:
            report = await real(notebook)
            async with client(env) as c:
                assert (await c.post(f"/api/compiles/{holder['run']}/cancel", json={})).status_code == 200
            return report

        monkeypatch.setattr(env.verify, "run", cancelling)
        async with client(env) as c:
            holder["run"] = run = (await c.post(f"/api/lessons/{lesson}/compile", json={})).json()["run_id"]
            for _ in range(4):
                await env.work()
            body = (await c.get(f"/api/compiles/{run}")).json()
        assert body["state"] == "cancelled"
        stages = [r[0] for r in await env.rows("SELECT stage FROM compile_steps WHERE run_id = :r ORDER BY step", r=run)]
        # ADR-0039 order: lab verification is the second check, after the rule check
        assert "lab_verification" not in stages and stages[-1] == "rules"
        assert await env.scalar("SELECT g2 FROM compile_runs WHERE id = :r", r=run) is None  # no _finish
        assert await env.scalar(
            "SELECT count(*) FROM lesson_revision_stages WHERE lesson_id = :l AND stage = 'lab_verification'",
            l=lesson,
        ) == 0


async def _cancel_waiting_on_a_lock(env: Any) -> int:
    """The backend of the cancel's UPDATE once it waits on a lock (the run row), polled until it does."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10
    while loop.time() < deadline:
        pid = await env.scalar(
            "SELECT pid FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
            " AND query LIKE 'UPDATE compile_runs SET cancelled_at = now()%'"
        )
        if pid is not None:
            return int(pid)
        await asyncio.sleep(0.02)
    raise AssertionError("the cancel never waited on the run row")


async def test_cancel_waits_while_the_real_step_holds_the_run_row_then_nothing_after_runs(
    pg: Pg, tmp_path: Path, monkeypatch: Any
) -> None:
    steps = need("reader.pipeline.steps")
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        lesson = await env.import_plan()
        env.cli.scripts[:] = scripts_for(CLEAN, "pass")
        real_cancelled, real_load = steps._cancelled, steps.load_run
        paused, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        loads = [0]

        async def pause_after_the_second_read(conn: Any, run_id: Any) -> bool:
            seen: bool = await real_cancelled(conn, run_id)  # FOR UPDATE: the step's transaction holds the row
            if not paused.is_set():
                paused.set()
                await release.wait()
            return seen

        async def next_step_after_the_cancel(conn: Any, run_id: Any, *args: Any) -> Any:
            loads[0] += 1
            if loads[0] > 1:  # the next step's read (1) comes after the cancel committed
                await cancelled.wait()
            return await real_load(conn, run_id, *args)

        monkeypatch.setattr(steps, "_cancelled", pause_after_the_second_read)
        monkeypatch.setattr(steps, "load_run", next_step_after_the_cancel)
        async with client(env) as c:
            run = (await c.post(f"/api/lessons/{lesson}/compile", json={})).json()["run_id"]
            revisions = await env.scalar("SELECT count(*) FROM lesson_revisions WHERE lesson_id = :l", l=lesson)
            work = asyncio.create_task(env.work())
            await asyncio.wait_for(paused.wait(), 30)
            cancel = asyncio.create_task(c.post(f"/api/compiles/{run}/cancel", json={}))
            await _cancel_waiting_on_a_lock(env)
            assert not cancel.done()
            assert await env.scalar("SELECT cancelled_at FROM compile_runs WHERE id = :r", r=run) is None
            release.set()
            response = await asyncio.wait_for(cancel, 30)
            cancelled.set()
            await asyncio.wait_for(work, 60)
        assert (response.status_code, response.json()["state"]) == (200, "cancelled")
        # the paused draft step committed everything before the cancel; the next step returned at (1)
        assert [r[0] for r in await env.rows("SELECT stage FROM compile_steps WHERE run_id = :r", r=run)] == ["draft"]
        assert await env.scalar("SELECT count(*) FROM lesson_revisions WHERE lesson_id = :l", l=lesson) == revisions + 1
        assert await env.scalar("SELECT count(*) FROM agent_spend WHERE kind = 'reserve'") == 1  # the draft's only
        assert loads[0] == 2


async def test_a_cancel_during_a_budget_refusal_is_not_overwritten(pg: Pg, tmp_path: Path, monkeypatch: Any) -> None:
    steps = need("reader.pipeline.steps")
    errors = need("reader.agent.errors")
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        lesson = await env.import_plan()
        holder: dict[str, Any] = {}

        async def cancel_then_refuse(*args: Any, **kwargs: Any) -> Any:
            async with client(env) as c:
                assert (await c.post(f"/api/compiles/{holder['run']}/cancel", json={})).status_code == 200
            holder["finished_at"] = await env.scalar("SELECT finished_at FROM compile_runs WHERE id = :r", r=holder["run"])
            raise errors.BudgetRefused("task_budget_exhausted")

        monkeypatch.setattr(steps, "_model_step", cancel_then_refuse)
        async with client(env) as c:
            holder["run"] = run = (await c.post(f"/api/lessons/{lesson}/compile", json={})).json()["run_id"]
            keys = await _keys(env, run)
            await env.work()
            assert (await c.get(f"/api/compiles/{run}")).json()["state"] == "cancelled"
        row = (await env.rows("SELECT budget_refused, g2, finished_at FROM compile_runs WHERE id = :r", r=run))[0]
        assert (row[0], row[1], row[2]) == (None, None, holder["finished_at"])
        assert await _keys(env, run) == keys  # nothing enqueued after the refusal


async def test_cancel_of_a_budget_stopped_run_open_or_finished(pg: Pg, tmp_path: Path) -> None:
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        lesson = await env.import_plan()
        ivan = env.users["ivan"]
        insert = (
            "INSERT INTO compile_runs (owner_id, lesson_id, cap_usd, task_budgets, budget_refused, finished_at)"
            " VALUES (:o, :l, 8.00, '{}'::jsonb, 'task_budget_exhausted', FINISHED) RETURNING id"
        )
        async with env.engine.begin() as conn:
            from sqlalchemy import text

            stopped_open, stopped_done = [
                (await conn.execute(text(insert.replace("FINISHED", end)), {"o": ivan, "l": lesson})).scalar_one()
                for end in ("NULL", "now()")
            ]
        async with client(env) as c:
            r = await c.post(f"/api/compiles/{stopped_open}/cancel", json={})
            assert (r.status_code, r.json()["state"]) == (200, "cancelled")
            done = await c.post(f"/api/compiles/{stopped_done}/cancel", json={})
            assert (done.status_code, done.json()["error"]) == (409, "not_running")
        assert await env.scalar("SELECT cancelled_at FROM compile_runs WHERE id = :r", r=stopped_done) is None
        assert await env.scalar("SELECT count(*) FROM audit_log WHERE action = 'compile.cancel'") == 1


async def test_another_owners_run_and_lesson_are_not_found_on_cancel_and_history(pg: Pg, tmp_path: Path) -> None:
    store = need("reader.lessons.store")
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        await env.import_plan()
        anna = env.users["anna"]  # owns a course of her own (ownership is per row)
        async with env.engine.begin() as conn:
            from sqlalchemy import text

            course = await store.create_course(conn, anna, "Anna's course", None)
            theirs = await store.ensure_lesson(conn, anna, course, 1, "Hers")
            run = (
                await conn.execute(
                    text(
                        "INSERT INTO compile_runs (owner_id, lesson_id, cap_usd, task_budgets)"
                        " VALUES (:o, :l, 8.00, '{}'::jsonb) RETURNING id"
                    ),
                    {"o": anna, "l": theirs},
                )
            ).scalar_one()
        before = await env.rows("SELECT * FROM compile_runs ORDER BY id")
        async with client(env) as c:
            cancel = await c.post(f"/api/compiles/{run}/cancel", json={})
            history = await c.get(f"/api/lessons/{theirs}/compiles")
        assert (cancel.status_code, cancel.json()["error"]) == (404, "not_found")
        assert history.status_code == 404
        assert await env.rows("SELECT * FROM compile_runs ORDER BY id") == before
        assert await env.scalar("SELECT count(*) FROM audit_log") == 0


async def test_run_history(pg: Pg, tmp_path: Path) -> None:
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        lesson = await env.import_plan()
        ivan = env.users["ivan"]
        for age in range(21, 0, -1):
            await _sql(
                env,
                # finished runs of the approved order (order_version 1): they derive by the approved rule
                "INSERT INTO compile_runs (owner_id, lesson_id, cap_usd, task_budgets, g2, created_at, finished_at,"
                " order_version) VALUES (:o, :l, 8.00, '{}'::jsonb, '{\"blocking_open\": 0}'::jsonb,"
                " now() - make_interval(hours => :a), now() - make_interval(hours => :a), 1)",
                o=ivan, l=lesson, a=age,
            )
        async with client(env) as c:
            run = (await c.post(f"/api/lessons/{lesson}/compile", json={})).json()["run_id"]
            await env.app.job_manager.connector.execute_query_async(
                "DELETE FROM procrastinate_jobs WHERE status = 'todo'"
            )
            await _sql(env, "DELETE FROM job_keys WHERE args->>'run_id' = :r", r=run)
            r = await c.get(f"/api/lessons/{lesson}/compiles")
            assert r.status_code == 200, r.text
            listed = r.json()["runs"]
            assert len(listed) == 20 and listed[0]["run_id"] == run
            assert set(listed[0]) == {
                "run_id", "state", "created_at", "finished_at", "spent_usd", "cap_usd", "steps", "error",
                "loops", "stop", "refused_limit",  # FR-PIPELINE-6 revised (ADR-0039)
            }
            assert (listed[0]["loops"], listed[0]["stop"], listed[0]["refused_limit"]) == (0, None, None)
            assert listed[0]["state"] == "stalled" and listed[1]["state"] == "converged"
            assert [x["created_at"] for x in listed] == sorted((x["created_at"] for x in listed), reverse=True)
            assert await _keys(env, run) == 0  # read-only: no ensure
        async with client(env, "anna") as c:
            assert (await c.get(f"/api/lessons/{lesson}/compiles")).status_code == 404


async def test_inactive_lessons_and_tombstoned_reads(pg: Pg, tmp_path: Path) -> None:
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        lesson = await env.import_plan()
        course = await env.scalar("SELECT course_id FROM lessons WHERE id = :l", l=lesson)
        bad = {"stage": "fact_check", "cost": 0.24, "invalid": "summary_only"}
        env.cli.scripts[:] = scripts_for([{"stage": "draft", "cost": 0.6}, bad, bad, bad], "pass")
        async with client(env) as c:
            run = (await c.post(f"/api/lessons/{lesson}/compile", json={})).json()["run_id"]
            for _ in range(4):
                await env.work()
            assert (await c.get(f"/api/compiles/{run}")).json()["state"] == "stage_failed"
            for column, table, key in (("skipped_at", "lessons", lesson), ("archived_at", "courses", course)):
                extra = ", position = NULL" if table == "courses" else ""
                await _sql(env, f"UPDATE {table} SET {column} = now(){extra} WHERE id = :k", k=key)  # noqa: S608
                for method, path in (("POST", f"/api/lessons/{lesson}/compile"), ("POST", f"/api/compiles/{run}/resume")):
                    r = await c.request(method, path, json={})
                    assert (r.status_code, r.json()) == (409, {"error": "lesson_inactive"}), (column, path)
                restore = ", position = 1" if table == "courses" else ""
                await _sql(env, f"UPDATE {table} SET {column} = NULL{restore} WHERE id = :k", k=key)  # noqa: S608
            for column, table, key in (("deleted_at", "lessons", lesson), ("deleted_at", "courses", course)):
                extra = ", position = NULL" if table == "courses" else ""
                await _sql(env, f"UPDATE {table} SET {column} = now(){extra} WHERE id = :k", k=key)  # noqa: S608
                for method, path in (
                    ("POST", f"/api/lessons/{lesson}/compile"),
                    ("POST", f"/api/compiles/{run}/resume"),
                    ("GET", f"/api/compiles/{run}"),
                    ("GET", f"/api/lessons/{lesson}/compile"),
                    ("GET", f"/api/lessons/{lesson}/compiles"),
                    ("GET", f"/api/lessons/{lesson}/estimate"),
                ):
                    r = await c.request(method, path, json={}) if method == "POST" else await c.get(path)
                    assert r.status_code == 404, (table, path, r.text)
                assert run not in {x["run_id"] for x in (await c.get("/api/compiles")).json()["runs"]}
                restore = ", position = 1" if table == "courses" else ""
                await _sql(env, f"UPDATE {table} SET {column} = NULL{restore} WHERE id = :k", k=key)  # noqa: S608
            assert (await c.get(f"/api/compiles/{run}")).status_code == 200
            # the manifest import onto a deleted course: 409 with its id, nothing written
            await _sql(env, "UPDATE courses SET deleted_at = now(), position = NULL WHERE id = :k", k=course)
            before = await env.rows("SELECT count(*), max(g1_at) FROM course_plans")
            changed = copy.deepcopy(plan((61, 63)))
            r = await c.post("/api/pipeline/courses", json=changed)
            assert (r.status_code, r.json()) == (409, {"error": "course_deleted", "course_id": str(course)})
            assert await env.rows("SELECT count(*), max(g1_at) FROM course_plans") == before
