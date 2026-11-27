# @trace FR-PIPELINE-20
"""FR-PIPELINE-20 revised: the order change at deploy. Migration pipeline_0006 adds `order_version` (1 for rows that
exist, 2 for new runs); at worker start one statement cancels every open v1 run whatever its jobs' state, with an
audit row (actor: the nil UUID) and one obs.runs end each; a queued v1 job does nothing; a finished v1 run derives
its state by the approved rule. [db]"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from .conftest import BY_NAME_HINT, Pg, need, pipeline_env, scripts_for, target

pytestmark = pytest.mark.anyio
NIL = "00000000-0000-0000-0000-000000000000"


async def _sql(env: Any, sql: str, **params: Any) -> Any:
    from sqlalchemy import text

    async with env.engine.begin() as conn:
        return await conn.execute(text(sql), params)


async def _v1_run(env: Any, lesson: uuid.UUID, *, finished: bool = False) -> uuid.UUID:
    row = await _sql(
        env,
        "INSERT INTO compile_runs (owner_id, lesson_id, cap_usd, task_budgets, order_version, g2, finished_at)"
        " VALUES (:o, :l, 8.00, '{}'::jsonb, 1, CAST(:g AS jsonb), CASE WHEN :f THEN now() END) RETURNING id",
        o=env.users["ivan"], l=lesson, g='{"blocking_open": 0}' if finished else None, f=finished,
    )
    run: uuid.UUID = row.scalar_one()
    return run


async def test_migration_defaults_and_downgrade(pg: Pg, tmp_path: Path) -> None:
    from sqlalchemy import text

    from reader.db import make_engine

    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        lesson = await env.import_plan()
        legacy = await _v1_run(env, lesson, finished=True)
        new = await env.compile()
        assert await env.scalar("SELECT order_version FROM compile_runs WHERE id = :r", r=legacy) == 1
        assert await env.scalar("SELECT order_version FROM compile_runs WHERE id = :r", r=new) == 2
    engine = make_engine(target(pg))
    migration = need("reader.pipeline.migrations.pipeline_0006_order_version")
    async with engine.begin() as conn:
        for statement in migration.DOWNGRADE:
            await conn.execute(text(statement))
        columns = (
            await conn.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = 'compile_runs'"
                     " AND column_name = 'order_version'")
            )
        ).all()
    assert columns == []
    await engine.dispose()


async def test_worker_start_cancels_open_v1_runs(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs = need("reader.pipeline.runs")
    from reader.obs import runs as obs_runs

    ends: list[tuple[str, int]] = []

    async def enqueue_end(kind: str, ident: str, end: int, jobs: Any = None) -> None:
        ends.append((ident, end))

    monkeypatch.setattr(obs_runs, "enqueue_end", enqueue_end)
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        lesson = await env.import_plan()
        queued = await _v1_run(env, lesson)
        dead = await _v1_run(env, lesson)
        idle = await _v1_run(env, lesson)
        finished = await _v1_run(env, lesson, finished=True)
        # a queued job for `queued`, a dead key for `dead`, nothing for `idle`
        args = {"run_id": str(queued), "step": 2, "stage": "fact_check", "input_sha256": "0" * 64}
        await env.jobs.enqueue("pipeline.stage", args)
        await _sql(
            env,
            "INSERT INTO job_keys (kind, key, args, outcome, attempts)"
            " VALUES ('pipeline.stage', :k, CAST(:a AS jsonb), 'dead', 3)",
            k="dead-" + str(dead), a='{"run_id": "' + str(dead) + '"}',
        )
        cancelled = await runs.cancel_legacy(env.engine, env.jobs)
        assert sorted(cancelled) == sorted([queued, dead, idle])
        for run in (queued, dead, idle):
            row = (await env.rows("SELECT cancelled_at, finished_at, ends FROM compile_runs WHERE id = :r", r=run))[0]
            assert row[0] is not None and row[1] is not None and row[2] == 1
            audit = await env.rows(
                "SELECT actor_id::text, action, detail FROM audit_log WHERE object_id = :o", o=str(run)
            )
            assert audit == [(NIL, "compile.cancel", {"lesson_id": str(lesson)})]
        assert sorted(ends) == sorted([(str(r), 1) for r in (queued, dead, idle)])
        # the queued v1 job runs afterwards: it reserves, calls and writes nothing
        env.cli.scripts[:] = scripts_for([{"stage": "fact_check", "cost": 0.24}], "pass")
        await env.work()
        assert await env.scalar("SELECT count(*) FROM compile_steps WHERE run_id = :r", r=queued) == 0
        assert await env.scalar("SELECT count(*) FROM agent_spend WHERE run_id = :r", r=queued) == 0
        assert len(env.cli.scripts) == 1
        # a finished v1 run keeps the approved rule, with no stop
        status = await env.state(finished)
        assert status["state"] == "converged" and status["stop"] is None and status["refused_limit"] is None
        # a second start changes nothing
        assert await runs.cancel_legacy(env.engine, env.jobs) == []
        assert len(ends) == 3


async def test_open_v2_runs_are_untouched(pg: Pg, tmp_path: Path) -> None:
    runs = need("reader.pipeline.runs")
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        await env.import_plan()
        run = await env.compile()
        assert await runs.cancel_legacy(env.engine, env.jobs) == []
        assert await env.scalar("SELECT cancelled_at FROM compile_runs WHERE id = :r", r=run) is None


async def test_read_1_refuses_a_v1_job_without_the_cancel(pg: Pg, tmp_path: Path) -> None:
    """(b): a queued job of an open v1 run that was not cancelled (the worker start has not run) does nothing."""
    steps = need("reader.pipeline.steps")
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        lesson = await env.import_plan()
        run = await _v1_run(env, lesson)
        args = {"run_id": str(run), "step": 1, "stage": "draft", "input_sha256": "0" * 64}
        payload = type("Args", (), args)
        async with env.engine.begin() as conn:
            await steps.handle(payload, conn, None)
        assert await env.scalar("SELECT count(*) FROM compile_steps WHERE run_id = :r", r=run) == 0
        assert await env.scalar("SELECT count(*) FROM agent_spend WHERE run_id = :r", r=run) == 0
        assert await env.scalar("SELECT cancelled_at FROM compile_runs WHERE id = :r", r=run) is None


async def test_ends_cap_and_worker_start_order(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(a): `ends` stays at most 100; the worker start cancels v1 runs before it ensures unfinished runs."""
    jobs_mod = need("reader.pipeline.jobs")
    order: list[str] = []
    real_cancel, real_ensure = jobs_mod.cancel_legacy, jobs_mod.ensure_all

    async def cancel(engine: Any, jobs: Any) -> Any:
        order.append("cancel")
        return await real_cancel(engine, jobs)

    async def ensure(engine: Any, jobs: Any) -> Any:
        order.append("ensure")
        return await real_ensure(engine, jobs)

    monkeypatch.setattr(jobs_mod, "cancel_legacy", cancel)
    monkeypatch.setattr(jobs_mod, "ensure_all", ensure)
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        lesson = await env.import_plan()
        run = await _v1_run(env, lesson)
        await _sql(env, "UPDATE compile_runs SET ends = 100 WHERE id = :r", r=run)
        await jobs_mod.on_worker_start(env.jobs)
        assert order == ["cancel", "ensure"]
        assert await env.scalar("SELECT ends FROM compile_runs WHERE id = :r", r=run) == 100
        assert await env.scalar("SELECT cancelled_at FROM compile_runs WHERE id = :r", r=run) is not None
