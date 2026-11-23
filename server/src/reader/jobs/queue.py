"""The job runtime on procrastinate: enqueue, the worker wrapper, the sweep, requeue and status (SRV-EVENTS).

procrastinate jobs are single-shot (no RetryStrategy): this module is the one judge of attempts and outcomes,
kept per key in `job_keys`. Every read-then-write of a key's state by the worker, the sweep or `requeue_dead`
runs under the key's advisory transaction lock; `enqueue` never takes it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import procrastinate
from procrastinate import exceptions as pexc
from procrastinate.jobs import Status
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from reader.jobs.catalog import SWEEP_PRIORITY, Catalog
from reader.jobs.model import (
    SWEEP_TASK,
    JobContext,
    JobError,
    JobFailed,
    JobKind,
    Result,
    canonical_json,
    check_payload,
    idempotency_key,
    lock_id,
)
from reader.jobs.states import RECONCILE_ENQUEUE, RECONCILE_STALL_LIKE, deciding_status, status_of

log = logging.getLogger("reader.jobs")

STALLED_AFTER_S: Final = 30.0
RECONCILE_AGE_S: Final = 60.0
FIRST_RETRY_DELAY_S: Final = 5
RETRY_DELAY_FACTOR: Final = 5
LIVE_STATUSES: Final = ("todo", "doing", "aborting")


def retry_delay_s(attempts: int) -> int:
    """5 s after the first counted failure, 25 s after the second, x5 after that."""
    return int(FIRST_RETRY_DELAY_S * RETRY_DELAY_FACTOR ** max(attempts - 1, 0))


@dataclass
class SweepReport:
    finished: list[int] = field(default_factory=list)
    reenqueued: list[str] = field(default_factory=list)
    dead: list[str] = field(default_factory=list)
    skipped_live: list[str] = field(default_factory=list)
    cancelled: list[int] = field(default_factory=list)


async def _try_lock(conn: AsyncConnection, key: str) -> bool:
    got = (await conn.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": lock_id(key)})).scalar()
    return bool(got)


async def _lock(conn: AsyncConnection, key: str) -> None:
    await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_id(key)})


async def _row(conn: AsyncConnection, kind: str, key: str) -> Mapping[str, Any] | None:
    row = (
        (
            await conn.execute(
                text(
                    "SELECT args::text AS args, attempts, outcome, job_id FROM job_keys"
                    " WHERE kind = :kind AND key = :key"
                ),
                {"kind": kind, "key": key},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return {**row, "args": json.loads(row["args"])}


_COUNT_SQL: Final = text(
    "UPDATE job_keys SET attempts = attempts + 1, updated_at = now(),"
    " outcome = CASE WHEN attempts + 1 >= :max THEN 'dead' END,"
    " error_code = CASE WHEN attempts + 1 >= :max THEN CAST(:code AS text) END,"
    " job_id = CASE WHEN attempts + 1 >= :max THEN CAST(:job_id AS bigint) END,"
    " finished_at = CASE WHEN attempts + 1 >= :max THEN now() END"
    " WHERE kind = :kind AND key = :key AND outcome IS NULL RETURNING attempts, outcome"
)
_COUNT_FORCE_SQL: Final = text(
    "UPDATE job_keys SET attempts = attempts + 1, updated_at = now(), outcome = 'dead', error_code = :code,"
    " job_id = :job_id, finished_at = now()"
    " WHERE kind = :kind AND key = :key AND outcome IS NULL RETURNING attempts, outcome"
)


_OUTCOME_SQL: Final = text(
    "UPDATE job_keys SET outcome = :outcome, error_code = :code, job_id = :job_id,"
    " finished_at = now(), updated_at = now()"
    " WHERE kind = :kind AND key = :key AND outcome IS NULL"
)


async def _set_outcome(
    conn: AsyncConnection, kind: str, key: str, outcome: str, code: str | None, job_id: int | None
) -> bool:
    """FR-EVENTS-13: the first committed outcome wins; False when another outcome is already there."""
    done = await conn.execute(
        _OUTCOME_SQL, {"outcome": outcome, "code": code, "job_id": job_id, "kind": kind, "key": key}
    )
    return done.rowcount == 1


async def _count(
    conn: AsyncConnection, kind: JobKind, key: str, code: str, job_id: int | None, *, dead: bool = False
) -> tuple[int, bool] | None:
    """Increment the attempt count; write `dead` in the same statement when exhausted (or when `dead`)."""
    if dead:
        row = (
            await conn.execute(
                _COUNT_FORCE_SQL, {"code": code, "job_id": job_id, "kind": kind.name, "key": key}
            )
        ).first()
    else:
        row = (
            await conn.execute(
                _COUNT_SQL,
                {"max": kind.max_attempts, "code": code, "job_id": job_id, "kind": kind.name, "key": key},
            )
        ).first()
    if row is None:
        return None
    return int(row[0]), row[1] == "dead"


class Jobs:
    """One per process: the SQLAlchemy engine (asyncpg) for key state, the procrastinate app for delivery."""

    def __init__(
        self,
        engine: AsyncEngine,
        app: procrastinate.App,
        catalog: Catalog,
        secret_values: Iterable[str] = (),
        retry_delay: Callable[[int], int] = retry_delay_s,
    ) -> None:
        self.engine = engine
        self.app = app
        self.catalog = catalog
        self._secrets = tuple(secret_values)
        self.retry_delay = retry_delay
        for kind in catalog.values():
            self._register(kind)
        self._register_sweep()

    # ---- registration -------------------------------------------------------------------------------------

    def _register(self, kind: JobKind) -> None:
        async def task(context: procrastinate.JobContext, payload: dict[str, Any]) -> None:
            await self._run(kind, context, payload)

        self.app.task(name=kind.name, queue=kind.queue, pass_context=True)(task)

    def _register_sweep(self) -> None:
        async def sweep_task(timestamp: int) -> None:
            del timestamp
            await self.sweep()

        task = self.app.task(
            name=SWEEP_TASK, queue=SWEEP_TASK, queueing_lock=SWEEP_TASK, priority=SWEEP_PRIORITY
        )(sweep_task)
        self.app.periodic(cron="* * * * *", periodic_id=SWEEP_TASK)(task)

    @property
    def queues(self) -> list[str]:
        return [*self.catalog.queues, SWEEP_TASK]

    # ---- enqueue (FR-EVENTS-3) ----------------------------------------------------------------------------

    def _validate(self, kind: JobKind, args: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
        raw: Any = args
        if isinstance(args, BaseModel):
            raw = args.model_dump()
        check_payload(args, {}, ())
        try:
            model = kind.args.model_validate(raw)
        except ValidationError:
            raise JobError("args_invalid") from None
        payload = model.model_dump(mode="json")
        check_payload(args, payload, self._secrets)
        return payload

    async def enqueue(
        self, kind_name: str, args: BaseModel | Mapping[str, Any], schedule_in: int = 0
    ) -> Result:
        kind = self.catalog.get(kind_name)
        if kind is None:
            raise JobError("unknown_kind")
        payload = self._validate(kind, args)
        key = idempotency_key(kind, payload)
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO job_keys (kind, key, args, created_at, updated_at)"
                    " VALUES (:kind, :key, CAST(:args AS jsonb), now(), now()) ON CONFLICT DO NOTHING"
                ),
                {"kind": kind.name, "key": key, "args": _json(payload)},
            )
            row = await _row(conn, kind.name, key)
        assert row is not None  # noqa: S101 (inserted or present)
        return await self._defer(kind, key, payload, row, schedule_in)

    async def _defer(
        self, kind: JobKind, key: str, payload: dict[str, Any], row: Mapping[str, Any], schedule_in: int
    ) -> Result:
        if row["outcome"] == "succeeded":
            return Result("already_done", row["job_id"])
        if row["outcome"] == "dead":
            return Result("dead", row["job_id"])
        options: dict[str, Any] = {
            "lock": key,
            "queueing_lock": key,
            "lock_mode": "ordered",
            "queue": kind.queue,
            "priority": kind.priority,
        }
        if schedule_in > 0:
            options["schedule_in"] = {"seconds": schedule_in}
        try:
            job_id = await self.app.configure_task(kind.name, **options).defer_async(payload=payload)
        except pexc.AlreadyEnqueued:
            return Result("already_queued")
        return Result("queued", job_id)

    # ---- the worker wrapper (FR-EVENTS-4, -6) -------------------------------------------------------------

    async def _run(self, kind: JobKind, context: procrastinate.JobContext, payload: dict[str, Any]) -> None:
        job_id = context.job.id
        assert job_id is not None  # noqa: S101
        key: str | None = None
        try:
            key = idempotency_key(kind, payload)
            failure = await self._attempt(kind, key, payload, job_id)
            if failure is not None:
                error, timed_out, lost = failure
                # A deadline overrun is judged even when cancelling the statement invalidated the session:
                # the cause is the run itself. Taking the key lock again in _judge serializes with the sweep.
                if lost and not timed_out:
                    log.warning("job session lost: kind=%s job_id=%s", kind.name, job_id)
                    await self._count_lost_run(kind, key, payload, job_id, "session_lost")
                    raise JobFailed("session_lost")
                await self._judge(kind, key, payload, job_id, error, timed_out)
        except JobFailed, asyncio.CancelledError:
            raise  # JobFailed carries only a code; cancellation is a shutdown abort (procrastinate: aborted)
        except Exception:  # a database or wrapper failure: never let its message or args out
            log.warning("job wrapper failed: kind=%s job_id=%s", kind.name, job_id)
            if key is not None:  # without a key (payload not keyable) there is no key row to count against
                await self._count_lost_run(kind, key, payload, job_id, "wrapper_error")
            raise JobFailed("wrapper_error") from None

    async def _count_lost_run(
        self, kind: JobKind, key: str, payload: dict[str, Any], job_id: int, code: str
    ) -> None:
        """Count a run that ended without a judged outcome, unless the sweep already counted it.

        On a fresh connection, under the key lock: while this job is still `doing` and the key has no outcome,
        no one else has counted the run (the sweep finishes the jobs it counts), so it is counted here like a
        stall (dead when exhausted, else enqueued again at once). When that connection fails too, nothing is
        counted here; the job ends `failed` and FR-EVENTS-15 counts it after 60 s.
        """
        step = "count"
        try:
            enqueue = False
            async with self.engine.begin() as conn:
                await _lock(conn, key)
                status = (
                    await conn.execute(
                        text("SELECT status::text FROM procrastinate_jobs WHERE id = :id"), {"id": job_id}
                    )
                ).scalar()
                row = await _row(conn, kind.name, key)
                if status != "doing" or row is None or row["outcome"] is not None:
                    return
                counted = await _count(conn, kind, key, code, job_id)
                enqueue = counted is not None and not counted[1]
            if enqueue:
                step = "re-enqueue"
                await self._defer(kind, key, payload, {"outcome": None, "job_id": None}, 0)
        except Exception:  # the database is unreachable again: FR-EVENTS-15 takes the key after 60 s
            if step == "count":
                log.warning("lost run not counted: kind=%s job_id=%s", kind.name, job_id)
            else:
                log.warning("lost run counted, re-enqueue failed: kind=%s job_id=%s", kind.name, job_id)

    async def _attempt(
        self, kind: JobKind, key: str, payload: dict[str, Any], job_id: int
    ) -> tuple[BaseException, bool, bool] | None:
        """One run under the key lock: None when done, else (exception, deadline expired, session lost)."""
        async with self.engine.connect() as conn, conn.begin() as tx:
            await conn.execute(text("SET LOCAL idle_in_transaction_session_timeout = 0"))
            await _lock(conn, key)
            row = await _row(conn, kind.name, key)
            if row is None:
                log.warning("job without key row: kind=%s job_id=%s", kind.name, job_id)
                raise JobFailed("no_key_row")
            if row["outcome"] is not None:
                log.info("job skipped: kind=%s job_id=%s outcome=%s", kind.name, job_id, row["outcome"])
                return None
            attempts = int(row["attempts"])
            if attempts >= kind.max_attempts:
                await _set_outcome(conn, kind.name, key, "dead", "exhausted", job_id)
                await tx.commit()
                raise JobFailed("exhausted")
            deadline = asyncio.timeout(kind.timeout_s)
            try:
                model = kind.args.model_validate(payload)
                async with deadline:
                    await kind.handler(model, conn, JobContext(job_id, key, attempts))
                if not await _set_outcome(conn, kind.name, key, "succeeded", None, job_id):
                    winner = await _row(conn, kind.name, key)
                    await tx.rollback()
                    log.warning(
                        "outcome_lost: kind=%s job_id=%s winner=%s",
                        kind.name,
                        job_id,
                        winner["outcome"] if winner else None,
                    )
                    return None
                await tx.commit()
                return None
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await tx.rollback()
                raise
            except Exception as error:  # every handler failure is judged by _judge, never logged as is
                # The run's own session died (network cut, backend terminated): the key lock went with it, so
                # the sweep owns the key now and counts the lost run once (FR-EVENTS-7, -15). Not judged here.
                lost = conn.invalidated or (isinstance(error, DBAPIError) and error.connection_invalidated)
                with contextlib.suppress(Exception):
                    await tx.rollback()
                return error, deadline.expired(), lost

    async def _judge(
        self,
        kind: JobKind,
        key: str,
        payload: dict[str, Any],
        job_id: int,
        failure: BaseException,
        timed_out: bool,
    ) -> None:
        # Only the run's own deadline is a timeout; a handler's own TimeoutError is judged by `transient`.
        if timed_out:
            code, transient = "timeout", False
        else:
            code = type(failure).__name__
            transient = isinstance(failure, kind.transient)
        async with self.engine.begin() as conn:
            await _lock(conn, key)
            counted = await _count(conn, kind, key, code, job_id, dead=not transient)
        if counted is not None:
            attempts, dead = counted
            log.info("job failed: kind=%s job_id=%s attempts=%s code=%s", kind.name, job_id, attempts, code)
            if not dead:
                try:
                    await self._defer(
                        kind, key, payload, {"outcome": None, "job_id": None}, self.retry_delay(attempts)
                    )
                except Exception:  # FR-EVENTS-6: a lost re-enqueue takes the stall-like path after 60 s
                    log.warning("re-enqueue failed: kind=%s job_id=%s", kind.name, job_id)
        raise JobFailed(code) from None

    # ---- requeue and status (FR-EVENTS-8, -12) ------------------------------------------------------------

    async def requeue_dead(self, kind_name: str, key: str) -> Result:
        kind = self.catalog.get(kind_name)
        if kind is None:
            raise JobError("unknown_kind")
        async with self.engine.begin() as conn:
            if not await _try_lock(conn, key):
                return Result("running")
            row = await _row(conn, kind.name, key)
            if row is None:
                return Result("unknown_key")
            if row["outcome"] == "succeeded":
                return Result("already_done", row["job_id"])
            if row["outcome"] == "dead":
                await conn.execute(
                    text(
                        "UPDATE job_keys SET outcome = NULL, error_code = NULL, job_id = NULL,"
                        " finished_at = NULL, attempts = 0, updated_at = now()"
                        " WHERE kind = :kind AND key = :key"
                    ),
                    {"kind": kind.name, "key": key},
                )
        return await self._defer(kind, key, row["args"], {"outcome": None, "job_id": None}, 0)

    async def _job_statuses(self, conn: AsyncConnection, key: str) -> list[tuple[int, str]]:
        return await _job_statuses(conn, key)

    async def job_status(self, kind_name: str, key: str) -> str:
        async with self.engine.connect() as conn:
            return await job_status_of(conn, kind_name, key)

    # ---- the sweep (FR-EVENTS-7, -15) ---------------------------------------------------------------------

    async def _finish(self, job: Any, status: Status) -> bool:
        try:
            await self.app.job_manager.finish_job(job, status, delete_job=False)
        except Exception:  # the job already left `doing` (finished by its own worker or an earlier sweep)
            log.info("finish skipped: job_id=%s", job.id)
            return False
        return True

    async def _sweep_defer(self, kind: JobKind, key: str, args: dict[str, Any]) -> bool:
        """A sweep's enqueue (FR-EVENTS-7, -15): a failure is logged with the kind only and ends nothing; the
        key is left with no todo job, so reconciliation takes it once its row is 60 s old."""
        try:
            await self._defer(kind, key, args, {"outcome": None, "job_id": None}, 0)
        except Exception:
            log.warning("sweep re-enqueue failed: kind=%s", kind.name)
            return False
        return True

    async def sweep(
        self, stalled_after_s: float = STALLED_AFTER_S, reconcile_age_s: float = RECONCILE_AGE_S
    ) -> SweepReport:
        report = SweepReport()
        await self._sweep_stalled(report, stalled_after_s)
        await self._reconcile(report, reconcile_age_s)
        await self._cancel_unknown(report)
        return report

    async def _sweep_stalled(self, report: SweepReport, stalled_after_s: float) -> None:
        stalled = await self.app.job_manager.get_stalled_jobs(seconds_since_heartbeat=stalled_after_s)
        for job in stalled:
            job_id = int(job.id or 0)
            if job.task_name == SWEEP_TASK:
                if await self._finish(job, Status.FAILED):
                    report.finished.append(job_id)
                continue
            kind = self.catalog.get(job.task_name)
            key = job.lock
            if kind is None or key is None:
                if key is not None:
                    async with self.engine.begin() as conn:
                        if not await _try_lock(conn, key):  # a live session (another worker version) runs it
                            report.skipped_live.append(key)
                            continue
                        await _set_outcome(conn, job.task_name, key, "dead", "unknown_kind", job_id)
                if await self._finish(job, Status.FAILED):
                    report.finished.append(job_id)
                continue
            enqueue = False
            finish_status = Status.FAILED
            async with self.engine.begin() as conn:
                if not await _try_lock(conn, key):
                    report.skipped_live.append(key)
                    continue
                row = await _row(conn, kind.name, key)
                if row is None:
                    pass
                elif row["outcome"] is not None:
                    finish_status = Status.SUCCEEDED if row["outcome"] == "succeeded" else Status.FAILED
                else:
                    counted = await _count(conn, kind, key, "stalled", job_id)
                    if counted is not None and counted[1]:
                        report.dead.append(key)
                    else:
                        enqueue = True
            if enqueue and row is not None and await self._sweep_defer(kind, key, row["args"]):
                report.reenqueued.append(key)
            if await self._finish(job, finish_status):
                report.finished.append(job_id)

    async def _reconcile(self, report: SweepReport, reconcile_age_s: float) -> None:
        async with self.engine.connect() as conn:
            candidates = (
                await conn.execute(
                    text(
                        "SELECT kind, key FROM job_keys k WHERE outcome IS NULL"
                        " AND updated_at < now() - make_interval(secs => :age)"
                        " AND NOT EXISTS (SELECT 1 FROM procrastinate_jobs j WHERE j.lock = k.key"
                        " AND j.status::text IN ('todo', 'doing', 'aborting'))"
                    ),
                    {"age": reconcile_age_s},
                )
            ).all()
        for kind_name, key in candidates:
            kind = self.catalog.get(kind_name)
            action = "-"
            row: Mapping[str, Any] | None = None
            async with self.engine.begin() as conn:
                if not await _try_lock(conn, key):
                    report.skipped_live.append(key)
                    continue
                row = await _row(conn, kind_name, key)
                statuses = await self._job_statuses(conn, key)
                if row is None or row["outcome"] is not None or any(s in LIVE_STATUSES for _, s in statuses):
                    continue
                if kind is None:  # another worker version's kind: skipped, never decided here (FR-EVENTS-15)
                    continue
                action = status_of(deciding_status(statuses), None, True)[1]
                if action == RECONCILE_STALL_LIKE:
                    newest = max(statuses)[0]
                    counted = await _count(conn, kind, key, "orphaned", newest)
                    if counted is not None and counted[1]:
                        report.dead.append(key)
                        action = "-"
                    else:
                        action = RECONCILE_ENQUEUE
            if (
                action == RECONCILE_ENQUEUE
                and kind is not None
                and row is not None
                and await self._sweep_defer(kind, key, row["args"])
            ):
                report.reenqueued.append(key)

    async def _cancel_unknown(self, report: SweepReport) -> None:
        known = [*self.catalog, SWEEP_TASK]
        async with self.engine.connect() as conn:
            ids = (
                (
                    await conn.execute(
                        text(
                            "SELECT id FROM procrastinate_jobs WHERE status = 'todo'"
                            " AND NOT (task_name = ANY(:known))"
                        ),
                        {"known": known},
                    )
                )
                .scalars()
                .all()
            )
        for job_id in ids:
            if await self.app.job_manager.cancel_job_by_id_async(int(job_id)):
                report.cancelled.append(int(job_id))


def _json(payload: Mapping[str, Any]) -> str:
    return canonical_json(dict(payload))


async def _job_statuses(conn: AsyncConnection, key: str) -> list[tuple[int, str]]:
    rows = await conn.execute(
        text("SELECT id, status::text FROM procrastinate_jobs WHERE lock = :key"), {"key": key}
    )
    return [(int(i), str(s)) for i, s in rows]


async def job_status_of(conn: AsyncConnection, kind_name: str, key: str) -> str:
    """FR-EVENTS-12 on an open connection, without a procrastinate app (shared with SRV-INGEST's routes)."""
    row = await _row(conn, kind_name, key)
    deciding = deciding_status(await _job_statuses(conn, key))
    return status_of(deciding, row["outcome"] if row else None, row is not None)[0]
