"""SRV-EVENTS [db]: the main and judge lanes (FR-EVENTS-9 amended for ADR-0039 B7b), the handler's own connections
(FR-EVENTS-1 c), the reserved periodic tasks (FR-EVENTS-15 amended), the judge lane's reaper (FR-EVENTS-34) and lane
separation (INV-EVENTS-9). Real `worker.run` tasks in process, plus one killed subprocess lane.

@trace FR-EVENTS-9 FR-EVENTS-1 FR-EVENTS-15 FR-EVENTS-34 INV-EVENTS-9
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import signal
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest

from .conftest import (
    JUDGE_ACTIVE,
    JUDGE_GATES,
    JUDGE_RUNS,
    KIND,
    Env,
    Pg,
    effect_kind,
    jobs_env,
    judge_stub_kind,
    require_jobs,
    target,
)

pytestmark = pytest.mark.anyio
HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / "src"
JUDGE = "judge.find"
HOLD = "test.hold"  # a main-queue stub, to observe the main lane's concurrency


@pytest.fixture(autouse=True)
def _reset() -> None:
    JUDGE_RUNS.clear()
    JUDGE_ACTIVE.clear()
    JUDGE_GATES.clear()


def _kinds() -> list[Any]:
    return [effect_kind(), judge_stub_kind(), judge_stub_kind(HOLD, "test")]


def _key(env: Env, name: str, request_id: str) -> str:
    return str(require_jobs().idempotency_key(env.jobs.catalog[name], {"request_id": request_id}))


async def _until(predicate: Any, timeout_s: float = 20.0) -> None:
    with anyio.fail_after(timeout_s):
        while not await predicate():  # noqa: ASYNC110 (polls database state)
            await asyncio.sleep(0.05)


async def _outcome(env: Env, key: str) -> Any:
    rows = await env.rows("SELECT outcome, error_code FROM job_keys WHERE key = :k", k=key)
    return tuple(rows[0]) if rows else None


@asynccontextmanager
async def _lane(env: Env, lane: str, **kw: Any) -> AsyncIterator[asyncio.Task[int]]:
    from reader.jobs.worker import run

    stop = asyncio.Event()
    task = asyncio.create_task(run(env.engine, target(env.pg), stop, env.jobs.catalog, hooks=(), lane=lane, **kw))
    try:
        yield task
    finally:
        stop.set()
        with anyio.fail_after(20):
            await task


async def test_each_lane_serves_only_its_queues_and_the_judge_lane_runs_no_start_sweep(
    pg: Pg, monkeypatch: Any
) -> None:
    from reader.jobs.queue import Jobs

    sweeps: list[str] = []
    original = Jobs.sweep

    async def counted(self: Any, *a: Any, **kw: Any) -> Any:
        sweeps.append(self.lane.name)
        return await original(self, *a, **kw)

    monkeypatch.setattr(Jobs, "sweep", counted)
    async with jobs_env(pg, kinds=_kinds()) as env:
        await env.jobs.enqueue(JUDGE, {"request_id": "r1"})
        await env.jobs.enqueue(KIND, {"name": "m1"})
        async with _lane(env, "judge"):
            await _until(lambda: _done(env, _key(env, JUDGE, "r1")))
            await asyncio.sleep(1.0)
            assert await env.key_row("m1") is not None
            assert (await env.key_row("m1")).outcome is None  # the main queue is not the judge lane's
            assert await env.job_statuses("m1") == ["todo"]
        assert sweeps == []  # no start sweep on the judge lane
        await env.jobs.enqueue(JUDGE, {"request_id": "r2"})
        async with _lane(env, "main"):
            await _until(lambda: _effect_done(env, "m1"))
            await asyncio.sleep(1.0)
            assert await _outcome(env, _key(env, JUDGE, "r2")) == (None, None)  # not the main lane's
        assert sweeps[:1] == ["main"]


async def _done(env: Env, key: str) -> bool:
    return await _outcome(env, key) == ("succeeded", None)


async def _effect_done(env: Env, name: str) -> bool:
    row = await env.key_row(name)
    return row is not None and row.outcome == "succeeded"


async def test_concurrency_four_on_the_judge_lane_and_one_on_main(pg: Pg) -> None:
    async with jobs_env(pg, kinds=_kinds()) as env:
        for i in range(5):
            await env.jobs.enqueue(JUDGE, {"request_id": f"j{i}", "hold": "g"})
        for i in range(3):
            await env.jobs.enqueue(HOLD, {"request_id": f"h{i}", "sleep_s": 0.3})
        async with _lane(env, "judge"), _lane(env, "main"):

            async def four() -> bool:
                return JUDGE_ACTIVE.get("judge", [0, 0])[0] == 4

            await _until(four)
            await asyncio.sleep(0.5)
            assert JUDGE_ACTIVE["judge"] == [4, 4]  # the fifth waits
            JUDGE_GATES["g"].set()

            async def all_done() -> bool:
                keys = [_key(env, JUDGE, f"j{i}") for i in range(5)] + [_key(env, HOLD, f"h{i}") for i in range(3)]
                return all([await _done(env, k) for k in keys])

            await _until(all_done, 30)
        assert JUDGE_ACTIVE["test"][1] == 1  # main never ran two at once
        assert JUDGE_ACTIVE["judge"][1] == 4


async def test_an_idle_judge_lane_wakes_on_notify(pg: Pg) -> None:
    async with jobs_env(pg, kinds=_kinds()) as env:
        async with _lane(env, "judge"):
            await asyncio.sleep(2.0)  # idle: listening
            await env.jobs.enqueue(JUDGE, {"request_id": "wake"})
            with anyio.fail_after(1.0):  # procrastinate's polling interval is 5 s: only the notify is this fast
                while "wake" not in JUDGE_RUNS:  # noqa: ASYNC110
                    await asyncio.sleep(0.02)


async def test_own_connection_write_survives_a_failed_run(pg: Pg) -> None:
    async with jobs_env(pg, kinds=_kinds()) as env:
        await env.jobs.enqueue(JUDGE, {"request_id": "own", "write": True, "fail": True})
        async with _lane(env, "judge"):
            await _until(lambda: _dead(env, _key(env, JUDGE, "own")))
        count = await env.scalar("SELECT count(*) FROM t_effects WHERE key = 'own'")
        assert count == 1  # committed on its own connection; the run's transaction rolled back


async def _dead(env: Env, key: str) -> bool:
    row = await _outcome(env, key)
    return row is not None and row[0] == "dead"


async def test_four_runs_stay_inside_both_pools(pg: Pg, monkeypatch: Any) -> None:
    from sqlalchemy import event

    from reader.jobs import worker

    apps: list[Any] = []
    make_app = worker.make_app

    def capture(t: Any, pool_size: int = worker.POOL_MAX_SIZE) -> Any:
        app = make_app(t, pool_size)
        apps.append(app)
        return app

    monkeypatch.setattr(worker, "make_app", capture)
    async with jobs_env(pg, kinds=_kinds()) as env:
        out: list[int] = [0, 0]  # [now, peak] checked out of the SQLAlchemy pool

        def checkout(*_: Any) -> None:
            out[0] += 1
            out[1] = max(out)

        def checkin(*_: Any) -> None:
            out[0] -= 1

        event.listen(env.engine.sync_engine, "checkout", checkout)
        event.listen(env.engine.sync_engine, "checkin", checkin)
        for i in range(4):
            await env.jobs.enqueue(JUDGE, {"request_id": f"p{i}", "hold": "p", "write": True})
        async with _lane(env, "judge"):

            async def four() -> bool:
                return JUDGE_ACTIVE.get("judge", [0, 0])[0] == 4

            await _until(four)
            pool = apps[-1].connector.pool  # psycopg_pool's AsyncConnectionPool
            stats = pool.get_stats()
            assert stats.get("requests_waiting", 0) == 0
            assert stats.get("requests_errors", 0) == 0  # no PoolTimeout was raised to a requester
            assert pool.max_size == 8
            JUDGE_GATES["p"].set()
            await _until(lambda: _done(env, _key(env, JUDGE, "p3")))
        # The lane's 2 per run (8), plus this test's own polling connection.
        assert out[1] <= 9


async def test_sweep_never_cancels_reserved_periodic_tasks(pg: Pg) -> None:
    from reader.jobs.model import JUDGE_GC_TASK, JUDGE_REAP_TASK

    periodic = dataclasses.replace(effect_kind(), periodic=("0 0 1 1 *", lambda _t: {"name": "tick"}))
    async with jobs_env(pg, kinds=[periodic, judge_stub_kind()]) as env:
        names = [JUDGE_REAP_TASK, JUDGE_GC_TASK, f"{KIND}.tick"]
        for name in names:
            await env.app.configure_task(name=name).defer_async(timestamp=0)
        await env.app.configure_task(name="x.unknown", queue="test", allow_unknown=True).defer_async()
        report = await env.jobs.sweep()
        rows = await env.rows("SELECT task_name, status::text FROM procrastinate_jobs ORDER BY id")
        status = {r[0]: r[1] for r in rows}
        assert {n: status[n] for n in names} == dict.fromkeys(names, "todo")
        assert status["x.unknown"] == "cancelled"
        assert len(report.cancelled) == 1


async def test_a_stalled_collector_is_finished_failed_without_a_key(pg: Pg) -> None:
    from reader.jobs.model import JUDGE_GC_TASK, JUDGE_REAP_TASK

    async with jobs_env(pg, kinds=[effect_kind(), judge_stub_kind()]) as env:
        for name in (JUDGE_REAP_TASK, JUDGE_GC_TASK):
            await env.app.configure_task(name=name).defer_async(timestamp=0)
            job = await env.stall_next()
            await env.jobs.sweep(stalled_after_s=0)
            rows = await env.rows("SELECT status::text FROM procrastinate_jobs WHERE id = :i", i=job.id)
            assert rows[0][0] == "failed"
        assert await env.scalar("SELECT count(*) FROM job_keys") == 0


async def test_judge_gc_runs_on_an_idle_main_lane(pg: Pg) -> None:
    async with jobs_env(pg, kinds=_kinds()) as env, _lane(env, "main"), _lane(env, "judge"):

        async def gc_succeeded() -> bool:
            n = await env.scalar(
                "SELECT count(*) FROM procrastinate_jobs WHERE task_name = 'jobs.judge_gc' AND status = 'succeeded'"
            )
            return bool(n)

        await _until(gc_succeeded, 130)


async def test_the_judge_lane_reaps_while_main_is_busy(pg: Pg) -> None:
    async with jobs_env(pg, kinds=_kinds()) as env:
        owner = await _user(env)
        await env.jobs.enqueue(HOLD, {"request_id": "busy", "hold": "busy"})
        await env.jobs.enqueue(JUDGE, {"request_id": "stuck"})
        # Fetched before any lane runs (so no periodic task is ahead of it), by a worker that dies later.
        dead_worker = await _fetch_judge(env)
        async with _lane(env, "main"):

            async def busy() -> bool:
                return JUDGE_ACTIVE.get("test", [0, 0])[0] == 1

            await _until(busy)
            await env.execute(  # the fetching worker is now dead: a judge job under a stale worker
                "UPDATE procrastinate_workers SET last_heartbeat = now() - interval '1 hour' WHERE id = :w",
                w=dead_worker,
            )
            await _expired_row(env, owner, 61)
            async with _lane(env, "judge"):
                seen: list[Any] = []

                async def reaped() -> bool:
                    gone = await env.scalar("SELECT count(*) FROM judge_requests") == 0
                    outcome = await _outcome(env, _key(env, JUDGE, "stuck"))
                    jobs = await env.rows("SELECT task_name, queue_name, status::text FROM procrastinate_jobs")
                    seen[:] = [gone, outcome, jobs]
                    return gone and outcome == ("dead", "stalled")

                try:
                    await _until(reaped, 75)
                except TimeoutError:
                    pytest.fail(f"not reaped: {seen}")
            JUDGE_GATES["busy"].set()


async def _fetch_judge(env: Env) -> int:
    """Fetch the judge queue's next job under a registered worker (as that worker would); return its id."""
    worker_id = int(await env.app.job_manager.register_worker())
    job = await env.app.job_manager.fetch_job(queues=["judge"], worker_id=worker_id)
    assert job is not None and job.task_name == JUDGE
    return worker_id


async def _user(env: Env) -> uuid.UUID:
    from .test_judge_requests_db import _user as user

    return await user(env)


async def _expired_row(env: Env, owner: uuid.UUID, seconds_past: int) -> uuid.UUID:
    rid = uuid.uuid4()
    await env.execute(
        "INSERT INTO judge_requests (request_id, owner_id, line, run_id, run_cap_usd, input_sha256, created_at,"
        " deadline, query, cards, pack_ids) VALUES (:r, :o, 'tutor', gen_random_uuid(), 0.5, repeat('a', 64),"
        " now() - make_interval(secs => :p + 5), now() - make_interval(secs => :p), 'q', '[[\"c\", \"t\"]]',"
        " ARRAY['p1'])",
        r=rid, o=owner, p=seconds_past,
    )
    return rid


async def test_a_killed_judge_lane_ends_its_key_dead_without_a_second_run(pg: Pg) -> None:
    async with jobs_env(pg, kinds=_kinds()) as env:
        await env.jobs.enqueue(JUDGE, {"request_id": "killed", "sleep_s": 600})
        child_env = dict(os.environ, PYTHONPATH=f"{HERE}{os.pathsep}{SRC}", PYTHONDONTWRITEBYTECODE="1")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(HERE / "kill_lane_worker.py"), stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=child_env,
        )
        key = _key(env, JUDGE, "killed")
        try:
            assert proc.stdin is not None
            spec = {"host": pg.host, "port": pg.port, "user": pg.user, "database": pg.database}
            proc.stdin.write((json.dumps({**spec, "password": pg.password}) + "\n").encode())
            await proc.stdin.drain()
            proc.stdin.close()

            async def doing() -> bool:
                return await env.rows("SELECT status::text FROM procrastinate_jobs WHERE lock = :k", k=key) == [
                    ("doing",)
                ]

            await _until(doing, 30)
            await asyncio.sleep(1.0)
            proc.send_signal(signal.SIGKILL)
            await proc.wait()

            async def judged() -> bool:
                await env.jobs.judge_reap(stalled_after_s=0)
                return await _outcome(env, key) == ("dead", "stalled")

            await _until(judged, 30)
            statuses = [r[0] for r in await env.rows(
                "SELECT status::text FROM procrastinate_jobs WHERE lock = :k ORDER BY id", k=key
            )]
            assert statuses == ["failed"]  # never re-enqueued
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
