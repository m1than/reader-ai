"""The worker loop: the start sweep, then procrastinate serving every declared kind (SRV-EVENTS FR-EVENTS-9).

`reader-server --role worker` calls `run` once the schema is at all heads.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from typing import Final

import procrastinate
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reader.db import DbTarget
from reader.jobs.catalog import Catalog, load_kinds, start_hooks
from reader.jobs.logfilter import install_log_filter
from reader.jobs.model import JobError
from reader.jobs.queue import Jobs

log = logging.getLogger("reader.jobs")

CONCURRENCY: Final = 1
_RUNNING: list[Jobs | None] = [None]


def running() -> Jobs:
    """The worker's `Jobs`, for a handler that enqueues a follow-up job (paired SRV-PIPELINE amendment)."""
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


async def run(
    engine: AsyncEngine,
    target: DbTarget,
    stop: asyncio.Event,
    catalog: Catalog | None = None,
    secret_values: Iterable[str] = (),
) -> int:
    """Serve jobs until `stop` is set. Returns 0 after a stop, 1 when the database is unreachable at start.

    `secret_values` are every resolved secret the calling process holds (FR-EVENTS-10); the database
    password is always among them.
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
        await jobs.sweep()
        _RUNNING[0] = jobs
        if (
            catalog is None
        ):  # the declared kinds: their start hooks (e.g. SRV-PIPELINE's ensure of unfinished runs)
            for hook in start_hooks():
                try:
                    await hook(jobs)
                except Exception:  # a hook's failure is logged by code only; the worker still serves
                    log.warning("jobs: start hook failed: %s", getattr(hook, "__module__", "?"))
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
        await app.close_async()
