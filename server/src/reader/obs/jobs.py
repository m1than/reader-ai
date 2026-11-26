"""Retention (SRV-OBS FR-OBS-11): job kind `obs.retention`, one key per UTC day. The handler first enqueues
the next day (the argument day + 1), then deletes MLflow traces older than `retention_days` in one REST
call. With tracing off it only enqueues the next day. MLflow unreachable is transient; any other failure is
RetentionFailed."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from reader.jobs.model import JobKind
from reader.obs import align, bench, bootstrap, feedback, health, labels, prompts, runs
from reader.obs import eval as eval_jobs

KIND: Final = "obs.retention"
MLFLOW: Final = "http://mlflow:5000"
DELETE_PATH: Final = "/api/2.0/mlflow/traces/delete-traces"
RUN_AT: Final = time(3, 0, tzinfo=UTC)
DAY_MS: Final = 86_400_000
ENQUEUE_TRIES: Final = 3
ENQUEUE_PAUSE_S: Final = 1.0


class RetentionFailed(Exception):
    """MLflow answered, but not with a successful delete: the day's key ends dead (FR-EVENTS-6)."""


class RetentionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("day")
    @classmethod
    def _real_day(cls, value: str) -> str:
        date.fromisoformat(value)
        return value


def next_day(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def seconds_until(day: str, now: datetime) -> int:
    """Seconds from `now` to 03:00 UTC of `day`; 0 when already past."""
    at = datetime.combine(date.fromisoformat(day), RUN_AT)
    return max(int((at - now).total_seconds()), 0)


def cutoff_ms(now: datetime, retention_days: int) -> int:
    return int(now.timestamp() * 1000) - retention_days * DAY_MS


def delete_request(now_epoch_s: float, retention_days: int) -> tuple[str, dict[str, Any]]:
    """The one REST call: URL and JSON body (no max_traces: MLflow 3.18.0 sets no server cap)."""
    now = datetime.fromtimestamp(now_epoch_s, UTC)
    body = {"experiment_id": "0", "max_timestamp_millis": cutoff_ms(now, retention_days)}
    return MLFLOW + DELETE_PATH, body


async def retain(
    day: str, *, jobs: Any, enabled: bool, retention_days: int, client: httpx.AsyncClient, now: datetime
) -> int:
    """Enqueue the next day first (a failure there never stops today's delete), then delete; returns the
    count."""
    following = next_day(day)
    for attempt in range(1, ENQUEUE_TRIES + 1):
        try:
            await jobs.enqueue(KIND, {"day": following}, schedule_in=seconds_until(following, now))
            break
        except Exception:
            if attempt == ENQUEUE_TRIES:
                logging.getLogger("reader.obs").warning("obs retention: next day not enqueued")
            else:
                await asyncio.sleep(ENQUEUE_PAUSE_S)
    if not enabled:
        return 0
    url, body = delete_request(now.timestamp(), retention_days)
    response = await client.post(url, json=body, timeout=30)
    if response.status_code != 200:
        raise RetentionFailed(f"status {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        raise RetentionFailed("body") from None
    if not isinstance(data, dict):
        raise RetentionFailed("body")
    count = data.get("traces_deleted", 0)  # protobuf JSON omits a zero count
    if not isinstance(count, int) or isinstance(count, bool):
        raise RetentionFailed("body")
    return count


async def handle(args: RetentionArgs, conn: Any, ctx: Any) -> None:
    from reader.jobs import running
    from reader.obs import setup as obs_setup

    settings = await obs_setup.effective_settings(conn)  # the retention override per run (FR-CONFIG-26)
    now = datetime.now(UTC)
    if conn is not None:  # ADR-0039 (FR-OBS-11 as amended): Postgres first, tracing on or off
        await postgres_retention(conn, now, settings.retention_days)
    async with httpx.AsyncClient(trust_env=False) as client:
        await retain(
            args.day, jobs=running(), enabled=settings.enabled, retention_days=settings.retention_days,
            client=client, now=now,
        )
        if settings.enabled:  # FR-OBS-31: Runs and unused LoggedModels past retention (phase 2)
            from reader.obs import _STATE

            key = _STATE.get("version_key")
            try:  # the trace delete above is the job's outcome; a prune failure waits for tomorrow's run
                await runs.prune(client, now=now, retention_days=settings.retention_days,
                                 running_key=key if isinstance(key, str) else None)
            except Exception as failure:
                name = type(failure).__name__
                logging.getLogger("reader.obs").warning("obs retention prune_failed error=%s", name)
    # FR-OBS-15: the daily run also enqueues the prompt registration (enqueue-only, never raises), so a worker
    # that runs for days retries a dead key and registers changed templates.
    try:
        jobs = running()
    except Exception:
        logging.getLogger("reader.obs").warning("obs prompts not enqueued")
        return
    await prompts.on_worker_start(jobs, now)


# FR-OBS-10: worker starts older than retention decide nothing any more, except the latest of them, which
# still decides the rows between the cutoff and the next start.
PRUNE_SQL: Final = (
    "DELETE FROM obs_tracing WHERE started_at < :c"
    " AND started_at < (SELECT max(started_at) FROM obs_tracing WHERE started_at < :c)"
)


async def prune_starts(conn: Any, cutoff: datetime) -> None:
    from sqlalchemy import text

    await conn.execute(text(PRUNE_SQL), {"c": cutoff})


# ADR-0039: alignment rows still running a day after their last step (FR-OBS-41), and refused or discarded
# proposals past retention (FR-OBS-31 as amended; they hold no text).
STEP_LOST_SQL: Final = (
    "UPDATE judge_prompt_proposals SET state = 'refused', refusal = 'step_lost', candidate_text = NULL,"
    " candidates_under_test = NULL, updated_at = now()"
    " WHERE state = 'running' AND updated_at < now() - interval '1 day'"
)
PROPOSALS_SQL: Final = (
    "DELETE FROM judge_prompt_proposals WHERE state IN ('refused', 'discarded')"
    " AND updated_at < now() - make_interval(days => :d)"
)


async def postgres_retention(conn: Any, now: datetime, retention_days: int) -> None:
    """FR-OBS-11 as amended: the Postgres deletes, each in its own committed transaction (so neither a later MLflow
    failure nor the next step's failure undoes it), before any MLflow request; a failure is logged by code."""
    from sqlalchemy import text

    from reader.obs import judge_inputs

    async def starts(c: Any) -> None:
        await prune_starts(c, now - timedelta(days=retention_days))

    async def inputs(c: Any) -> None:
        await judge_inputs.delete_expired(c, retention_days)

    async def lost(c: Any) -> None:
        await c.execute(text(STEP_LOST_SQL))

    async def proposals(c: Any) -> None:
        await c.execute(text(PROPOSALS_SQL), {"d": int(retention_days)})

    for name, step in (("judge_inputs", inputs), ("starts", starts), ("step_lost", lost),
                       ("proposals", proposals)):
        try:
            async with conn.engine.begin() as own:
                await step(own)
        except Exception as failure:
            logging.getLogger("reader.obs").warning(
                "obs retention postgres_failed step=%s error=%s", name, type(failure).__name__
            )


RECORD_START_SQL: Final = "INSERT INTO obs_tracing (started_at, enabled) VALUES (clock_timestamp(), :enabled)"
RECORD_TRIES: Final = 3


async def record_start(engine: Any, enabled: bool) -> bool:
    """FR-OBS-10: one `obs_tracing` row per worker start, tried 3 times, 1 s apart. If every try fails, the
    previous start's row still decides this start's jobs: a link may then be missing, or, when tracing was
    turned off and that row says on, point to a trace that does not exist (logged, declared in FR-OBS-10)."""
    from sqlalchemy import text

    for attempt in range(1, RECORD_TRIES + 1):
        try:
            async with engine.begin() as conn:
                await conn.execute(text(RECORD_START_SQL), {"enabled": enabled})
            return True
        except Exception:
            if attempt < RECORD_TRIES:
                await asyncio.sleep(ENQUEUE_PAUSE_S)
    logging.getLogger("reader.obs").warning("obs tracing start not recorded")
    return False


async def on_worker_start(jobs: Any, now: datetime | None = None) -> None:
    from reader.obs import setup as obs_setup

    engine = getattr(jobs, "engine", None)
    if engine is not None:
        await record_start(engine, obs_setup.current_settings().enabled)
    today = (now or datetime.now(UTC)).date().isoformat()
    try:
        await jobs.enqueue(KIND, {"day": today})
    finally:
        await prompts.on_worker_start(jobs, now)  # FR-OBS-15: enqueue-only, never raises
        await bootstrap.on_worker_start(jobs, now)  # FR-OBS-21, -26: enqueue-only, never raises


JOB_KINDS: Final = (
    feedback.KIND_DECL,
    prompts.KIND_DECL,
    JobKind(
        name=KIND,
        queue="obs",
        args=RetentionArgs,
        key=("day",),
        handler=handle,
        max_attempts=3,
        transient=(httpx.TransportError,),
        timeout_s=60,
    ),
    # SRV-OBS phase 2 (phase2_job_kinds, ADR-0038)
    bootstrap.KIND_DECL,
    runs.KIND_DECL,
    bench.KIND_DECL,
    labels.KIND_DECL,
    health.KIND_DECL,
    # ADR-0039 (kl_job_kinds): FR-OBS-39, -14, -41
    *eval_jobs.JOB_KINDS,
    align.KIND_DECL,
)
