"""SRV-EVENTS FR-EVENTS-19 with SRV-OBS FR-OBS-5: the `trace` task kwarg and one linked trace per job. [db]

@trace FR-EVENTS-19 FR-OBS-5 INV-OBS-4 FR-OBS-7
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from obs.conftest import finished, obs_setup, require

from .conftest import KIND, Pg, effect_kind, jobs_env

pytestmark = pytest.mark.anyio


async def _job_rows(env: Any, name: str) -> list[dict[str, Any]]:
    rows = await env.rows(
        "SELECT id, args::text FROM procrastinate_jobs WHERE lock = :k ORDER BY id", k=env.key(name)
    )
    return [{"id": int(i), **json.loads(a)} for i, a in rows]


def _roots(spans: list[Any]) -> list[Any]:
    return sorted((s for s in spans if s.name == f"job {KIND}"), key=lambda s: s.attributes["reader.job.id"])


def test_kind_modules_list_obs_jobs() -> None:
    from reader.jobs.catalog import KIND_MODULES, load_kinds

    assert "reader.obs.jobs" in KIND_MODULES
    assert "obs.retention" in load_kinds()


async def test_enqueue_carries_the_current_span_and_the_run_links_to_it(pg: Pg) -> None:
    prop = require("reader.obs.propagation")
    ids = require("reader.obs.ids")
    with obs_setup("worker") as (handle, memory):
        async with jobs_env(pg) as env:
            with require("reader.obs").tracer().start_as_current_span("POST /api/lessons/{lesson_id}"):
                parent = prop.current_traceparent()
                await env.jobs.enqueue(KIND, {"name": "a"})
            (job,) = await _job_rows(env, "a")
            assert job["trace"] == parent
            key_args = [r[0] for r in await env.rows("SELECT args FROM job_keys")]
            assert "trace" not in json.dumps(key_args)
            await env.work()
            (root,) = _roots(finished(handle, memory))
    assert root.context.trace_id == ids.derive_trace_id(job["id"], env.key("a"))
    assert [format(link.context.span_id, "016x") for link in root.links] == [parent.split("-")[2]]
    assert root.attributes["reader.job.outcome"] == "succeeded"
    assert root.attributes["reader.job.attempt"] == 0


async def test_no_span_no_trace_kwarg(pg: Pg) -> None:
    async with jobs_env(pg) as env:
        await env.jobs.enqueue(KIND, {"name": "b"})
        (job,) = await _job_rows(env, "b")
    assert "trace" not in job


async def test_retry_links_to_the_failed_run(pg: Pg) -> None:
    with obs_setup("worker") as (handle, memory):
        async with jobs_env(pg) as env:
            await env.jobs.enqueue(KIND, {"name": "t", "mode": "transient"})
            await env.work()  # attempt 1 fails transient: re-enqueued from inside the run (and so on)
            first, second = (await _job_rows(env, "t"))[:2]
            roots = _roots(finished(handle, memory))
    assert "trace" not in first
    run1 = roots[0]
    assert run1.attributes["reader.job.outcome"] == "failed"
    assert run1.attributes["reader.error_class"] == "Transient"
    assert second["trace"].split("-")[1] == format(run1.context.trace_id, "032x")
    assert second["trace"].split("-")[2] == format(run1.context.span_id, "016x")


async def test_handler_enqueue_carries_the_running_job(pg: Pg) -> None:
    from pydantic import BaseModel, ConfigDict

    jobs = require("reader.jobs")
    holder: dict[str, Any] = {}

    class Args(BaseModel):
        model_config = ConfigDict(extra="forbid")
        name: str

    async def chain(args: Args, conn: Any, ctx: Any) -> None:
        await holder["jobs"].enqueue(KIND, {"name": f"{args.name}-next"})

    chained = jobs.JobKind(
        name="test.chain",
        queue="test",
        args=Args,
        key=("name",),
        handler=chain,
        max_attempts=1,
        transient=(),
        timeout_s=30.0,
    )
    with obs_setup("worker", job_kinds=frozenset({KIND, "test.chain"})) as (handle, memory):
        async with jobs_env(pg, kinds=[effect_kind(), chained]) as env:
            holder["jobs"] = env.jobs
            await env.jobs.enqueue("test.chain", {"name": "c"})
            await env.work()
            nxt = (await _job_rows(env, "c-next"))[0]
            spans = finished(handle, memory)
    chain_root = next(s for s in spans if s.name == "job test.chain")
    assert nxt["trace"].split("-")[1] == format(chain_root.context.trace_id, "032x")
    effect_root = next(s for s in spans if s.name == f"job {KIND}")
    assert effect_root.context.trace_id != chain_root.context.trace_id  # one trace per job
    assert [format(link.context.trace_id, "032x") for link in effect_root.links] == [
        format(chain_root.context.trace_id, "032x")
    ]


async def test_sweep_reenqueue_has_no_link(pg: Pg) -> None:
    with obs_setup("worker") as (handle, memory):
        async with jobs_env(pg) as env:
            with require("reader.obs").tracer().start_as_current_span("POST /api/lessons/{lesson_id}"):
                await env.jobs.enqueue(KIND, {"name": "s"})
            await env.stall_next()
            await env.jobs.sweep(stalled_after_s=0)
            rows = await _job_rows(env, "s")
            await env.work()
            roots = _roots(finished(handle, memory))
    assert "trace" in rows[0]
    assert "trace" not in rows[-1]
    assert list(roots[-1].links) == []


async def test_span_failures_never_change_the_outcome(pg: Pg, monkeypatch: pytest.MonkeyPatch) -> None:
    prop = require("reader.obs.propagation")

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("span helper failed")

    monkeypatch.setattr(prop, "current_traceparent", broken)
    async with jobs_env(pg) as env:
        await env.jobs.enqueue(KIND, {"name": "x"})
        await env.work()
        row = await env.key_row("x")
    assert row[1] == "succeeded"
