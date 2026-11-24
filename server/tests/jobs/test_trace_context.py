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


@pytest.mark.parametrize(
    ("ending", "outcome", "code"),
    [
        ("cancelled", "aborted", None),
        ("lost", "failed", "session_lost"),
        ("lost_dead", "dead", "session_lost"),
    ],
)
async def test_root_outcome_of_a_cancelled_or_lost_run(ending: str, outcome: str, code: str | None) -> None:
    """Round 2: the wrapper's `aborted` and `session_lost` marks, without a database (steps faked)."""
    import asyncio

    from reader.jobs import queue
    from reader.jobs.model import JobFailed

    prop = require("reader.obs.propagation")

    class Fake:
        async def _attempt(self, *_a: Any) -> Any:
            if ending == "cancelled":
                raise asyncio.CancelledError
            return (RuntimeError("x"), False, True)  # (error, timed out, session lost)

        async def _count_lost_run(self, *_a: Any) -> bool:
            return ending == "lost_dead"

    with obs_setup("worker") as (handle, memory):
        with prop.job_span(KIND, 7, "k", {}), pytest.raises((asyncio.CancelledError, JobFailed)):
            await queue.Jobs._run_in_span(Fake(), effect_kind(), 7, {"name": "x"})  # type: ignore[arg-type]
        (root,) = _roots(finished(handle, memory))
    attrs = dict(root.attributes)
    assert attrs["reader.job.outcome"] == outcome
    assert attrs.get("reader.error_code") == code


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


async def test_root_outcomes_permanent_exhausted_skipped(pg: Pg) -> None:
    """Round 1 survivors: the root span carries the key's outcome for each way a run can end."""
    with obs_setup("worker") as (handle, memory):
        async with jobs_env(pg) as env:
            await env.jobs.enqueue(KIND, {"name": "perm", "mode": "permanent"})
            await env.jobs.enqueue(KIND, {"name": "full"})
            await env.execute("UPDATE job_keys SET attempts = 3 WHERE key = :k", k=env.key("full"))
            await env.jobs.enqueue(KIND, {"name": "done"})
            await env.execute(
                "UPDATE job_keys SET outcome = 'succeeded', finished_at = now() WHERE key = :k",
                k=env.key("done"),
            )
            await env.work()
            ids = {name: (await _job_rows(env, name))[0]["id"] for name in ("perm", "full", "done")}
            roots = {s.attributes["reader.job.id"]: s for s in _roots(finished(handle, memory))}
    perm, full, done = (dict(roots[ids[n]].attributes) for n in ("perm", "full", "done"))
    assert (perm["reader.job.outcome"], perm["reader.error_class"]) == ("dead", "RuntimeError")
    assert "reader.error_code" not in perm
    assert roots[ids["perm"]].status.status_code.name == "ERROR"
    assert (full["reader.job.outcome"], full["reader.error_code"], full["reader.job.attempt"]) == (
        "dead",
        "exhausted",
        3,
    )
    assert done["reader.job.outcome"] == "skipped"
    assert "reader.job.attempt" not in done
    assert roots[ids["done"]].status.status_code.name == "UNSET"


async def test_a_wrapper_error_that_ends_the_key_dead_is_marked_dead(
    pg: Pg, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reader.jobs import queue

    async def broken(self: Any, *_args: Any, **_kw: Any) -> Any:
        raise RuntimeError("wrapper broke")

    monkeypatch.setattr(queue.Jobs, "_attempt", broken)
    with obs_setup("worker") as (handle, memory):
        async with jobs_env(pg) as env:
            await env.jobs.enqueue(KIND, {"name": "w"})
            await env.execute("UPDATE job_keys SET attempts = 2 WHERE key = :k", k=env.key("w"))
            await env.work()
            (root,) = _roots(finished(handle, memory))
            row = await env.key_row("w")
    assert row[1] == "dead"
    attrs = root.attributes
    assert (attrs["reader.job.outcome"], attrs["reader.error_code"]) == ("dead", "wrapper_error")


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
