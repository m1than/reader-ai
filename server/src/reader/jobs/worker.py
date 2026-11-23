"""The worker loop: the start sweep, the start hooks, then procrastinate serving every declared kind
(SRV-EVENTS FR-EVENTS-1, FR-EVENTS-9).

`reader-server --role worker` calls `run` once the schema is at all heads.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Iterable, Sequence
from typing import Final

import procrastinate
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reader.db import DbTarget
from reader.jobs.catalog import Catalog, StartHook, load_kinds, start_hooks
from reader.jobs.logfilter import install_log_filter
from reader.jobs.model import JobError
from reader.jobs.queue import Jobs

log = logging.getLogger("reader.jobs")

CONCURRENCY: Final = 1
# A chosen bound (FR-EVENTS-1): a start hook only enqueues, which takes milliseconds per key.
HOOK_TIMEOUT_S: float = 30.0
_RUNNING: list[Jobs | None] = [None]


def running() -> Jobs:
    """The worker's `Jobs`, for a handler that enqueues a follow-up job (paired SRV-PIPELINE amendment).

    Set only while `run` serves (from the start hooks on); outside it, `JobError("no_worker")`."""
    jobs = _RUNNING[0]
    if jobs is None:
        raise JobError("no_worker")
    return jobs


SHUTDOWN_GRACEFUL_TIMEOUT_S: Final = 8.0
POOL_MAX_SIZE: Final = 4


def make_app(target: DbTarget) -> procrastinate.App:
    """procrastinate on psycopg 3; the password goes in as a connect argument, never in a conninfo string."""
    connector = procrastinate.PsycopgConnector(
        conninfo="",
        kwargs={
            "host": target.host,
            "port": target.port,
            "user": target.user,
            "dbname": target.database,
            "password": target.password.reveal(),
        },
        min_size=1,
        max_size=POOL_MAX_SIZE,
    )
    return procrastinate.App(connector=connector)


async def _unless_stopped[T](work: Awaitable[T], stop: asyncio.Event) -> tuple[bool, T | None]:
    """Await `work` unless `stop` is set first, then cancel it. Returns (stopped, result); raises its
    error."""
    task = asyncio.ensure_future(work)
    stopping = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({task, stopping}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopping.cancel()
        if not task.done():  # stopped, or this coroutine itself was cancelled: the work never outlives it
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    if task.cancelled():
        return True, None
    return False, task.result()


async def _start_hooks(jobs: Jobs, hooks: Sequence[StartHook], stop: asyncio.Event) -> bool:
    """Each hook once, in order, isolated and bounded (FR-EVENTS-1). False when `stop` was set meanwhile."""
    for hook in hooks:
        if stop.is_set():
            return False
        try:
            stopped, _ = await _unless_stopped(asyncio.wait_for(hook(jobs), HOOK_TIMEOUT_S), stop)
        except Exception:  # an error or the timeout: logged by module only; the next hook still runs
            log.warning("jobs: start hook failed: %s", getattr(hook, "__module__", "?"))
            continue
        if stopped:
            return False
    return not stop.is_set()


async def run(
    engine: AsyncEngine,
    target: DbTarget,
    stop: asyncio.Event,
    catalog: Catalog | None = None,
    secret_values: Iterable[str] = (),
    hooks: Sequence[StartHook] | None = None,
) -> int:
    """Serve jobs until `stop` is set. Returns 0 after a stop, 1 when the database is unreachable at start or
    the start sweep fails (0 when `stop` is already set by then).

    `secret_values` are every resolved secret the calling process holds (FR-EVENTS-10); the database
    password is always among them. `hooks` are the start hooks (None: every declared module's
    `on_worker_start`, FR-EVENTS-1).
    """
    install_log_filter()  # before procrastinate logs anything
    try:
        async with engine.connect() as conn:  # fails at once; psycopg's pool would wait to fill
            await conn.execute(text("SELECT 1"))
        app = make_app(target)
        await app.open_async()
    except Exception:  # driver detail is not logged (INV-EVENTS-5)
        log.warning("jobs: database unreachable at start")
        return 1
    try:
        kinds = catalog if catalog is not None else load_kinds()
        jobs = Jobs(engine, app, kinds, [target.password.reveal(), *secret_values])
        try:
            stopped, _ = await _unless_stopped(jobs.sweep(), stop)
        except Exception:  # driver detail is not logged (INV-EVENTS-5)
            log.warning("jobs: start sweep failed")
            return 0 if stop.is_set() else 1
        if stopped:
            return 0
        _RUNNING[0] = jobs
        # The start hooks (e.g. SRV-PIPELINE's ensure of unfinished runs): after the sweep, running() set.
        if not await _start_hooks(jobs, start_hooks() if hooks is None else hooks, stop):
            return 0
        worker = asyncio.create_task(
            app.run_worker_async(
                queues=jobs.queues,
                concurrency=CONCURRENCY,
                listen_notify=True,
                shutdown_graceful_timeout=SHUTDOWN_GRACEFUL_TIMEOUT_S,
                install_signal_handlers=False,
            )
        )
        stopping = asyncio.create_task(stop.wait())
        await asyncio.wait({worker, stopping}, return_when=asyncio.FIRST_COMPLETED)
        stopping.cancel()
        if not worker.done():
            worker.cancel()  # procrastinate stops gracefully on cancel, then aborts after the timeout
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        return 0
    finally:
        _RUNNING[0] = None
        await app.close_async()
