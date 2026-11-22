"""SRV-EVENTS [db]: enqueue, the worker wrapper, the sweep, requeue and status against Postgres.

@trace FR-EVENTS-3 FR-EVENTS-4 FR-EVENTS-5 FR-EVENTS-6 FR-EVENTS-7 FR-EVENTS-8 FR-EVENTS-9 FR-EVENTS-11
@trace FR-EVENTS-12 FR-EVENTS-13 FR-EVENTS-14 FR-EVENTS-15 FR-EVENTS-10 FR-EVENTS-16
@trace INV-EVENTS-1 INV-EVENTS-2 INV-EVENTS-3 INV-EVENTS-4 INV-EVENTS-5 INV-EVENTS-7
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import anyio
import pytest

from .conftest import HANDLER_SECRET, KIND, Pg, jobs_env, require_jobs, target

pytestmark = pytest.mark.anyio

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
        assert "jobs_0001" in versions
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


# ---- FR-EVENTS-9: the worker entry


async def test_worker_run_returns_0_after_stop_and_1_when_unreachable(pg: Pg) -> None:
    import dataclasses

    require_jobs()
    from reader.db import make_engine
    from reader.jobs.worker import run

    async with jobs_env(pg) as env:
        stop = asyncio.Event()
        task = asyncio.create_task(run(env.engine, target(pg), stop, env.jobs.catalog))
        await asyncio.sleep(1.0)
        stop.set()
        with anyio.fail_after(15):
            assert await task == 0
    bad = dataclasses.replace(target(pg), port=1)
    engine = make_engine(bad)
    try:
        with anyio.fail_after(30):
            assert await run(engine, bad, asyncio.Event()) == 1
    finally:
        await engine.dispose()


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
