"""SRV-EVENTS [unit]: the worker lanes as data (FR-EVENTS-9 amended for ADR-0039 B7b).

@trace FR-EVENTS-9 FR-EVENTS-1
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from .conftest import require_jobs


def test_lanes_declare_queues_concurrency_pools_and_hooks() -> None:
    require_jobs()
    from reader.jobs.catalog import load_kinds
    from reader.jobs.lanes import LANES, lane_queues
    from reader.jobs.model import SWEEP_TASK

    catalog = load_kinds()
    main, judge = LANES["main"], LANES["judge"]
    assert lane_queues(main, catalog.queues) == [*(q for q in catalog.queues if q != "judge"), SWEEP_TASK]
    assert lane_queues(judge, catalog.queues) == ["judge"]
    assert (main.concurrency, judge.concurrency) == (1, 4)
    assert (main.pool_size, judge.pool_size) == (5, 8)
    assert (main.sweep, judge.sweep) == (True, False)
    assert main.hook_modules is None
    assert judge.hook_modules == ("reader.agent.lane",)


def test_a_queue_no_lane_serves_is_refused() -> None:
    require_jobs()
    from reader.jobs.catalog import load_kinds
    from reader.jobs.lanes import LANES, Lane
    from reader.jobs.model import JobError

    narrow = MappingProxyType(
        {"main": Lane("main", 1, ("search",), True, None), "judge": LANES["judge"]}
    )
    with pytest.raises(JobError) as caught:
        load_kinds(lanes=narrow)
    assert caught.value.code == "queue_unserved"


def test_only_judge_find_may_use_the_judge_queue() -> None:
    import dataclasses

    jobs = require_jobs()
    from .test_jobs_unit import _kind_named

    with pytest.raises(jobs.JobError) as caught:
        jobs.Catalog([dataclasses.replace(_kind_named("other.kind"), queue="judge")])
    assert caught.value.code == "queue_reserved"


async def test_unknown_lane_fails_before_the_database() -> None:
    require_jobs()
    import asyncio

    from reader.jobs.model import JobError
    from reader.jobs.worker import run

    with pytest.raises(JobError) as caught:
        await run(None, None, asyncio.Event(), lane="fast")  # type: ignore[arg-type]
    assert caught.value.code == "lane_unknown"


pytestmark = pytest.mark.anyio
