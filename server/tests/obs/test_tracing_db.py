"""SRV-OBS [db]: which summary rows get a trace link (FR-OBS-10's obs_tracing rule through `summary()`), the
worker-start rows, their pruning by the retention job (FR-OBS-11), and migration obs_0001 (backfill, downgrade).

@trace FR-OBS-10 FR-OBS-11
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text

from jobs.conftest import target
from reader.db import make_engine, migrate

from .conftest import require

pytestmark = pytest.mark.anyio
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
OWNER = uuid.UUID("00000000-0000-4000-8000-000000000001")
ON = SimpleNamespace(enabled=True, retention_days=30)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _h(hours: float) -> datetime:
    return NOW + timedelta(hours=hours)


async def _engine(pg: Any) -> Any:
    engine = make_engine(target(pg))
    await migrate(engine)
    return engine


async def _starts(engine: Any, rows: list[tuple[datetime, bool]]) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM obs_tracing"))
        for started, enabled in rows:
            await conn.execute(
                text("INSERT INTO obs_tracing (started_at, enabled) VALUES (:s, :e)"), {"s": started, "e": enabled}
            )


async def _dead_jobs(engine: Any, finished: dict[str, datetime]) -> None:
    """One dead `search.embed` key row per code (a system row: shown unscoped, one group per code)."""
    async with engine.begin() as conn:
        for n, (code, at) in enumerate(sorted(finished.items()), start=1):
            await conn.execute(
                text(
                    "INSERT INTO job_keys (kind, key, args, attempts, outcome, error_code, job_id, created_at,"
                    " updated_at, finished_at) VALUES ('search.embed', :k, '{}', 1, 'dead', :c, :j, :f, :f, :f)"
                ),
                {"k": f"search.embed:{n:064x}", "c": code, "j": 1000 + n, "f": at},
            )


async def _linked(engine: Any, days: int = 7, tracing: Any = ON) -> dict[str, bool]:
    summary = require("reader.obs.summary")
    async with engine.connect() as conn:
        body = await summary.summary(conn, OWNER, days=days, now=NOW, tracing=tracing)
    return {g["error_code"]: bool(g["trace_ids"]) for g in body["failures"]["jobs"]}


async def test_periods_in_the_window_decide_each_row(pg: Any) -> None:
    """Enabled → disabled → enabled inside the window, a start before it, and the rows inserted newest first."""
    engine = await _engine(pg)
    try:
        await _dead_jobs(
            engine,
            {
                "a_before_first_start": _h(-24 * 9),  # outside the 7-day window: not listed at all
                "b_pre_window_start": _h(-7),  # decided by the start before the window (enabled)
                "c_enabled": _h(-5),
                "d_disabled": _h(-3),
                "e_enabled_again": _h(-1),
                "f_at_a_start": _h(-4),  # exactly at the disabling start: that start decides
            },
        )
        # Inserted newest first: the rule must not depend on the order the rows come back in.
        await _starts(engine, [(_h(-2), True), (_h(-4), False), (_h(-6), True), (_h(-24 * 8), True)])
        linked = await _linked(engine)
        assert linked == {
            "b_pre_window_start": True,
            "c_enabled": True,
            "d_disabled": False,
            "e_enabled_again": True,
            "f_at_a_start": False,
        }
        # Without the start before the window, the window's first rows have no deciding start.
        await _starts(engine, [(_h(-2), True), (_h(-4), False), (_h(-6), True)])
        assert (await _linked(engine))["b_pre_window_start"] is False
        # Tracing off in the api now: no link at all.
        off = SimpleNamespace(enabled=False, retention_days=30)
        assert not any((await _linked(engine, tracing=off)).values())
    finally:
        await engine.dispose()


async def test_rows_older_than_retention_get_no_link(pg: Any) -> None:
    engine = await _engine(pg)
    try:
        await _dead_jobs(engine, {"old": _h(-24 * 40), "edge": _h(-24 * 30), "young": _h(-24 * 29)})
        await _starts(engine, [(_h(-24 * 60), True)])
        assert await _linked(engine, days=45) == {"old": False, "edge": True, "young": True}
        ten_years = SimpleNamespace(enabled=True, retention_days=3650)
        assert await _linked(engine, days=45, tracing=ten_years) == {"old": True, "edge": True, "young": True}
    finally:
        await engine.dispose()


async def test_worker_start_rows_and_their_pruning(pg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from reader.obs import setup

    jobs = require("reader.obs.jobs")
    engine = await _engine(pg)
    enqueued: list[Any] = []

    class Queue:
        def __init__(self) -> None:
            self.engine = engine

        async def enqueue(self, kind: str, args: Any, schedule_in: int = 0) -> None:
            enqueued.append((kind, args))

    try:
        await _starts(engine, [])
        before = datetime.now(UTC)
        for enabled in (True, False):
            monkeypatch.setattr(setup, "current_settings", lambda e=enabled: setup.Settings(e, 30))
            await jobs.on_worker_start(Queue(), now=NOW)
        after = datetime.now(UTC)
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT started_at, enabled FROM obs_tracing ORDER BY 1"))).all()
        assert [r.enabled for r in rows] == [True, False]
        assert all(before <= r.started_at <= after for r in rows)  # the database clock at the start
        # FR-OBS-15's obs.prompts enqueue rides the same hook (tested in test_retention.py).
        assert [e for e in enqueued if e[0] == "obs.retention"] == [("obs.retention", {"day": "2026-11-24"})] * 2
        # Pruning keeps every start after the cutoff and the latest one before it.
        await _starts(engine, [(_h(-24 * 50), True), (_h(-24 * 40), False), (_h(-24 * 35), True), (_h(-1), True)])
        async with engine.begin() as conn:
            await jobs.prune_starts(conn, NOW - timedelta(days=30))
        async with engine.connect() as conn:
            left = (await conn.execute(text("SELECT started_at FROM obs_tracing ORDER BY 1"))).scalars().all()
        assert left == [_h(-24 * 35), _h(-1)]
        async with engine.begin() as conn:  # nothing before the cutoff: nothing deleted
            await jobs.prune_starts(conn, NOW - timedelta(days=60))
        async with engine.connect() as conn:
            assert len((await conn.execute(text("SELECT 1 FROM obs_tracing"))).all()) == 2
    finally:
        await engine.dispose()


async def test_a_failed_start_row_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = require("reader.obs.jobs")
    monkeypatch.setattr(jobs, "ENQUEUE_PAUSE_S", 0.0)
    tries: list[bool] = []

    class Broken:
        def begin(self) -> Any:
            tries.append(True)
            raise ConnectionError("db down")

    assert await jobs.record_start(Broken(), True) is False
    assert len(tries) == jobs.RECORD_TRIES == 3


async def test_obs_0001_backfill_and_downgrade(pg: Any) -> None:
    """The backfill row sits at the oldest obs.retention key row; the downgrade drops the table."""
    from alembic import command

    from reader.db import alembic_config

    engine = await _engine(pg)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM obs_tracing"))
            for day, at in (("2026-11-20", _h(-24 * 4)), ("2026-11-22", _h(-24 * 2))):
                await conn.execute(
                    text(
                        "INSERT INTO job_keys (kind, key, args, attempts, created_at, updated_at)"
                        " VALUES ('obs.retention', :k, CAST(:a AS jsonb), 0, :c, :c)"
                    ),
                    {"k": f"obs.retention:{day}", "a": json.dumps({"day": day}), "c": at},
                )

        def down_then_up(sync_conn: Any) -> None:
            config = alembic_config()
            config.attributes["connection"] = sync_conn
            command.downgrade(config, "obs@base")
            gone = sync_conn.execute(text("SELECT to_regclass('obs_tracing')")).scalar()
            assert gone is None
            command.upgrade(config, "obs@head")

        async with engine.connect() as conn:
            await conn.run_sync(down_then_up)
            await conn.commit()
            rows = (await conn.execute(text("SELECT started_at, enabled FROM obs_tracing"))).all()
        assert [(r.started_at, r.enabled) for r in rows] == [(_h(-24 * 4), True)]
    finally:
        await engine.dispose()
