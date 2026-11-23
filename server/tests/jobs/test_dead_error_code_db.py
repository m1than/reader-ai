"""SRV-EVENTS FR-EVENTS-17: `dead_error_code_of`, the published read of a dead key's error code. [db]

@trace FR-EVENTS-17
"""

from __future__ import annotations

import pytest

from .conftest import KIND, Pg, jobs_env

pytestmark = pytest.mark.anyio


async def test_dead_error_code_of(pg: Pg) -> None:
    from reader.jobs import queue

    assert hasattr(queue, "dead_error_code_of"), "reader.jobs.queue.dead_error_code_of is missing"

    async def code(env: object, name: str) -> str | None:
        async with env.engine.connect() as conn:  # type: ignore[attr-defined]
            result: str | None = await queue.dead_error_code_of(conn, KIND, env.key(name))  # type: ignore[attr-defined]
            return result

    async with jobs_env(pg, max_attempts=2) as env:
        assert await code(env, "none") is None  # no row
        await env.jobs.enqueue(KIND, {"name": "ok"})
        assert await code(env, "ok") is None  # queued
        await env.jobs.enqueue(KIND, {"name": "perm", "mode": "permanent"})
        await env.jobs.enqueue(KIND, {"name": "trans", "mode": "transient"})
        for _ in range(3):
            await env.work()
        assert await env.jobs.job_status(KIND, env.key("ok")) == "succeeded"
        assert await code(env, "ok") is None
        assert await env.jobs.job_status(KIND, env.key("perm")) == "dead"
        assert await code(env, "perm") == "RuntimeError"
        assert await env.jobs.job_status(KIND, env.key("trans")) == "dead"
        assert await code(env, "trans") == "Transient"
        await env.jobs.requeue_dead(KIND, env.key("perm"))
        assert await code(env, "perm") is None
