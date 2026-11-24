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
    untraced = dataclasses.replace(effect_kind(), traced=False)
    with obs_setup("worker") as (handle, memory):
        async with jobs_env(pg, kinds=[untraced]) as env:
            await env.jobs.enqueue(KIND, {"name": "u"})
            (job_args,) = [r[0] for r in await env.rows("SELECT args FROM procrastinate_jobs")]
            await env.work()
            assert await env.effects("u") == 1
            spans: list[Any] = finished(handle, memory)
    assert "trace" not in job_args  # no trace context passed
    assert [s for s in spans if s.name == f"job {KIND}"] == []


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
