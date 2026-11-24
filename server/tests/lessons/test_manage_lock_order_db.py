"""The lock order itself, read from `pg_locks` (SRV-LESSON INV-LESSON-23; SRV-INGEST FR-INGEST-31). [db]

@trace INV-LESSON-23 FR-INGEST-31

No timing race decides an outcome here: a test connection holds one lesson's run lock ("RCMP") in an open
transaction; each request starts in its own task and the test waits until `pg_locks` shows a backend waiting for
exactly that lock. Then the advisory locks that backend already holds are the oracle: the plan-course lock
("RCPC"), the lower lesson's run lock when the held one is the higher of the course's two lessons, and never the
owner's position lock ("RCPO"), which comes last. Once the test commits, the request completes.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from pipeline.conftest import MATERIAL, client, config_text, pipeline_env

from .conftest import Pg

pytestmark = pytest.mark.anyio
RCPC, RCMP, RCPO = 0x52435043, 0x52434D50, 0x5243504F
WAIT_S = 10
MATERIAL_ID = "synthetic-1"  # the config id pipeline_env's material row carries

Request = Callable[[httpx.AsyncClient], Awaitable[httpx.Response]]


async def _key(env: Any, namespace: int, key: str) -> tuple[int, int]:
    """(classid, objid) of pg_advisory_xact_lock(namespace, hashtext(key)) as pg_locks shows it."""
    objid = await env.scalar("SELECT hashtext(:k)::bigint & 4294967295", k=key)
    return namespace, int(objid)


async def _waiter(env: Any, awaited: tuple[int, int], holder: int) -> int:
    """The pid of the backend waiting for `awaited`, polled until it appears (the request reached the lock)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT_S
    while loop.time() < deadline:
        pid = await env.scalar(
            "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND NOT granted AND objsubid = 2"
            " AND classid::bigint = :ns AND objid::bigint = :obj AND pid <> :h",
            ns=awaited[0], obj=awaited[1], h=holder,
        )
        if pid is not None:
            return int(pid)
        await asyncio.sleep(0.02)
    raise AssertionError(f"no backend waits for {awaited}")


async def _held(env: Any, pid: int) -> set[tuple[int, int]]:
    rows = await env.rows(
        "SELECT classid::bigint, objid::bigint FROM pg_locks WHERE pid = :p AND locktype = 'advisory' AND granted",
        p=pid,
    )
    return {(int(r[0]), int(r[1])) for r in rows}


async def _oracle(
    env: Any, c: httpx.AsyncClient, request: Request, lesson: uuid.UUID, expected: set[tuple[int, int]]
) -> httpx.Response:
    """Hold `lesson`'s run lock, start `request`, assert what its backend holds while it waits, then release."""
    from sqlalchemy import text

    awaited = await _key(env, RCMP, str(lesson))
    async with env.engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_xact_lock(:ns, hashtext(:k))"), {"ns": RCMP, "k": str(lesson)})
        holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        task = asyncio.create_task(request(c))
        try:
            pid = await _waiter(env, awaited, holder_pid)
            held = await _held(env, pid)
            assert not task.done()
        finally:
            await holder.commit()
        response = await asyncio.wait_for(task, WAIT_S)
    assert held == expected, (held, expected)
    assert all(ns != RCPO for ns, _ in held)
    return response


async def test_each_request_waits_on_the_run_lock_holding_only_the_earlier_locks(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reader.config import load_config

    async with pipeline_env(pg, tmp_path) as env:
        await env.import_plan()
        ivan = env.users["ivan"]
        course = await env.scalar("SELECT course_id FROM lessons WHERE id = :l", l=env.lesson)
        low, high = sorted(
            r[0] for r in await env.rows("SELECT id FROM lessons WHERE course_id = :c", c=course)
        )
        plan_lock = await _key(env, RCPC, f"{ivan}:synthetic")
        low_lock = await _key(env, RCMP, str(low))
        title = await env.scalar("SELECT title FROM courses WHERE id = :c", c=course)

        async def close_runs() -> None:
            from sqlalchemy import text

            async with env.engine.begin() as conn:
                await conn.execute(
                    text("UPDATE compile_runs SET finished_at = now(), cancelled_at = now() WHERE finished_at IS NULL")
                )

        async with client(env) as c:
            # compile (create_run) of L02, the lesson with source spans: RCPC, then the lesson's run lock
            compiled = env.lesson
            r = await _oracle(
                env, c, lambda c: c.post(f"/api/lessons/{compiled}/compile", json={}), compiled, {plan_lock}
            )
            assert r.status_code == 202, r.text
            await close_runs()
            # lesson skip and delete: RCPC, then the lesson's run lock (no position lock at all)
            for path, method, undo in (
                (f"/api/lessons/{high}/skip", "POST", f"/api/lessons/{high}/unskip"),
                (f"/api/lessons/{high}", "DELETE", f"/api/lessons/{high}/restore"),
            ):
                r = await _oracle(env, c, lambda c, p=path, m=method: c.request(m, p, json={}), high, {plan_lock})
                assert r.status_code == 200, (path, r.text)
                assert (await c.post(undo, json={})).status_code == 200
            # course archive and delete over two lessons: RCPC, the lower lesson, waiting for the higher
            r = await _oracle(
                env, c, lambda c: c.post(f"/api/courses/{course}/archive", json={}), high, {plan_lock, low_lock}
            )
            assert r.status_code == 200, r.text
            assert (await c.post(f"/api/courses/{course}/unarchive", json={})).status_code == 200
            r = await _oracle(
                env,
                c,
                lambda c: c.request("DELETE", f"/api/courses/{course}", json={"confirm": title}),
                high,
                {plan_lock, low_lock},
            )
            assert r.status_code == 200, r.text
            assert (await c.post(f"/api/courses/{course}/restore", json={})).status_code == 200
        # the material cascade (SRV-INGEST FR-INGEST-31): the same order over the whole set
        path = tmp_path / "with-material.yaml"
        path.write_text(
            config_text()
            + f'materials:\n  - {{ id: {MATERIAL_ID}, file: ./m.pdf, sha256: "{MATERIAL}", license: personal-use,'
            " language: ru }\n",
            encoding="utf-8",
        )
        load_config(path)  # valid, so the routes that read READER_CONFIG get this material
        monkeypatch.setenv("READER_CONFIG", str(path))
        async with client(env) as c:
            r = await _oracle(
                env,
                c,
                lambda c: c.request(
                    "DELETE", f"/api/materials/{MATERIAL_ID}", json={"confirm": MATERIAL_ID, "cascade": True}
                ),
                high,
                {plan_lock, low_lock},
            )
            assert r.status_code == 200, r.text
            assert [x["course_id"] for x in r.json()["courses"]] == [str(course)]
