"""SRV-OBS [db]: the owner's `observability.retention_days` override through the real readers (SRV-CONFIG
FR-CONFIG-26, paired): the retention job's cutoff and the obs summary's trace cutoff.

@trace FR-OBS-11 FR-OBS-1 FR-CONFIG-26

The process's file value is 30 days (setup's current settings); the override says 7 (the job) or 1 (the summary).
Only the trace store's HTTP calls are replaced (the job's `retain` and `runs.prune` record their arguments); the
`obs_tracing` prune and the summary run against Postgres.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from .conftest import require

pytestmark = pytest.mark.anyio
FILE_DAYS = 30


class _Jobs:
    async def enqueue(self, kind: str, args: dict[str, Any], schedule_in: int = 0) -> None:
        return None


async def _ready(pg: Any, days: int) -> Any:
    from sqlalchemy import text

    from jobs.conftest import target
    from reader.db import make_engine, migrate

    engine = make_engine(target(pg))
    await migrate(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO settings_overrides (key, value, updated_by, version)"
                " VALUES ('observability.retention_days', CAST(:v AS jsonb), :u, 1)"
            ),
            {"v": str(days), "u": uuid.uuid4()},
        )
    return engine


async def test_the_retention_job_cuts_off_at_the_override(pg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy import text

    import reader.jobs
    from reader.obs import setup

    jobs = require("reader.obs.jobs")
    seen: dict[str, Any] = {}

    async def retain(day: str, **kwargs: Any) -> int:
        seen["retain"] = kwargs["retention_days"]
        return 0

    async def prune(client: Any, *, now: datetime, retention_days: int, running_key: str | None) -> None:
        seen["prune"] = retention_days

    monkeypatch.setattr(setup, "current_settings", lambda: setup.Settings(True, FILE_DAYS))
    monkeypatch.setattr(reader.jobs, "running", _Jobs)
    monkeypatch.setattr(jobs, "retain", retain)
    monkeypatch.setattr(jobs.runs, "prune", prune)
    engine = await _ready(pg, 7)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM obs_tracing"))
            for days_ago in (40, 20, 10, 2):
                await conn.execute(
                    text(
                        "INSERT INTO obs_tracing (started_at, enabled)"
                        " VALUES (now() - make_interval(days => :d), true)"
                    ),
                    {"d": days_ago},
                )
        async with engine.begin() as conn:
            await jobs.handle(jobs.RetentionArgs(day="2026-11-24"), conn, None)
        async with engine.connect() as conn:
            left = [
                int(r[0])
                for r in await conn.execute(
                    text("SELECT round(extract(epoch FROM now() - started_at) / 86400) FROM obs_tracing"
                         " ORDER BY started_at")
                )
            ]
    finally:
        await engine.dispose()
    assert seen == {"retain": 7, "prune": 7}
    # Cutoff 7 days: the starts before it go, except the latest of them (it decides the rows after the cutoff).
    # The file's 30 days would have kept all four.
    assert left == [10, 2]


async def test_the_summary_reads_the_override_per_request(pg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from reader.obs import setup

    summary = require("reader.obs.summary")
    gates: list[Any] = []
    original = summary.trace_gate

    async def recording(conn: Any, since: datetime, now: datetime, tracing: Any) -> Any:
        gate = await original(conn, since, now, tracing)
        gates.append(gate)
        return gate

    monkeypatch.setattr(setup, "current_settings", lambda: setup.Settings(True, FILE_DAYS))
    monkeypatch.setattr(summary, "trace_gate", recording)
    now = datetime.now(UTC)
    engine = await _ready(pg, 1)
    try:
        async with engine.connect() as conn:
            await summary.summary(conn, uuid.uuid4(), days=7, now=now)
    finally:
        await engine.dispose()
    (gate,) = gates
    assert gate.oldest == now - timedelta(days=1)  # the file's value would give now - 30 days
