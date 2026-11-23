"""Retention (SRV-OBS FR-OBS-11): job kind `obs.retention`, one key per UTC day. The handler first enqueues
the next day (the argument day + 1), then deletes MLflow traces older than `retention_days` in one REST
call. With tracing off it only enqueues the next day. MLflow unreachable is transient; any other failure is
RetentionFailed."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from reader.jobs.model import JobKind

KIND: Final = "obs.retention"
MLFLOW: Final = "http://mlflow:5000"
DELETE_PATH: Final = "/api/2.0/mlflow/traces/delete-traces"
RUN_AT: Final = time(3, 0, tzinfo=UTC)
DAY_MS: Final = 86_400_000


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
    try:
        await jobs.enqueue(KIND, {"day": following}, schedule_in=seconds_until(following, now))
    except Exception:
        logging.getLogger("reader.obs").warning("obs retention: next day not enqueued")
    if not enabled:
        return 0
    url, body = delete_request(now.timestamp(), retention_days)
    response = await client.post(url, json=body, timeout=30)
    if response.status_code != 200:
        raise RetentionFailed(f"status {response.status_code}")
    data = response.json()
    count = data.get("traces_deleted", 0) if isinstance(data, dict) else 0
    return int(count) if isinstance(count, int) and not isinstance(count, bool) else 0


async def handle(args: RetentionArgs, conn: Any, ctx: Any) -> None:
    from reader.jobs import running
    from reader.obs import setup as obs_setup

    settings = obs_setup.current_settings()
    async with httpx.AsyncClient(trust_env=False) as client:
        await retain(
            args.day, jobs=running(), enabled=settings.enabled, retention_days=settings.retention_days,
            client=client, now=datetime.now(UTC),
        )


async def on_worker_start(jobs: Any, now: datetime | None = None) -> None:
    today = (now or datetime.now(UTC)).date().isoformat()
    await jobs.enqueue(KIND, {"day": today})


JOB_KINDS: Final = (
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
)
