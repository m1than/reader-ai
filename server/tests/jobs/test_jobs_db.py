"""SRV-EVENTS [db]: enqueue, the worker wrapper, the sweep, requeue and status against Postgres.

@trace FR-EVENTS-1 FR-EVENTS-3 FR-EVENTS-4 FR-EVENTS-5 FR-EVENTS-6 FR-EVENTS-7 FR-EVENTS-8 FR-EVENTS-9
@trace FR-EVENTS-11 FR-EVENTS-12 FR-EVENTS-13 FR-EVENTS-14 FR-EVENTS-15 FR-EVENTS-10 FR-EVENTS-16
@trace INV-EVENTS-1 INV-EVENTS-2 INV-EVENTS-3 INV-EVENTS-4 INV-EVENTS-5 INV-EVENTS-7
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

import anyio
import pytest

from .conftest import HANDLER_SECRET, KIND, Pg, jobs_env, require_jobs, target

pytestmark = pytest.mark.anyio

HERE = Path(__file__).resolve().parent  # kill_worker.py and conftest.py, for the real worker process
SRC = HERE.parents[1] / "src"

_TABLES = "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
_TODO_DELAY = (
    "SELECT extract(epoch FROM scheduled_at - now()) FROM procrastinate_jobs"
    " WHERE lock = :k AND status = 'todo'"
)
_LOCK = "SELECT pg_advisory_xact_lock(:k)"


async def _results(env: Any, name: str, n: int = 5, **args: Any) -> list[str]:
    out: list[str] = []

    async def one() -> None:
        out.append((await env.jobs.enqueue(KIND, {"name": name, **args})).kind)

    async with anyio.create_task_group() as tg:
        for _ in range(n):
            tg.start_soon(one)
    return sorted(out)


def _failed() -> Any:
    from procrastinate.jobs import Status

    return Status.FAILED


# ---- FR-EVENTS-3, -4, -12: the happy path


async def test_enqueue_run_and_results(pg: Pg) -> None:
    async with jobs_env(pg) as env:
        first = await env.jobs.enqueue(KIND, {"name": "a"})
        assert first.kind == "queued"
        assert first.job_id is not None
        assert (await env.jobs.enqueue(KIND, {"name": "a", "mode": "ok"})).kind == "already_queued"
        assert await env.jobs.job_status(KIND, env.key("a")) == "queued"
        await env.work()
        assert await env.effects("a") == 1
        assert await env.jobs.job_status(KIND, env.key("a")) == "succeeded"
        done = await env.jobs.enqueue(KIND, {"name": "a"})
        assert done.kind == "already_done"
        assert done.job_id == first.job_id
        assert await env.jobs.job_status(KIND, "test.effect:" + "f" * 64) == "none"


async def test_migration_heads(pg: Pg) -> None:
    from sqlalchemy import text

    require_jobs()
    from reader.db import alembic_config, at_heads, make_engine, migrate

    engine = make_engine(target(pg))
    try:
        await migrate(engine)
        assert await at_heads(engine) is True
        async with engine.connect() as conn:
            versions = set((await conn.execute(text("SELECT version_num FROM alembic_version"))).scalars())
            tables = set((await conn.execute(text(_TABLES))).scalars())
        assert "jobs_0003" in versions  # the branch head since FR-EVENTS-30 (V/manage)
        assert "job_keys" in tables
        assert "procrastinate_jobs" in tables

        def downgrade(sync_conn: Any) -> None:
            from alembic import command

            config = alembic_config()
            config.attributes["connection"] = sync_conn
            command.downgrade(config, "jobs@base")

        async with engine.connect() as conn:
            await conn.run_sync(downgrade)
            await conn.commit()
            after = set((await conn.execute(text(_TABLES))).scalars())
        assert "job_keys" not in after
        assert "procrastinate_jobs" in after
        assert "users" in after
    finally:
        await engine.dispose()


async def test_enqueue_never_waits_for_a_key_lock(pg: Pg) -> None:
    from sqlalchemy import text

    async with jobs_env(pg) as env:
        jobs = require_jobs()
        async with env.engine.connect() as holder:
            await holder.execute(text(_LOCK), {"k": jobs.lock_id(env.key("held"))})
            with anyio.fail_after(2):
                result = await env.jobs.enqueue(KIND, {"name": "held"})
            assert result.kind == "queued"
            assert (await env.jobs.requeue_dead(KIND, env.key("held"))).kind == "running"
            await holder.rollback()


# ---- FR-EVENTS-1 (SRV-TUTOR amendment): priority


async def test_priority_is_passed_at_every_defer_and_orders_fetch(pg: Pg) -> None:
    import dataclasses

    from .conftest import HANDLER_RUNS, effect_kind

    jobs = require_jobs()
    low = effect_kind()
    high = dataclasses.replace(low, name="test.high", priority=100)
    async with jobs_env(pg, kinds=[low, high]) as env:
        HANDLER_RUNS.clear()
        assert (await env.jobs.enqueue(KIND, {"name": "first"})).kind == "queued"
        assert (await env.jobs.enqueue("test.high", {"name": "second"})).kind == "queued"
        assert (await env.jobs.enqueue("test.high", {"name": "flaky", "mode": "transient"})).kind == "queued"
        low_key = jobs.idempotency_key(low, {"name": "first"})
        high_key = jobs.idempotency_key(high, {"name": "second"})
        flaky_key = jobs.idempotency_key(high, {"name": "flaky"})
        sql = "SELECT lock, priority FROM procrastinate_jobs WHERE lock IN (:a, :b)"
        stored = {r[0]: r[1] for r in await env.rows(sql, a=low_key, b=high_key)}
        assert stored == {low_key: 0, high_key: 100}
        await env.work()
        # The later, higher-priority job ran first; the transient one was deferred again with its priority.
        assert HANDLER_RUNS.index(high_key) < HANDLER_RUNS.index(low_key)
        sql = "SELECT priority FROM procrastinate_jobs WHERE lock = :k ORDER BY id"
        priorities = await env.rows(sql, k=flaky_key)
        assert len(priorities) >= 2
        assert {p[0] for p in priorities} == {100}


# ---- FR-EVENTS-6, -14: failures, retries, exhaustion


async def test_exhaustion_counts_per_key(pg: Pg) -> None:
    async with jobs_env(pg) as env:
        await env.jobs.enqueue(KIND, {"name": "t", "mode": "transient"})
        await env.work()
        row = await env.key_row("t")
        assert (row.attempts, row.outcome, row.error_code) == (3, "dead", "Transient")
        assert await env.effects("t") == 0  # effects roll back with their failed run
        # three single-shot procrastinate jobs, counted on one key
        assert await env.job_statuses("t") == ["failed", "failed", "failed"]
        assert (await env.jobs.enqueue(KIND, {"name": "t"})).kind == "dead"


async def test_retry_delays_are_5_then_25_seconds(pg: Pg) -> None:
    require_jobs()
    from reader.jobs.queue import retry_delay_s

    async with jobs_env(pg) as env:
        assert [retry_delay_s(1), retry_delay_s(2)] == [5, 25]
        env.jobs.retry_delay = retry_delay_s
        await env.jobs.enqueue(KIND, {"name": "d", "mode": "transient"})
        await env.work()
        assert 3 <= float(await env.scalar(_TODO_DELAY, k=env.key("d"))) <= 5.5
        await env.execute("UPDATE procrastinate_jobs SET scheduled_at = now() WHERE status = 'todo'")
        await env.work()
        assert 23 <= float(await env.scalar(_TODO_DELAY, k=env.key("d"))) <= 25.5


async def test_permanent_failure_and_timeout_are_dead_at_once(pg: Pg) -> None:
    async with jobs_env(pg, timeout_s=0.3) as env:
        await env.jobs.enqueue(KIND, {"name": "p", "mode": "permanent", "message": "boom"})
        await env.jobs.enqueue(KIND, {"name": "slow", "sleep_s": 5})
        await env.work()
        p, slow = await env.key_row("p"), await env.key_row("slow")
        assert (p.attempts, p.outcome, p.error_code) == (1, "dead", "RuntimeError")
        assert (slow.attempts, slow.outcome, slow.error_code) == (1, "dead", "timeout")
        assert await env.effects("p") == 0
        assert await env.effects("slow") == 0


async def test_handler_timeouterror_is_judged_by_transient(pg: Pg) -> None:
    import dataclasses

    from .conftest import effect_kind

    async def raises_timeout(args: Any, conn: Any, ctx: Any) -> None:
        raise TimeoutError("provider read timed out")

    base = effect_kind()
    transient = dataclasses.replace(base, handler=raises_timeout, transient=(TimeoutError,))
    async with jobs_env(pg, kinds=[transient]) as env:
        env.jobs.retry_delay = lambda _attempts: 3600
        await env.jobs.enqueue(KIND, {"name": "t1"})
        await env.work()
        row = await env.key_row("t1")
        assert (row.attempts, row.outcome) == (1, None)  # counted, retried later, not `timeout`
        assert await env.job_statuses("t1") == ["failed", "todo"]
    permanent = dataclasses.replace(base, handler=raises_timeout, transient=())
    async with jobs_env(pg, kinds=[permanent]) as env:
        await env.jobs.enqueue(KIND, {"name": "t2"})
        await env.work()
        row = await env.key_row("t2")
        assert (row.attempts, row.outcome, row.error_code) == (1, "dead", "TimeoutError")


async def test_deadline_overrun_is_timeout(pg: Pg) -> None:
    import dataclasses

    from sqlalchemy import text

    from .conftest import effect_kind

    async def sleeps_in_sql(args: Any, conn: Any, ctx: Any) -> None:
        await conn.execute(text("SELECT pg_sleep(5)"))

    kind = dataclasses.replace(effect_kind(), handler=sleeps_in_sql, timeout_s=0.5, transient=(TimeoutError,))
    async with jobs_env(pg, kinds=[kind]) as env:
        await env.jobs.enqueue(KIND, {"name": "dl"})
        await env.work()
        row = await env.key_row("dl")
        assert (row.attempts, row.outcome, row.error_code) == (1, "dead", "timeout")


# ---- FR-EVENTS-7, -15: stalls and the sweep


async def test_kill_mid_handler(pg: Pg) -> None:
    async with jobs_env(pg) as env:
        await env.jobs.enqueue(KIND, {"name": "k"})
        await env.stall_next()  # the worker took the job and died before any commit
        report = await env.jobs.sweep()
        assert env.key("k") in report.reenqueued
        assert (await env.key_row("k")).attempts == 1
        await env.work()
        assert await env.effects("k") == 1
        assert (await env.key_row("k")).outcome == "succeeded"
        assert await env.job_statuses("k") == ["failed", "succeeded"]


async def test_kill_after_commit_before_finish(pg: Pg) -> None:
    async with jobs_env(pg) as env:
        await env.jobs.enqueue(KIND, {"name": "c"})
        job = await env.stall_next()
        # The run committed its effect and outcome, then the worker died before procrastinate finished it.
        await env.execute("INSERT INTO t_effects (key, job_id) VALUES (:k, :j)", k=env.key("c"), j=job.id)
        await env.execute(
            "UPDATE job_keys SET outcome = 'succeeded', job_id = :j, finished_at = now() WHERE key = :k",
            k=env.key("c"),
            j=job.id,
        )
        report = await env.jobs.sweep()
        assert job.id in report.finished
        assert await env.job_statuses("c") == ["succeeded"]
        assert await env.effects("c") == 1
        assert (await env.key_row("c")).attempts == 0


async def test_stall_exhausts_into_dead(pg: Pg) -> None:
    async with jobs_env(pg, max_attempts=1) as env:
        await env.jobs.enqueue(KIND, {"name": "s"})
        await env.stall_next()
        report = await env.jobs.sweep()
        assert env.key("s") in report.dead
        row = await env.key_row("s")
        assert (row.attempts, row.outcome, row.error_code) == (1, "dead", "stalled")
        assert await env.job_statuses("s") == ["failed"]


async def test_stale_heartbeat_lock_held_is_skipped(pg: Pg) -> None:
    from sqlalchemy import text

    async with jobs_env(pg) as env:
        jobs = require_jobs()
        await env.jobs.enqueue(KIND, {"name": "live"})
        await env.stall_next()
        async with env.engine.connect() as holder:
            await holder.execute(text(_LOCK), {"k": jobs.lock_id(env.key("live"))})
            report = await env.jobs.sweep()
            await holder.rollback()
        assert env.key("live") in report.skipped_live
        assert (await env.key_row("live")).attempts == 0
        assert await env.job_statuses("live") == ["doing"]


async def test_unknown_kind_stalled_while_lock_held(pg: Pg) -> None:
    from sqlalchemy import text

    async with jobs_env(pg) as env:
        jobs = require_jobs()
        from reader.jobs.queue import Jobs
        from reader.jobs.worker import make_app

        await env.jobs.enqueue(KIND, {"name": "uk"})
        await env.stall_next()
        other = make_app(target(pg))
        await other.open_async()
        try:
            old = Jobs(env.engine, other, jobs.Catalog([]))  # a worker whose catalog lacks the kind
            async with env.engine.connect() as holder:
                await holder.execute(text(_LOCK), {"k": jobs.lock_id(env.key("uk"))})
                report = await old.sweep()
                await holder.rollback()
        finally:
            await other.close_async()
        assert env.key("uk") in report.skipped_live
        assert (await env.key_row("uk")).outcome is None
        assert await env.job_statuses("uk") == ["doing"]


async def test_real_run_holds_the_lock_then_sigkill(pg: Pg) -> None:
    import json
    import os
    import signal
    import subprocess
    import sys

    async with jobs_env(pg) as env:
        await env.jobs.enqueue(KIND, {"name": "live", "sleep_s": 600})
        child_env = dict(
            os.environ, PYTHONPATH=f"{HERE}{os.pathsep}{SRC}", PYTHONDONTWRITEBYTECODE="1"
        )
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(HERE / "kill_worker.py"),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
        )
        try:
            assert proc.stdin is not None
            spec = {"host": pg.host, "port": pg.port, "user": pg.user, "database": pg.database}
            proc.stdin.write((json.dumps({**spec, "password": pg.password}) + "\n").encode())
            await proc.stdin.drain()
            proc.stdin.close()
            for _ in range(300):
                if await env.job_statuses("live") == ["doing"]:
                    break
                await asyncio.sleep(0.1)
            assert await env.job_statuses("live") == ["doing"]
            await asyncio.sleep(1.0)  # the wrapper takes the key lock, the handler inserts and sleeps
            live = await env.jobs.sweep(stalled_after_s=0)  # "stalled" by definition; the session is alive
            assert env.key("live") in live.skipped_live
            assert (await env.key_row("live")).attempts == 0
            assert (await env.jobs.requeue_dead(KIND, env.key("live"))).kind == "running"
            proc.send_signal(signal.SIGKILL)
            await proc.wait()
            after = None
            for _ in range(200):
                after = await env.jobs.sweep(stalled_after_s=0)
                if env.key("live") in after.reenqueued:
                    break
                await asyncio.sleep(0.1)
            assert after is not None
            assert env.key("live") in after.reenqueued
            assert (await env.key_row("live")).attempts == 1
            assert await env.effects("live") == 0  # the killed run's insert rolled back with its session
            assert await env.job_statuses("live") == ["failed", "todo"]
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


async def test_self_terminating_handler_is_bounded(pg: Pg) -> None:
    """A handler that kills its own session, re-enqueued after every run, still ends dead (FR-EVENTS-14)."""
    import dataclasses

    from sqlalchemy import text

    from .conftest import HANDLER_RUNS, effect_kind

    async def kills_own_session(args: Any, conn: Any, ctx: Any) -> None:
        HANDLER_RUNS.append(ctx.key)
        await conn.execute(text("SELECT pg_terminate_backend(pg_backend_pid())"))

    kind = dataclasses.replace(effect_kind(), handler=kills_own_session)
    async with jobs_env(pg, kinds=[kind]) as env:
        # One enqueue only: every later run comes from the worker's own re-enqueue after a lost run.
        assert (await env.jobs.enqueue(KIND, {"name": "self"})).kind == "queued"
        await env.work()
        row = await env.key_row("self")
        assert (row.attempts, row.outcome, row.error_code) == (3, "dead", "session_lost")
        assert env.runs("self") == 3
        assert await env.job_statuses("self") == ["failed", "failed", "failed"]
        assert (await env.jobs.enqueue(KIND, {"name": "self"})).kind == "dead"


async def test_exhausted_at_claim_never_runs(pg: Pg) -> None:
    async with jobs_env(pg) as env:
        await env.jobs.enqueue(KIND, {"name": "ex"})
        await env.execute("UPDATE job_keys SET attempts = 3 WHERE key = :k", k=env.key("ex"))
        await env.work()
        row = await env.key_row("ex")
        assert (row.attempts, row.outcome, row.error_code) == (3, "dead", "exhausted")
        assert env.runs("ex") == 0


async def test_session_cut_mid_run_then_sweep(pg: Pg) -> None:
    async with jobs_env(pg) as env:
        await env.jobs.enqueue(KIND, {"name": "cut", "sleep_s": 2})
        worker = asyncio.create_task(env.work())
        for _ in range(100):
            if await env.job_statuses("cut") == ["doing"]:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)  # the handler holds the key lock and sleeps
        # The worker's session dies (a network cut, a backend crash) while the worker process lives on.
        await env.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_locks"
            " WHERE locktype = 'advisory' AND granted AND pid <> pg_backend_pid()"
            " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
        )
        report = await env.jobs.sweep(stalled_after_s=0)
        assert env.key("cut") in report.reenqueued
        # The worker wakes, its run fails on the dead session, and its finish of a job the sweep already
        # finished fails inside procrastinate's own task. procrastinate reports that itself (an unretrieved
        # task exception or an error record); this test tolerates it and checks only the key's state.
        # The same in-process worker then runs the job the sweep re-enqueued, which succeeds.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(worker, timeout=20)
        row = await env.key_row("cut")
        assert (row.attempts, row.outcome) == (1, "succeeded")  # the lost run was counted once, not judged
        assert await env.effects("cut") == 1  # the cut run's insert died with its session
        assert env.runs("cut") == 2


async def test_reclaim_after_success_skips(pg: Pg) -> None:
    async with jobs_env(pg) as env:
        await env.jobs.enqueue(KIND, {"name": "r"})
        await env.work()
        # A second job for the key, as a caller racing the success could leave behind.
        deferrer = env.app.configure_task(KIND, lock=env.key("r"), queue="test")
        await deferrer.defer_async(payload={"name": "r"})
        await env.work()
        assert await env.effects("r") == 1
        assert env.runs("r") == 1  # the second job skipped without calling the handler
        assert await env.job_statuses("r") == ["succeeded", "succeeded"]


async def test_finish_after_sweep_finished(pg: Pg) -> None:
    from procrastinate.jobs import Status

    async with jobs_env(pg) as env:
        await env.jobs.enqueue(KIND, {"name": "f"})
        job = await env.stall_next()
        await env.jobs.sweep()
        # The original worker comes back and finishes a job the sweep already finished.
        assert await env.jobs._finish(job, Status.SUCCEEDED) is False
        assert await env.job_statuses("f") == ["failed", "todo"]
        assert (await env.key_row("f")).outcome is None


async def test_dead_then_claim(pg: Pg) -> None:
    async with jobs_env(pg) as env:
        await env.jobs.enqueue(KIND, {"name": "dc", "mode": "permanent"})
        await env.stall_next()
        # A todo job behind the run (lock_mode ordered holds it), claimed after the run ends dead.
        deferrer = env.app.configure_task(KIND, lock=env.key("dc"), queue="test")
        await deferrer.defer_async(payload={"name": "dc"})
        await env.execute(
            "UPDATE job_keys SET outcome = 'dead', error_code = 'RuntimeError', attempts = 1 WHERE key = :k",
            k=env.key("dc"),
        )
        await env.jobs.sweep()
        await env.work()
        assert await env.effects("dc") == 0
        assert env.runs("dc") == 0
        assert (await env.key_row("dc")).outcome == "dead"
        assert await env.job_statuses("dc") == ["failed", "succeeded"]


async def test_enqueue_races_each_transition(pg: Pg) -> None:
    async with jobs_env(pg) as env:
        assert await _results(env, "x") == ["already_queued"] * 4 + ["queued"]
        await env.stall_next()  # running: one todo may be added beside the doing job
        racing = await _results(env, "x")
        assert racing.count("queued") == 1
        assert racing.count("already_queued") == 4
        statuses = await env.job_statuses("x")
        assert statuses.count("doing") == 1
        assert statuses.count("todo") == 1
        await env.jobs.sweep()  # stall counted; the existing todo serves as the retry
        await env.work()
        assert await _results(env, "x") == ["already_done"] * 5
        assert await env.effects("x") == 1
        await env.jobs.enqueue(KIND, {"name": "y", "mode": "permanent"})
        await env.work()
        assert await _results(env, "y") == ["dead"] * 5
        requeues = [(await env.jobs.requeue_dead(KIND, env.key("y"))).kind for _ in range(3)]
        assert requeues == ["queued", "already_queued", "already_queued"]


async def test_requeue_then_sweep(pg: Pg) -> None:
    async with jobs_env(pg) as env:
        await env.jobs.enqueue(KIND, {"name": "q", "mode": "permanent"})
        await env.work()
        assert (await env.key_row("q")).outcome == "dead"
        # requeue_dead's commit happened, then the process died before the defer:
        await env.execute(
            "UPDATE job_keys SET outcome = NULL, error_code = NULL, job_id = NULL, finished_at = NULL,"
            " attempts = 0, updated_at = now() WHERE key = :k",
            k=env.key("q"),
        )
        early = await env.jobs.sweep()
        assert env.key("q") not in early.reenqueued
        await env.age_keys()
        late = await env.jobs.sweep()
        assert env.key("q") in late.reenqueued
        assert (await env.key_row("q")).attempts == 1
        assert (await env.job_statuses("q")).count("todo") == 1
        again = await env.jobs.sweep(reconcile_age_s=0)
        assert env.key("q") not in again.reenqueued


async def test_requeue_without_crash_needs_no_sweep(pg: Pg) -> None:
    async with jobs_env(pg) as env:
        await env.jobs.enqueue(KIND, {"name": "rq", "mode": "permanent"})
        await env.work()
        assert (await env.jobs.requeue_dead(KIND, env.key("rq"))).kind == "queued"
        assert (await env.key_row("rq")).attempts == 0
        report = await env.jobs.sweep(reconcile_age_s=0)
        assert env.key("rq") not in report.reenqueued
        assert (await env.jobs.requeue_dead(KIND, "test.effect:" + "e" * 64)).kind == "unknown_key"


async def test_failed_plus_todo_then_sweep(pg: Pg) -> None:
    async with jobs_env(pg, max_attempts=2) as env:
        key = env.key("ft")
        await env.jobs.enqueue(KIND, {"name": "ft"})
        job = await env.stall_next()
        await env.app.job_manager.finish_job(job, _failed(), delete_job=False)
        deferrer = env.app.configure_task(KIND, lock=key, queueing_lock=key, queue="test")
        await deferrer.defer_async(payload={"name": "ft"})
        await env.age_keys()
        report = await env.jobs.sweep()
        assert key not in report.reenqueued
        assert (await env.key_row("ft")).attempts == 0
        await env.execute("DELETE FROM procrastinate_jobs WHERE status = 'todo'")
        await env.age_keys()
        report = await env.jobs.sweep()
        assert key in report.reenqueued
        assert (await env.key_row("ft")).attempts == 1
        await env.execute("DELETE FROM procrastinate_jobs WHERE status = 'todo'")
        await env.age_keys()
        await env.jobs.sweep()
        row = await env.key_row("ft")
        assert (row.attempts, row.outcome, row.error_code) == (2, "dead", "orphaned")


async def test_shutdown_abort_not_counted(pg: Pg) -> None:
    async with jobs_env(pg) as env:
        await env.jobs.enqueue(KIND, {"name": "ab", "sleep_s": 30})
        worker = asyncio.create_task(
            env.app.run_worker_async(
                queues=env.jobs.queues,
                concurrency=1,
                install_signal_handlers=False,
                listen_notify=False,
                shutdown_graceful_timeout=0.5,
                fetch_job_polling_interval=0.1,
            )
        )
        for _ in range(100):
            if await env.job_statuses("ab") == ["doing"]:
                break
            await asyncio.sleep(0.05)
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        assert await env.job_statuses("ab") == ["aborted"]
        assert (await env.key_row("ab")).attempts == 0
        assert await env.effects("ab") == 0
        await env.age_keys()
        first = await env.jobs.sweep()
        assert env.key("ab") in first.reenqueued
        second = await env.jobs.sweep(reconcile_age_s=0)
        assert env.key("ab") not in second.reenqueued
        assert (await env.key_row("ab")).attempts == 0
        assert await env.job_statuses("ab") == ["aborted", "todo"]


async def test_unknown_kind_and_orphan_jobs(pg: Pg) -> None:
    async with jobs_env(pg) as env:
        unknown = await env.app.configure_task("gone.kind", queue="test").defer_async(payload={})
        report = await env.jobs.sweep()
        assert unknown in report.cancelled
        # A job of a known kind with no key row is finished failed, with no outcome.
        deferrer = env.app.configure_task(KIND, lock=env.key("orphan"), queue="test")
        await deferrer.defer_async(payload={"name": "orphan"})
        await env.work()
        assert await env.job_statuses("orphan") == ["failed"]
        assert await env.key_row("orphan") is None
        assert await env.effects("orphan") == 0
        # A stalled sweep job is finished failed.
        await env.app.configure_task("jobs.sweep", queue="jobs.sweep").defer_async(timestamp=0)
        sweep_job = await env.stall_next()
        report = await env.jobs.sweep()
        assert sweep_job.id in report.finished
        status = await env.scalar("SELECT status::text FROM procrastinate_jobs WHERE id = :i", i=sweep_job.id)
        assert status == "failed"


# ---- final code review (2924dcf): unknown kinds, lost runs, requeue, sweep robustness


async def _claim(env: Any) -> Any:
    """Fetch the next job as a live worker would, without running it (the test calls the wrapper itself)."""
    worker_id = await env.app.job_manager.register_worker()
    job = await env.app.job_manager.fetch_job(queues=None, worker_id=worker_id)
    assert job is not None
    return job


async def _run_claimed(env: Any, kind: Any, job: Any) -> str:
    """Run the wrapper on a claimed job; returns the JobFailed code. The job stays `doing` until finished."""
    from types import SimpleNamespace

    jobs = require_jobs()
    with pytest.raises(jobs.JobFailed) as failed:
        await env.jobs._run(kind, SimpleNamespace(job=job), job.task_kwargs["payload"])
    await env.app.job_manager.finish_job(job, _failed(), delete_job=False)
    return str(failed.value.code)


async def _other_version(env: Any, pg: Pg) -> Any:
    """A Jobs runtime of a worker version whose catalog lacks the test kind (a deploy rollback)."""
    from reader.jobs.queue import Jobs
    from reader.jobs.worker import make_app

    other = make_app(target(pg))
    await other.open_async()
    return other, Jobs(env.engine, other, require_jobs().Catalog([]))


async def test_unknown_kind_stalled_with_the_lock_free_is_dead(pg: Pg) -> None:
    """FR-EVENTS-7: a stalled job of a kind this worker does not know, lock free, gets dead unknown_kind."""
    async with jobs_env(pg) as env:
        await env.jobs.enqueue(KIND, {"name": "ukd"})
        job = await env.stall_next()
        other, old = await _other_version(env, pg)
        try:
            report = await old.sweep()
        finally:
            await other.close_async()
        assert job.id in report.finished
        row = await env.key_row("ukd")
        assert (row.outcome, row.error_code) == ("dead", "unknown_kind")
        assert await env.job_statuses("ukd") == ["failed"]


async def test_reconcile_skips_keys_of_an_unknown_kind(pg: Pg) -> None:
    """FR-EVENTS-15: after a rollback-style cancel, reconcile never marks an unknown kind's key dead."""
    async with jobs_env(pg) as env:
        await env.jobs.enqueue(KIND, {"name": "rb"})
        other, old = await _other_version(env, pg)
        try:
            await old.sweep()  # cancels the todo job of a kind it does not know
            assert await env.job_statuses("rb") == ["cancelled"]
            await env.age_keys()  # the key row is now older than 60 s
            later = await old.sweep()
            again = await old.sweep(reconcile_age_s=0)
        finally:
            await other.close_async()
        assert env.key("rb") not in later.dead
        assert env.key("rb") not in again.dead
        row = await env.key_row("rb")
        assert (row.attempts, row.outcome, row.error_code) == (0, None, None)


async def test_wrapper_error_counts_one_attempt_and_reenqueues(pg: Pg, monkeypatch: Any) -> None:
    """FR-EVENTS-4: a failure of the wrapper itself (here _judge) is counted once; the key is queued again."""
    async with jobs_env(pg) as env:
        await env.jobs.enqueue(KIND, {"name": "we", "mode": "permanent"})
        job = await _claim(env)

        async def broken(*_args: Any, **_kw: Any) -> None:
            raise RuntimeError("judge unavailable")

        monkeypatch.setattr(env.jobs, "_judge", broken)
        assert await _run_claimed(env, env.kind, job) == "wrapper_error"
        row = await env.key_row("we")
        assert (row.attempts, row.outcome) == (1, None)
        assert await env.job_statuses("we") == ["failed", "todo"]


async def test_lost_run_is_reenqueued_by_the_worker(pg: Pg) -> None:
    """FR-EVENTS-4: the worker itself counts a run whose session died and enqueues the key again."""
    import dataclasses

    from sqlalchemy import text

    from .conftest import effect_kind

    async def kills_own_session(args: Any, conn: Any, ctx: Any) -> None:
        await conn.execute(text("SELECT pg_terminate_backend(pg_backend_pid())"))

    kind = dataclasses.replace(effect_kind(), handler=kills_own_session)
    async with jobs_env(pg, kinds=[kind]) as env:
        await env.jobs.enqueue(KIND, {"name": "lost"})
        job = await _claim(env)
        assert await _run_claimed(env, kind, job) == "session_lost"
        row = await env.key_row("lost")
        assert (row.attempts, row.outcome) == (1, None)
        assert await env.job_statuses("lost") == ["failed", "todo"]


async def test_requeue_dead_on_a_key_without_outcome_or_job(pg: Pg) -> None:
    """FR-EVENTS-8: with no outcome, requeue_dead returns FR-EVENTS-3's result for the stored args."""
    async with jobs_env(pg) as env:
        await env.jobs.enqueue(KIND, {"name": "nj"})
        await env.execute("DELETE FROM procrastinate_jobs WHERE lock = :k", k=env.key("nj"))
        assert (await env.jobs.requeue_dead(KIND, env.key("nj"))).kind == "queued"
        assert await env.job_statuses("nj") == ["todo"]
        assert (await env.key_row("nj")).attempts == 0


async def test_sweep_job_is_stored_with_the_sweep_priority(pg: Pg) -> None:
    """FR-EVENTS-7: jobs.sweep runs with the highest priority and its own queueing lock."""
    from reader.jobs.catalog import SWEEP_PRIORITY

    async with jobs_env(pg) as env:
        await env.app.tasks["jobs.sweep"].defer_async(timestamp=0)
        rows = await env.rows(
            "SELECT priority, queueing_lock FROM procrastinate_jobs WHERE task_name = 'jobs.sweep'"
        )
        assert [tuple(r) for r in rows] == [(SWEEP_PRIORITY, "jobs.sweep")]


async def test_a_failing_enqueue_does_not_end_the_sweep_round(pg: Pg, monkeypatch: Any) -> None:
    """FR-EVENTS-15: one failing re-enqueue is logged and the round goes on; the key is taken later."""
    async with jobs_env(pg) as env:
        for name in ("d1", "d2"):
            await env.jobs.enqueue(KIND, {"name": name})
        await env.execute(
            "DELETE FROM procrastinate_jobs WHERE lock IN (:a, :b)", a=env.key("d1"), b=env.key("d2")
        )
        await env.age_keys()
        real = env.jobs._defer
        calls: list[str] = []

        async def first_fails(kind: Any, key: str, *args: Any, **kw: Any) -> Any:
            calls.append(key)
            if len(calls) == 1:
                raise RuntimeError("defer unavailable")
            return await real(kind, key, *args, **kw)

        monkeypatch.setattr(env.jobs, "_defer", first_fails)
        report = await env.jobs.sweep()
        assert len(calls) == 2
        assert report.reenqueued == [calls[1]]
        monkeypatch.setattr(env.jobs, "_defer", real)
        await env.age_keys()
        later = await env.jobs.sweep()
        assert later.reenqueued == [calls[0]]


# ---- FR-EVENTS-1, -9: start hooks and running()


async def _until(predicate: Any, timeout_s: float = 20.0) -> None:
    with anyio.fail_after(timeout_s):  # polls database state: there is no event to wait on
        while not await predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.05)


async def test_start_hook_runs_once_after_the_start_sweep_with_running_set(pg: Pg) -> None:
    jobs = require_jobs()
    from reader.jobs.worker import run, running

    with pytest.raises(jobs.JobError) as before:
        running()
    assert before.value.code == "no_worker"
    async with jobs_env(pg) as env:
        await env.jobs.enqueue(KIND, {"name": "hk"})
        await env.stall_next()  # the start sweep finishes this job failed and enqueues the key again
        seen: list[tuple[bool, list[str]]] = []

        async def hook(given: Any) -> None:
            seen.append((given is running(), await env.job_statuses("hk")))

        stop = asyncio.Event()
        task = asyncio.create_task(run(env.engine, target(pg), stop, env.jobs.catalog, hooks=[hook]))

        async def served() -> bool:
            return (await env.key_row("hk")).outcome == "succeeded"

        await _until(served)
        stop.set()
        with anyio.fail_after(15):
            assert await task == 0
    assert len(seen) == 1
    is_running, statuses = seen[0]
    assert is_running
    assert statuses[0] == "failed"  # the stalled job was swept before the hook ran
    with pytest.raises(jobs.JobError) as after:
        running()  # cleared in run's finally
    assert after.value.code == "no_worker"


async def test_a_failing_hook_does_not_stop_the_next_hook_or_the_worker(pg: Pg) -> None:
    from reader.jobs.worker import run

    async with jobs_env(pg) as env:
        await env.jobs.enqueue(KIND, {"name": "after"})
        calls: list[str] = []

        async def bad(_jobs: Any) -> None:
            calls.append("bad")
            raise RuntimeError("hook failed")

        async def good(_jobs: Any) -> None:
            calls.append("good")

        stop = asyncio.Event()
        task = asyncio.create_task(run(env.engine, target(pg), stop, env.jobs.catalog, hooks=[bad, good]))

        async def served() -> bool:
            return (await env.key_row("after")).outcome == "succeeded"

        await _until(served)
        stop.set()
        with anyio.fail_after(15):
            assert await task == 0
        assert calls == ["bad", "good"]


async def test_hook_timeout_and_stop_during_a_hook(pg: Pg, monkeypatch: Any) -> None:
    from reader.jobs import worker

    async with jobs_env(pg) as env:
        monkeypatch.setattr(worker, "HOOK_TIMEOUT_S", 0.2)
        await env.jobs.enqueue(KIND, {"name": "slow"})

        async def hangs(_jobs: Any) -> None:
            await asyncio.sleep(3600)

        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(env.engine, target(pg), stop, env.jobs.catalog, hooks=[hangs]))

        async def served() -> bool:
            return (await env.key_row("slow")).outcome == "succeeded"

        await _until(served)  # the hook timed out and the worker served
        stop.set()
        with anyio.fail_after(15):
            assert await task == 0

        monkeypatch.setattr(worker, "HOOK_TIMEOUT_S", 3600.0)
        started = asyncio.Event()

        async def waits(_jobs: Any) -> None:
            started.set()
            await asyncio.sleep(3600)

        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(env.engine, target(pg), stop, env.jobs.catalog, hooks=[waits]))
        with anyio.fail_after(15):
            await started.wait()
        stop.set()
        with anyio.fail_after(5):
            assert await task == 0  # stop is observed inside a hook


async def test_run_without_hooks_awaits_the_declared_start_hooks(pg: Pg, monkeypatch: Any) -> None:
    """FR-EVENTS-1: the production default `hooks=None` runs `start_hooks()`'s hooks, each once."""
    from reader.jobs import worker

    calls: list[Any] = []

    async def recording(given: Any) -> None:
        calls.append(given)

    monkeypatch.setattr(worker, "start_hooks", lambda: [recording])
    async with jobs_env(pg) as env:
        await env.jobs.enqueue(KIND, {"name": "dflt"})
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(env.engine, target(pg), stop, env.jobs.catalog))

        async def served() -> bool:
            return (await env.key_row("dflt")).outcome == "succeeded"

        await _until(served)
        stop.set()
        with anyio.fail_after(15):
            assert await task == 0
    assert len(calls) == 1
    assert calls[0].catalog is not None  # the worker's Jobs


async def test_a_failing_start_sweep_returns_1(pg: Pg, monkeypatch: Any) -> None:
    from reader.jobs.queue import Jobs
    from reader.jobs.worker import run

    async def broken(self: Any, *_args: Any, **_kw: Any) -> Any:
        raise RuntimeError("sweep failed")

    async with jobs_env(pg) as env:
        monkeypatch.setattr(Jobs, "sweep", broken)
        with anyio.fail_after(15):
            assert await run(env.engine, target(pg), asyncio.Event(), env.jobs.catalog, hooks=()) == 1

        # A stop that arrives while the start sweep fails is a stop: 0, not the failure's 1 (FR-EVENTS-9).
        stop = asyncio.Event()

        async def stopped_then_broken(self: Any, *_args: Any, **_kw: Any) -> Any:
            stop.set()
            raise RuntimeError("sweep failed")

        monkeypatch.setattr(Jobs, "sweep", stopped_then_broken)
        with anyio.fail_after(15):
            assert await run(env.engine, target(pg), stop, env.jobs.catalog, hooks=()) == 0


async def test_a_hook_that_cancels_itself_does_not_end_the_worker(pg: Pg) -> None:
    """FR-EVENTS-1: hook failure is isolated, a self-cancelled hook included; only `stop` ends the start."""
    from reader.jobs.worker import run

    async with jobs_env(pg) as env:
        await env.jobs.enqueue(KIND, {"name": "selfcancel"})
        calls: list[str] = []

        async def cancels_itself(_jobs: Any) -> None:
            calls.append("cancels")
            raise asyncio.CancelledError

        async def next_hook(_jobs: Any) -> None:
            calls.append("next")

        stop = asyncio.Event()
        task = asyncio.create_task(
            run(env.engine, target(pg), stop, env.jobs.catalog, hooks=[cancels_itself, next_hook])
        )

        async def served() -> bool:
            return (await env.key_row("selfcancel")).outcome == "succeeded"

        await _until(served)
        stop.set()
        with anyio.fail_after(15):
            assert await task == 0
        assert calls == ["cancels", "next"]


# ---- FR-EVENTS-9: the worker entry


async def test_worker_run_returns_0_after_stop_and_1_when_unreachable(pg: Pg) -> None:
    import dataclasses

    require_jobs()
    from reader.db import make_engine
    from reader.jobs.worker import run

    async with jobs_env(pg) as env:
        stop = asyncio.Event()
        task = asyncio.create_task(run(env.engine, target(pg), stop, env.jobs.catalog, hooks=()))
        await asyncio.sleep(1.0)
        stop.set()
        with anyio.fail_after(15):
            assert await task == 0
    bad = dataclasses.replace(target(pg), port=1)
    engine = make_engine(bad)
    try:
        with anyio.fail_after(30):
            assert await run(engine, bad, asyncio.Event(), hooks=()) == 1
    finally:
        await engine.dispose()


async def test_worker_run_stop_with_running_job(pg: Pg) -> None:
    import time

    require_jobs()
    from reader.jobs.worker import run

    capture = _Capture()
    root = logging.getLogger()
    level = root.level
    root.addHandler(capture)
    root.setLevel(logging.DEBUG)
    try:
        async with jobs_env(pg) as env:
            await env.jobs.enqueue(KIND, {"name": "stopme", "sleep_s": 60})
            stop = asyncio.Event()
            task = asyncio.create_task(run(env.engine, target(pg), stop, env.jobs.catalog, hooks=()))
            for _ in range(200):
                if await env.job_statuses("stopme") == ["doing"]:
                    break
                await asyncio.sleep(0.05)
            assert await env.job_statuses("stopme") == ["doing"]
            started = time.monotonic()
            stop.set()
            with anyio.fail_after(20):
                code = await task
            elapsed = time.monotonic() - started
            assert code == 0
            assert elapsed < 10  # graceful 8 s, then abort: inside Compose's 10 s stop timeout
            assert await env.job_statuses("stopme") == ["aborted"]
            assert (await env.key_row("stopme")).attempts == 0
    finally:
        root.removeHandler(capture)
        root.setLevel(level)
    lines = [r for r in capture.records if r.startswith("procrastinate")]
    assert any("Waiting for job to finish" in r for r in lines)
    assert all("stopme" not in r for r in lines)  # the payload never reaches a procrastinate record


# ---- INV-EVENTS-5, FR-EVENTS-10, -16: secrets and logs


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        job = getattr(record, "job", "")
        self.records.append(f"{record.name} {record.getMessage()} {job} {record.exc_info}")


async def test_canary_secret_never_logged(pg: Pg) -> None:
    from reader.config import Secret

    canary = "CANARY-jobs-" + "w" * 12
    marker = "PAYLOAD-MARKER-" + "v" * 6
    capture = _Capture()
    root = logging.getLogger()
    level = root.level
    root.addHandler(capture)
    root.setLevel(logging.DEBUG)
    try:
        async with jobs_env(pg, secret_values=(canary, pg.password)) as env:
            from reader.jobs.logfilter import install_log_filter

            jobs = require_jobs()
            install_log_filter()
            for bad in ({"name": canary}, {"name": f"a{canary}b"}, {"name": "n", "message": Secret(canary)}):
                with pytest.raises(jobs.JobError) as caught:
                    await env.jobs.enqueue(KIND, bad)
                assert caught.value.code == "secret_in_payload"
            # The handler's error message carries the canary, a secret it holds from outside its payload.
            HANDLER_SECRET[0] = canary
            await env.jobs.enqueue(KIND, {"name": marker, "mode": "permanent", "message": "@secret"})
            await env.jobs.enqueue(KIND, {"name": "t2", "mode": "transient", "message": "@secret"})
            await env.work()
            for table in ("procrastinate_jobs", "job_keys", "procrastinate_events"):
                sql = f"SELECT coalesce(string_agg(t::text, ' '), '') FROM {table} t"  # noqa: S608
                dump = await env.scalar(sql)
                assert canary not in dump, table
                assert pg.password not in dump, table
    finally:
        HANDLER_SECRET[0] = ""
        root.removeHandler(capture)
        root.setLevel(level)
    text = "\n".join(capture.records)
    assert capture.records
    assert canary not in text
    assert pg.password not in text
    procrastinate_lines = [r for r in capture.records if r.startswith("procrastinate")]
    assert procrastinate_lines
    assert all(marker not in r for r in procrastinate_lines)
