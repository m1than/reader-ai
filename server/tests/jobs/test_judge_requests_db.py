"""SRV-EVENTS: `judge_requests` and its store (FR-EVENTS-33), the collectors (FR-EVENTS-34) and the key retention
(FR-EVENTS-11 amended), ADR-0039 B7b.

@trace FR-EVENTS-33 FR-EVENTS-34 FR-EVENTS-11
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from .conftest import Env, Pg, effect_kind, jobs_env, judge_stub_kind, require_jobs

pytestmark = pytest.mark.anyio
REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "server" / "src"
CARDS = (("spn:a", "alpha card"), ("spn:b", "beta card"))
SHA = "a" * 64


async def _user(env: Env) -> uuid.UUID:
    from sqlalchemy import text

    async with env.engine.begin() as conn:  # committed: judge_requests references it
        value = (await conn.execute(text(
            "INSERT INTO users (handle, handle_skeleton, role, webauthn_user_id, display_name, consent_evaluation)"
            " VALUES ('ivan', 'ivan', 'owner', decode(repeat('ab', 64), 'hex'), 'I', false) RETURNING id"
        ))).scalar_one()
    return uuid.UUID(str(value))


async def _db_now(env: Env) -> Any:
    return await env.scalar("SELECT now()")


def _new(owner: uuid.UUID, deadline: Any, **changes: Any) -> Any:
    from reader.jobs.judge_requests import NewJudgeRequest

    fields: dict[str, Any] = {
        "request_id": uuid.uuid4(), "owner_id": owner, "line": "tutor", "run_id": uuid.uuid4(),
        "run_cap_usd": Decimal("0.50"), "lesson_id": None, "daily_budget_usd": None, "input_sha256": SHA,
        "deadline": deadline, "query": "what is a hardened key", "cards": CARDS, "pack_ids": ("p1",),
    }
    fields.update(changes)
    return NewJudgeRequest(**fields)


async def _put(env: Env, new: Any) -> None:
    from reader.jobs.judge_requests import STORE

    async with env.engine.begin() as conn:
        await STORE.put(conn, new)


async def test_put_get_delete_round_trip(pg: Pg) -> None:
    require_jobs()
    from reader.jobs.judge_requests import STORE

    async with jobs_env(pg) as env:
        owner = await _user(env)
        new = _new(owner, await _db_now(env) + timedelta(seconds=5))
        await _put(env, new)
        async with env.engine.connect() as conn:
            got = await STORE.get(conn, new.request_id)
        assert got is not None
        assert (got.request_id, got.owner_id, got.line, got.query, got.cards, got.pack_ids) == (
            new.request_id, owner, "tutor", new.query, CARDS, ("p1",)
        )
        assert got.deadline == new.deadline and got.created_at < got.deadline
        assert got.run_cap_usd == Decimal("0.50")
        async with env.engine.begin() as conn:
            await STORE.delete(conn, new.request_id)
            await STORE.delete(conn, new.request_id)  # a second delete is a no-op
            assert await STORE.get(conn, new.request_id) is None


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"line": "eval"}, "line"),
        ({"cards": ()}, "cards"),
        ({"cards": (("only-one",),)}, "cards"),
        ({"cards": (("a", 1),)}, "cards"),
        ({"pack_ids": (1,)}, "pack_ids"),
        ({"input_sha256": "ABC"}, "input_sha256"),
        ({"deadline_s": 9.0}, "deadline"),  # factor 3 x 9 s = 27 s > 30 - 5
        ({"deadline_s": -1.0}, "deadline"),
    ],
)
async def test_put_refuses_each_invalid_field_by_name(pg: Pg, change: dict[str, Any], field: str) -> None:
    require_jobs()
    from reader.jobs.judge_requests import JudgeRequestInvalid

    async with jobs_env(pg) as env:
        owner = await _user(env)
        seconds = change.pop("deadline_s", 5.0)
        new = _new(owner, await _db_now(env) + timedelta(seconds=seconds), **change)
        with pytest.raises(JudgeRequestInvalid) as caught:
            await _put(env, new)
        assert (caught.value.code, caught.value.field) == ("judge_request_invalid", field)
        assert await env.scalar("SELECT count(*) FROM judge_requests") == 0


async def test_window_boundary_eight_seconds_accepted(pg: Pg) -> None:
    async with jobs_env(pg) as env:
        owner = await _user(env)
        await _put(env, _new(owner, await _db_now(env) + timedelta(seconds=8)))
        assert await env.scalar("SELECT count(*) FROM judge_requests") == 1


@pytest.mark.parametrize(
    "values",
    [
        "'eval', repeat('a', 64), now(), now() + interval '5 s'",
        "'tutor', 'not-a-hash', now(), now() + interval '5 s'",
        "'tutor', repeat('a', 64), now(), now() - interval '1 s'",
    ],
)
async def test_checks_refuse_raw_inserts(pg: Pg, values: str) -> None:
    from sqlalchemy.exc import IntegrityError

    async with jobs_env(pg) as env:
        owner = await _user(env)
        with pytest.raises(IntegrityError):
            await env.execute(
                "INSERT INTO judge_requests (request_id, owner_id, line, input_sha256, created_at, deadline, run_id,"
                f" run_cap_usd, query, cards, pack_ids) VALUES (gen_random_uuid(), :o, {values},"
                " gen_random_uuid(), 0.5, 'q', '[]', ARRAY[]::text[])",
                o=owner,
            )


async def test_deleting_the_owner_removes_the_rows(pg: Pg) -> None:
    async with jobs_env(pg) as env:
        owner = await _user(env)
        await _put(env, _new(owner, await _db_now(env) + timedelta(seconds=5)))
        await env.execute("DELETE FROM users WHERE id = :o", o=owner)
        assert await env.scalar("SELECT count(*) FROM judge_requests") == 0


async def test_fresh_upgrade_and_branch_downgrade(pg: Pg) -> None:
    from sqlalchemy import text

    from reader.db import alembic_config

    async with jobs_env(pg) as env:  # `upgrade heads` on a fresh, empty database
        heads = {r[0] for r in await env.rows("SELECT version_num FROM alembic_version")}
        assert "jobs_0004" in heads

        def downgrade(sync_conn: Any) -> None:
            from alembic import command

            config = alembic_config()
            config.attributes["connection"] = sync_conn
            command.downgrade(config, "jobs_0003")

        async with env.engine.connect() as conn:
            await conn.run_sync(downgrade)
            await conn.commit()
            tables = set((await conn.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ))).scalars())
        assert "judge_requests" not in tables
        assert {"job_keys", "audit_log", "users"} <= tables


def test_no_import_cycle_between_the_store_and_the_lane() -> None:
    check = (
        "import sys, reader.jobs.judge_requests as r; assert 'reader.agent.lane' not in sys.modules;"
        " import reader.agent.lane as lane; assert lane.JOB_KINDS[0].name == 'judge.find'"
    )
    first = subprocess.run([sys.executable, "-c", check], capture_output=True, text=True, check=False,  # noqa: S603
                           env={"PYTHONPATH": str(SRC), "PATH": ""})
    assert first.returncode == 0, first.stderr[-2000:]
    second = subprocess.run([sys.executable, "-c", "import reader.agent.lane"], capture_output=True,  # noqa: S603
                            text=True, check=False, env={"PYTHONPATH": str(SRC), "PATH": ""})
    assert second.returncode == 0, second.stderr[-2000:]


@contextlib.asynccontextmanager
async def _lane_env(pg: Pg) -> AsyncIterator[tuple[Env, Any]]:
    """The real `judge.find` kind with the production STORE, a config binding the slot, and a provider that fails."""
    from reader.agent import lane
    from reader.agent.errors import agent_error
    from reader.config import load_config
    from reader.jobs.judge_requests import STORE

    config = load_config(REPO / "deploy" / "reader.example.yaml")

    class Failing:
        async def run(self, call: Any) -> Any:
            raise agent_error("provider_unavailable")

    @contextlib.asynccontextmanager
    async def provider(_engine: Any) -> AsyncIterator[Any]:
        yield Failing()

    kind = lane.make_kind(lane.Deps(provider=provider, config=lambda: config, requests=STORE))
    async with jobs_env(pg, kinds=[kind]) as env:
        yield env, kind


async def _work_judge_queue(env: Env) -> None:
    await env.app.run_worker_async(
        queues=["judge"], wait=False, concurrency=1, install_signal_handlers=False, listen_notify=False
    )


async def test_store_serves_the_lane_handler_end_to_end(pg: Pg) -> None:
    from reader.agent import judges

    async with _lane_env(pg) as (env, _kind):
        owner = await _user(env)
        new = _new(owner, await _db_now(env) + timedelta(seconds=5))
        new = _new(owner, new.deadline, input_sha256=judges.find_input_sha256(owner, new.query, list(CARDS)))
        await _put(env, new)
        await env.jobs.enqueue("judge.find", {"request_id": str(new.request_id), "input_sha256": new.input_sha256})
        await _work_judge_queue(env)
        assert await env.scalar("SELECT count(*) FROM judge_requests") == 0  # read, then deleted at the end
        key = await env.rows("SELECT outcome FROM job_keys WHERE kind = 'judge.find'")
        assert key == [("succeeded",)]


async def _raw_expired(env: Env, owner: uuid.UUID, seconds_past: int) -> None:
    await env.execute(
        "INSERT INTO judge_requests (request_id, owner_id, line, run_id, run_cap_usd, input_sha256, created_at,"
        " deadline, query, cards, pack_ids) VALUES (gen_random_uuid(), :o, 'tutor', gen_random_uuid(), 0.5,"
        " repeat('a', 64), now() - make_interval(secs => :p + 5), now() - make_interval(secs => :p), 'q',"
        " '[[\"c\", \"t\"]]', ARRAY['p1'])",
        o=owner, p=seconds_past,
    )


async def test_reap_deletes_only_rows_a_minute_past_their_deadline(pg: Pg) -> None:
    async with jobs_env(pg, kinds=[effect_kind(), judge_stub_kind()]) as env:
        owner = await _user(env)
        await _raw_expired(env, owner, 61)
        await _raw_expired(env, owner, 30)
        await env.jobs.judge_reap()
        assert await env.scalar("SELECT count(*) FROM judge_requests") == 1


async def _finished_job(env: Env, name: str, queue: str, status: Any, hours_ago: float) -> int:
    await env.app.configure_task(name=name, queue=queue, allow_unknown=True).defer_async()
    worker_id = await env.app.job_manager.register_worker()
    job = await env.app.job_manager.fetch_job(queues=[queue], worker_id=worker_id)
    assert job is not None
    await env.app.job_manager.finish_job(job, status, delete_job=False)
    await env.execute(
        "UPDATE procrastinate_events SET at = now() - make_interval(secs => :s) WHERE job_id = :j",
        s=hours_ago * 3600, j=job.id,
    )
    return int(job.id)


async def test_gc_retention_periodic_rows_outputs_and_isolation(pg: Pg, monkeypatch: Any) -> None:
    from procrastinate.jobs import Status

    from reader.agent import judges
    from reader.jobs.model import SWEEP_TASK

    calls: list[Any] = []

    async def spy(conn: Any, *a: Any, **kw: Any) -> int:
        calls.append(conn)
        return 0

    monkeypatch.setattr(judges, "expire", spy)
    async with jobs_env(pg, kinds=[effect_kind(), judge_stub_kind()]) as env:
        owner = await _user(env)
        await _raw_expired(env, owner, 61)
        rows = [
            ("judge.find", "judge.find:old", "succeeded", 2 * 86_400),
            ("judge.find", "judge.find:young", "succeeded", 3_600),
            ("judge.find", "judge.find:open", None, 2 * 86_400),
            ("test.effect", "test.effect:old", "succeeded", 9 * 86_400),
        ]
        for kind, key, outcome, age in rows:
            await env.execute(
                "INSERT INTO job_keys (kind, key, args, attempts, outcome, job_id, created_at, updated_at,"
                " finished_at) VALUES (:k, :key, '{}', 1, :o, NULL, now(), now(),"
                " CASE WHEN CAST(:o AS text) IS NULL THEN NULL ELSE now() - make_interval(secs => :a) END)",
                k=kind, key=key, o=outcome, a=age,
            )
        old_judge = await _finished_job(env, "judge.find", "judge", Status.FAILED, 48)
        await env.app.configure_task(name="judge.find", queue="judge", allow_unknown=True).defer_async()
        old_tick = await _finished_job(env, "jobs.sweep", SWEEP_TASK, Status.SUCCEEDED, 48)
        old_fail = await _finished_job(env, "jobs.sweep", SWEEP_TASK, Status.FAILED, 48)

        async def broken() -> None:
            raise RuntimeError("step 1 fails")

        monkeypatch.setattr(env.jobs, "_expire_requests", broken)
        await env.jobs.judge_gc()
        keys = {r[0] for r in await env.rows("SELECT key FROM job_keys")}
        assert keys == {"judge.find:young", "judge.find:open", "test.effect:old"}
        ids = {r[0] for r in await env.rows("SELECT id FROM procrastinate_jobs")}
        assert old_judge not in ids and old_tick not in ids and old_fail in ids
        todo = await env.scalar("SELECT count(*) FROM procrastinate_jobs WHERE status = 'todo'")
        assert todo == 1  # the todo judge job is kept
        assert len(calls) == 1  # judges.expire once per judge_gc run, after a failing step 1
        monkeypatch.undo()
        await env.jobs.judge_gc()
        assert await env.scalar("SELECT count(*) FROM judge_requests") == 0
