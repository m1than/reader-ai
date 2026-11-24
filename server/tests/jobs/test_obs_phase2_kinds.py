"""SRV-EVENTS [unit + db], paired SRV-OBS phase 2 change: the five phase 2 obs kinds exactly as SRV-OBS
`phase2_job_kinds` declares them, `JobKind.traced` (a `traced: false` kind opens no root span), and the
periodic `obs.health` entry (one job per 30-minute slot).

@trace FR-EVENTS-32 FR-OBS-32
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any

import pytest

from obs.conftest import finished, obs_setup, require

from .conftest import KIND, Pg, effect_kind, jobs_env, require_jobs

DECLARED = {  # SRV-OBS phase2_job_kinds: (key, max_attempts, timeout_s, traced)
    "obs.setup": (("digest",), 3, 60, True),
    "obs.runs": (("kind", "id", "end"), 3, 60, True),
    "obs.bench": (("upload_id",), 3, 60, True),
    "obs.label": (("label_id", "revision"), 3, 30, True),
    "obs.health": (("slot",), 1, 20, False),
}


def test_phase2_kinds_are_declared_exactly() -> None:
    jobs = require("reader.obs.jobs")
    by_name = {k.name: k for k in jobs.JOB_KINDS}
    for name, (key, attempts, timeout, traced) in DECLARED.items():
        k = by_name[name]
        got = (k.queue, k.key, k.max_attempts, k.timeout_s, k.traced)
        assert got == ("obs", key, attempts, timeout, traced), name
    runs = by_name["obs.runs"].args
    assert runs.model_validate({"kind": "compile", "id": "10000000-0000-4000-8000-000000000001", "end": 1})
    for bad in ({"kind": "tutor", "id": "10000000-0000-4000-8000-000000000001", "end": 1},
                {"kind": "ingest", "id": "10000000-0000-4000-8000-000000000001", "end": 0},
                {"kind": "ingest", "id": "10000000-0000-4000-8000-000000000001", "end": 101},
                {"kind": "ingest", "id": "not-a-uuid", "end": 1}):
        with pytest.raises(ValueError, match="validation error"):
            runs.model_validate(bad)


def test_traced_defaults_to_true() -> None:
    jobs = require_jobs()
    assert {f.name: f.default for f in dataclasses.fields(jobs.JobKind)}["traced"] is True


def test_the_health_slot_is_the_time_truncated_to_30_minutes() -> None:
    health = require("reader.obs.health")
    assert health.slot(datetime(2026, 11, 25, 12, 44, 59, tzinfo=UTC)) == "2026-11-25T12:30:00Z"
    assert health.slot(datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)) == "2026-11-25T12:00:00Z"
    assert health.CRON == "*/30 * * * *"


@pytest.mark.anyio
async def test_an_untraced_kind_opens_no_root_span(pg: Pg) -> None:
    """Enqueued from inside a live span, an untraced kind still carries no trace context, and its handler runs
    with no valid current span (code review round 2)."""
    from opentelemetry import trace

    seen: list[bool] = []
    base = effect_kind()

    async def handler(args: Any, conn: Any, ctx: Any) -> None:
        seen.append(trace.get_current_span().get_span_context().is_valid)
        await base.handler(args, conn, ctx)

    untraced = dataclasses.replace(base, traced=False, handler=handler)
    with obs_setup("worker") as (handle, memory):
        async with jobs_env(pg, kinds=[untraced]) as env:
            with require("reader.obs").tracer().start_as_current_span("caller"):
                await env.jobs.enqueue(KIND, {"name": "u"})
            (job_args,) = [r[0] for r in await env.rows("SELECT args FROM procrastinate_jobs")]
            await env.work()
            assert await env.effects("u") == 1
            spans: list[Any] = finished(handle, memory)
    assert "trace" not in job_args  # no trace context passed, though the caller had one
    assert seen == [False]
    assert [s for s in spans if s.name == f"job {KIND}"] == []


@pytest.mark.anyio
async def test_after_end_runs_after_commit_and_never_changes_the_outcome(pg: Pg, caplog: Any) -> None:
    """FR-EVENTS-32 (code review of 8466329): `after_end` sees the committed outcome (succeeded and dead), and
    a raising hook is logged by class name and leaves the outcome recorded."""
    import logging

    from sqlalchemy import text

    seen: list[tuple[str, str, Any]] = []

    async def hook(engine: Any, payload: Any, outcome: str) -> None:
        async with engine.connect() as conn:  # a fresh connection: the outcome is already committed
            row = (await conn.execute(text("SELECT outcome FROM job_keys WHERE args->>'name' = :n"),
                                      {"n": payload["name"]})).scalar()
        seen.append((payload["name"], outcome, row))
        raise RuntimeError("hook-secret-message")

    kind = dataclasses.replace(effect_kind(max_attempts=1), after_end=hook)
    with caplog.at_level(logging.WARNING):
        async with jobs_env(pg, kinds=[kind]) as env:
            await env.jobs.enqueue(KIND, {"name": "ok"})
            await env.jobs.enqueue(KIND, {"name": "bad", "mode": "permanent"})
            await env.work()
            outcomes = dict(await env.rows("SELECT args->>'name', outcome FROM job_keys"))
    assert outcomes == {"ok": "succeeded", "bad": "dead"}
    assert sorted(seen) == [("bad", "dead", "dead"), ("ok", "succeeded", "succeeded")]
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("after_end failed")]
    assert sorted(lines) == [f"after_end failed: kind={KIND} outcome={o} error=RuntimeError"
                             for o in ("dead", "succeeded")]
    assert not any("hook-secret-message" in r.getMessage() for r in caplog.records)


def _hooked(max_attempts: int, handler: Any = None) -> tuple[Any, list[tuple[str, str, Any]]]:
    """The test kind with an `after_end` that records (name, outcome, the key's committed outcome)."""
    from sqlalchemy import text

    calls: list[tuple[str, str, Any]] = []

    async def hook(engine: Any, payload: Any, outcome: str) -> None:
        async with engine.connect() as conn:
            row = (await conn.execute(text("SELECT outcome FROM job_keys WHERE args->>'name' = :n"),
                                      {"n": payload["name"]})).scalar()
        calls.append((payload["name"], outcome, row))

    kind = dataclasses.replace(effect_kind(max_attempts=max_attempts), after_end=hook)
    if handler is not None:
        kind = dataclasses.replace(kind, handler=handler)
    return kind, calls


@pytest.mark.anyio
@pytest.mark.parametrize(("attempts", "want"), [(1, [("lost", "dead", "dead")]), (3, [])])
async def test_a_lost_run_calls_after_end_once_when_its_count_ends_it_dead(
    pg: Pg, attempts: int, want: list[Any]
) -> None:
    """FR-EVENTS-32, R2-3: the lost-run path (the run's own session died); re-enqueued instead → no call."""
    from sqlalchemy import text

    from .test_jobs_db import _claim, _run_claimed

    async def kills_own_session(args: Any, conn: Any, ctx: Any) -> None:
        await conn.execute(text("SELECT pg_terminate_backend(pg_backend_pid())"))

    kind, calls = _hooked(attempts, kills_own_session)
    async with jobs_env(pg, kinds=[kind]) as env:
        await env.jobs.enqueue(KIND, {"name": "lost"})
        job = await _claim(env)
        assert await _run_claimed(env, kind, job) == "session_lost"
    assert calls == want


@pytest.mark.anyio
async def test_a_stalled_job_that_ends_dead_calls_after_end_once(pg: Pg) -> None:
    """FR-EVENTS-32, R2-3: the sweep's stalled path; a second sweep calls nothing more."""
    kind, calls = _hooked(1)
    async with jobs_env(pg, kinds=[kind]) as env:
        await env.jobs.enqueue(KIND, {"name": "s"})
        await env.stall_next()
        report = await env.jobs.sweep()
        assert env.key("s") in report.dead
        await env.jobs.sweep()
    assert calls == [("s", "dead", "dead")]


@pytest.mark.anyio
async def test_an_orphaned_key_that_ends_dead_calls_after_end_once(pg: Pg) -> None:
    """FR-EVENTS-32, R2-3: the sweep's reconcile path (a key with no live job, counted `orphaned`)."""
    from .test_jobs_db import _failed

    kind, calls = _hooked(1)
    async with jobs_env(pg, kinds=[kind]) as env:
        await env.jobs.enqueue(KIND, {"name": "o"})
        job = await env.stall_next()
        await env.app.job_manager.finish_job(job, _failed(), delete_job=False)  # no live job, no outcome
        await env.age_keys()
        report = await env.jobs.sweep()
        assert env.key("o") in report.dead
        assert (await env.key_row("o")).error_code == "orphaned"
        await env.age_keys()
        await env.jobs.sweep()
    assert calls == [("o", "dead", "dead")]


@pytest.mark.anyio
async def test_the_periodic_entry_enqueues_one_health_job_per_slot(pg: Pg) -> None:
    health = require("reader.obs.health")
    obs_jobs = require("reader.obs.jobs")
    async with jobs_env(pg, kinds=list(obs_jobs.JOB_KINDS)) as env:
        at = datetime(2026, 11, 25, 12, 31, tzinfo=UTC)
        await health.enqueue_slot(env.jobs, at)
        await health.enqueue_slot(env.jobs, at.replace(minute=59))  # the same slot: deduplicated by the key
        await health.enqueue_slot(env.jobs, at.replace(hour=13, minute=0))
        n = await env.scalar("SELECT count(*) FROM job_keys WHERE kind = 'obs.health'")
    assert n == 2
