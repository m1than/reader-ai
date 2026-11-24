"""SRV-OBS [db]: the job wrapper's session marker (FR-OBS-12 e): a `job.session` child of every `pipeline.*`
job's root carrying the run id from the validated args, and none for any other kind, even one whose args name
a run.

@trace FR-OBS-12 INV-OBS-3
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from obs.conftest import finished, obs_setup

from .conftest import Pg, jobs_env, require_jobs

pytestmark = pytest.mark.anyio
RUN = str(uuid.UUID("10000000-0000-4000-8000-0000000000aa"))


class StageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    step: int
    stage: str
    input_sha256: str


class OtherArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    run_id: str


async def _noop(args: Any, conn: Any, ctx: Any) -> None:
    return None


def _kinds() -> list[Any]:
    jobs = require_jobs()
    return [
        jobs.JobKind(name="pipeline.stage", queue="test", args=StageArgs, key=("run_id", "step"),
                     handler=_noop, max_attempts=1, transient=(), timeout_s=30),
        jobs.JobKind(name="test.effect", queue="test", args=OtherArgs, key=("name",), handler=_noop,
                     max_attempts=1, transient=(), timeout_s=30),
    ]


async def test_marker_only_under_pipeline_roots(pg: Pg) -> None:
    with obs_setup("worker") as (handle, memory):
        async with jobs_env(pg, kinds=_kinds()) as env:
            await env.jobs.enqueue("pipeline.stage",
                                   {"run_id": RUN, "step": 1, "stage": "draft", "input_sha256": "1" * 64})
            await env.jobs.enqueue("test.effect", {"name": "x", "run_id": RUN})
            await env.work()
            spans = finished(handle, memory)
    by_name: dict[str, list[Any]] = {}
    for s in spans:
        by_name.setdefault(s.name, []).append(s)
    (stage_root,) = by_name["job pipeline.stage"]
    (other_root,) = by_name["job test.effect"]
    (marker,) = by_name["job.session"]  # exactly one: none under the other kind's root
    assert marker.parent is not None
    assert marker.parent.span_id == stage_root.context.span_id
    assert marker.context.trace_id == stage_root.context.trace_id != other_root.context.trace_id
    assert marker.attributes["reader.run_id"] == RUN
    assert marker.attributes["session.id"] == RUN
    assert stage_root.attributes["reader.job.outcome"] == "succeeded"
