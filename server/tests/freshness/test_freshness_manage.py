# @trace FR-FRESH-20 FR-INGEST-31
"""[db] Freshness on a deleted material (SRV-FRESH FR-FRESH-20): the four routes answer 404 and write nothing,
a job ends before its next model call, the material delete is refused while a report runs, and a restore makes
the old reports readable again. The delete and restore go through SRV-INGEST's routes; before SRV-LESSON's
manage migration is merged, `course_columns` adds the course columns that delete reads (no-op after merge)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from .conftest import (
    FakeJobs,
    FakeProvider,
    Pg,
    World,
    client,
    deps,
    execute,
    run_job,
    scalar,
    world,
)
from .test_freshness_db import BIB, _replies, path, post_check, report_row

pytestmark = pytest.mark.anyio


async def course_columns(w: World) -> None:
    for column in ("deleted_at timestamptz", "deleted_by_material text", "archived_at timestamptz"):
        await execute(w.engine, f"ALTER TABLE courses ADD COLUMN IF NOT EXISTS {column} NULL")
    await execute(w.engine, "ALTER TABLE lessons ADD COLUMN IF NOT EXISTS deleted_at timestamptz NULL")


async def mark_deleted(w: World, deleted: bool = True) -> None:
    sql = (
        "UPDATE materials SET deleted_at = CASE WHEN :d THEN now() END WHERE owner_id = :o"
    )
    await execute(w.engine, sql, d=deleted, o=w.users["ivan"])


async def written(w: World) -> tuple[int, int, int]:
    return (
        int(await scalar(w.engine, "SELECT count(*) FROM freshness_reports")),
        int(await scalar(w.engine, "SELECT count(*) FROM material_bibliography")),
        int(await scalar(w.engine, "SELECT count(*) FROM agent_spend")),
    )


async def spend_rows(w: World, report_id: str) -> int:
    sql = "SELECT count(*) FROM agent_spend WHERE run_id = :r"
    return int(await scalar(w.engine, sql, r=uuid.UUID(report_id)))


@dataclass
class DeletingProvider(FakeProvider):
    """Deletes the material while the first search call is in flight (it still finishes and settles)."""

    deleted: bool = False

    async def run(self, call: Any) -> Any:
        result = await super().run(call)
        if call.stage == "search" and not self.deleted:
            self.deleted = True
            await mark_deleted_engine(self.engine)
        return result


async def mark_deleted_engine(engine: Any) -> None:
    await execute(engine, "UPDATE materials SET deleted_at = now()")


async def test_each_route_on_a_deleted_material_is_404_and_writes_nothing(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    async with client(w, jobs=FakeJobs()) as c:
        report_id = (await post_check(c, w)).json()["report_id"]
    await execute(w.engine, "UPDATE freshness_reports SET status = 'failed', error = 'job_failed'")
    async with client(w, jobs=FakeJobs()) as c:
        before_read = (await c.get(f"{path(w)}/freshness/{report_id}")).json()
    await mark_deleted(w)
    before = await written(w)
    jobs = FakeJobs()
    async with client(w, jobs=jobs) as c:
        responses = [
            await c.get(path(w) + "/freshness"),
            await c.get(f"{path(w)}/freshness/{report_id}"),
            await c.post(path(w) + "/freshness", json={"high_usd": "0.41"}),
            await c.put(path(w) + "/bibliography", json=BIB),
            await c.get("/api/materials/book/freshness"),  # by reader.yaml id too
        ]
    assert [(r.status_code, r.json()) for r in responses] == [(404, {"error": "not_found"})] * 5
    assert await written(w) == before
    assert jobs.enqueued == []
    await mark_deleted(w, deleted=False)
    async with client(w, jobs=FakeJobs()) as c:
        after_read = await c.get(f"{path(w)}/freshness/{report_id}")
        latest = (await c.get(path(w) + "/freshness")).json()["report"]
    assert (after_read.status_code, after_read.json()) == (200, before_read)
    assert latest["id"] == report_id


async def test_a_scan_job_whose_material_was_deleted_makes_no_model_call(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    jobs = FakeJobs()
    async with client(w, jobs=jobs) as c:
        assert (await c.put(path(w) + "/bibliography", json=BIB)).status_code == 200
        report_id = (await post_check(c, w)).json()["report_id"]
    await mark_deleted(w)
    provider = FakeProvider(w.engine, _replies())
    await run_job(w, "freshness.scan", uuid.UUID(report_id), deps(w, provider, jobs))
    assert provider.calls == []
    row = await report_row(w, report_id)
    assert (row["status"], row["error"], row["claims"]) == ("failed", "material_deleted", None)
    assert jobs.enqueued == [("freshness.scan", {"report_id": report_id})]  # no web job
    assert await spend_rows(w, report_id) == 0


async def test_a_web_job_stops_after_its_in_flight_search(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    jobs = FakeJobs()
    async with client(w, jobs=jobs) as c:
        assert (await c.put(path(w) + "/bibliography", json=BIB)).status_code == 200
        report_id = (await post_check(c, w)).json()["report_id"]
    scanner = FakeProvider(w.engine, _replies())
    await run_job(w, "freshness.scan", uuid.UUID(report_id), deps(w, scanner, jobs))
    assert jobs.enqueued[-1] == ("freshness.web", {"report_id": report_id})
    provider = DeletingProvider(w.engine, _replies())
    await run_job(w, "freshness.web", uuid.UUID(report_id), deps(w, provider, jobs))
    assert [c.stage for c in provider.calls] == ["search"]  # the in-flight call finished; none after it
    row = await report_row(w, report_id)
    assert (row["status"], row["error"]) == ("failed", "material_deleted")
    assert (row["verdict"], row["recommendations"], row["reasons"]) == (None, None, None)
    kinds = await scalar(
        w.engine,
        "SELECT string_agg(kind, ',' ORDER BY id) FROM agent_spend WHERE run_id = :r AND stage = 'search'",
        r=uuid.UUID(report_id),
    )
    assert kinds == "reserve,settle"  # the in-flight search settled as usual


async def test_the_delete_is_refused_while_a_report_runs_and_restore_shows_the_reports(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await world(pg, tmp_path)
    monkeypatch.setenv("READER_CONFIG", str(tmp_path / "reader.yaml"))
    await course_columns(w)
    async with client(w, jobs=FakeJobs()) as c:
        report_id = (await post_check(c, w)).json()["report_id"]
        busy = await c.request("DELETE", "/api/materials/book", json={"confirm": "book"})
        assert (busy.status_code, busy.json()) == (409, {"error": "material_busy"})
        await execute(w.engine, "UPDATE freshness_reports SET status = 'failed', error = 'job_failed'")
        shown = (await c.get(f"{path(w)}/freshness/{report_id}")).json()
        gone = await c.request("DELETE", "/api/materials/book", json={"confirm": "book"})
        assert gone.status_code == 200, gone.text
        hidden = await c.get(f"{path(w)}/freshness/{report_id}")
        back = await c.post("/api/materials/book/restore", json={})
        assert back.status_code == 200, back.text
        again = await c.get(f"{path(w)}/freshness/{report_id}")
    assert hidden.status_code == 404
    assert (again.status_code, again.json()) == (200, shown)
