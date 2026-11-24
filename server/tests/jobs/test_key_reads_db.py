"""SRV-EVENTS FR-EVENTS-18: the published key reads `key_rows_of` and `recent_keys_of`, and revision jobs_0002. [db]

@trace FR-EVENTS-18
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from .conftest import KIND, Pg, jobs_env

pytestmark = pytest.mark.anyio


async def _seed(env: object) -> None:
    await env.jobs.enqueue(KIND, {"name": "ok"})  # type: ignore[attr-defined]
    await env.jobs.enqueue(KIND, {"name": "perm", "mode": "permanent"})  # type: ignore[attr-defined]
    await env.jobs.enqueue(KIND, {"name": "perm2", "mode": "permanent"})  # type: ignore[attr-defined]
    await env.jobs.enqueue(KIND, {"name": "queued", "sleep_s": 0})  # type: ignore[attr-defined]
    for _ in range(2):
        await env.work()  # type: ignore[attr-defined]


async def test_key_rows_of(pg: Pg) -> None:
    from reader.jobs import queue

    async with jobs_env(pg) as env:
        await _seed(env)
        keys = [
            env.key("ok"),
            env.key("perm"),
            "test.effect:" + "0" * 64,
            "other.kind:" + env.key("ok").split(":")[1],
        ]
        async with env.engine.connect() as conn:
            rows = await queue.key_rows_of(conn, keys)
    by_key = {r.key: r for r in rows}
    assert set(by_key) == {env.key("ok"), env.key("perm")}  # unknown keys and a wrong prefix are omitted
    ok, perm = by_key[env.key("ok")], by_key[env.key("perm")]
    assert (ok.kind, ok.outcome, ok.error_code, ok.args) == (
        KIND,
        "succeeded",
        None,
        {"name": "ok", "mode": "ok", "sleep_s": 0.0, "message": ""},
    )
    assert (perm.outcome, perm.error_code, perm.attempts) == ("dead", "RuntimeError", 1)
    assert isinstance(ok.job_id, int) and ok.created_at is not None and ok.finished_at is not None


async def test_key_rows_of_skips_a_stored_row_whose_kind_is_not_its_prefix(pg: Pg) -> None:
    """Round 1 survivor: a real row (kind other.kind, key test.effect:…) is never returned for that key."""
    from sqlalchemy import text

    from reader.jobs import queue

    async with jobs_env(pg) as env:
        await _seed(env)
        key = env.key("ok")
        async with env.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO job_keys (kind, key, args, attempts, outcome, created_at, updated_at)"
                    " VALUES ('other.kind', :k, '{}', 1, 'dead', now(), now())"
                ),
                {"k": key},
            )
        async with env.engine.connect() as conn:
            rows = await queue.key_rows_of(conn, [key])
            await conn.execute(text("SET enable_seqscan = off"))
            plan = "\n".join(
                str(r[0])
                for r in (await conn.execute(text("EXPLAIN " + queue.KEY_ROWS_SQL), {"keys": [key]})).all()
            )
    assert [(r.kind, r.key) for r in rows] == [(KIND, key)]
    assert "job_keys_pkey" in plan, plan


async def test_recent_keys_of(pg: Pg) -> None:
    from reader.jobs import queue
    from reader.jobs.model import JobError

    async with jobs_env(pg) as env:
        await _seed(env)
        since = datetime.now(UTC) - timedelta(hours=1)
        async with env.engine.connect() as conn:
            dead, truncated = await queue.recent_keys_of(conn, [KIND], "dead", since, 10)
            one, cut = await queue.recent_keys_of(conn, [KIND], "dead", since, 1)
            none, _ = await queue.recent_keys_of(
                conn, [KIND], "dead", datetime.now(UTC) + timedelta(hours=1), 10
            )
            other, _ = await queue.recent_keys_of(conn, ["other.kind"], "dead", since, 10)
            done, _ = await queue.recent_keys_of(conn, [KIND], "succeeded", since, 10)
            for bad in (0, 501):
                with pytest.raises(JobError):
                    await queue.recent_keys_of(conn, [KIND], "dead", since, bad)
            from sqlalchemy import text

            await conn.execute(text("SET enable_seqscan = off"))
            plan = (
                await conn.execute(
                    text(
                        "EXPLAIN SELECT * FROM job_keys WHERE kind = 'x' AND outcome = 'dead' AND finished_at >= now()"
                        " ORDER BY finished_at DESC LIMIT 5"
                    )
                )
            ).all()
    assert {r.key for r in dead} == {env.key("perm"), env.key("perm2")}
    assert truncated is False
    assert len(one) == 1 and cut is True
    assert [r.finished_at for r in dead] == sorted((r.finished_at for r in dead), reverse=True)
    assert none == [] and other == []
    assert {r.key for r in done} == {env.key("ok"), env.key("queued")}
    assert "job_keys_kind_outcome_finished" in " ".join(str(r[0]) for r in plan)


async def test_jobs_0002_index_and_downgrade(pg: Pg) -> None:
    async with jobs_env(pg) as env:
        names = [
            r[0] for r in await env.rows("SELECT indexname FROM pg_indexes WHERE tablename = 'job_keys'")
        ]
        assert "job_keys_kind_outcome_finished" in names
        heads = [r[0] for r in await env.rows("SELECT version_num FROM alembic_version")]
    assert "jobs_0003" in heads  # jobs_0002's index above; jobs_0003 (FR-EVENTS-30) is the branch head
