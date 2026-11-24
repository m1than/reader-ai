"""SRV-INGEST [db], paired SRV-OBS phase 2 change: a succeeded import enqueues `obs.runs` {ingest, ingest id,
1} with `schedule_in` 60 s; a failing enqueue leaves the import succeeded (INV-OBS-3). The lookup is the
instance owner's ingest only, and the import's Run snapshot carries the complete metrics (FR-OBS-20 f).

@trace FR-INGEST-26 FR-OBS-20 INV-OBS-3
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from obs.conftest import obs_setup

from .conftest import Pg, build_artifact, need, tar_of, world

pytestmark = pytest.mark.anyio


class Captured:
    def __init__(self, fail: bool = False) -> None:
        self.fail, self.enqueued = fail, []

    async def enqueue(self, kind: str, args: Any, schedule_in: int = 0) -> Any:
        if self.fail and kind == "obs.runs":
            raise RuntimeError("queue down")
        self.enqueued.append((kind, dict(args), schedule_in))
        return None


async def _import(pg: Pg, tmp: Path, monkeypatch: pytest.MonkeyPatch, *, fail: bool = False,
                  enabled: bool = True, before: Any = None, inside: Any = None) -> tuple[Any, list[Any], Any]:
    """One import to its end; `before(w)` runs before it, `inside(w, ingest_id)` after it, in the world."""
    import reader.jobs

    captured = Captured(fail)
    monkeypatch.setattr(reader.jobs, "running", lambda: captured)
    with obs_setup("worker", enabled=enabled):
        async with world(pg, tmp) as w:
            if before is not None:
                await before(w)
            await need("reader.ingest.receive").receive(
                io.BytesIO(tar_of(build_artifact())), config=w.config, engine=w.engine, jobs=w.jobs,
                blobs_root=w.blobs)
            await w.work()
            ingest_id = await w.scalar("SELECT i.id FROM ingests i JOIN users u ON u.id = i.owner_id"
                                       " WHERE u.handle = 'ivan'")
            outcome = await w.scalar("SELECT outcome FROM job_keys WHERE kind = 'ingest.import'")
            if inside is not None:
                await inside(w, ingest_id)
    return ingest_id, [e for e in captured.enqueued if e[0] == "obs.runs"], outcome


async def test_the_lookup_is_the_owners_and_the_snapshot_is_complete(pg: Pg, tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """Another user's row of the same material, config and converter is never the import's ingest (owner
    filter); the snapshot has pages, spans, chunks, import_s and pages_per_min_import (FR-OBS-20 f)."""
    from datetime import UTC, datetime

    from sqlalchemy import text

    from reader.obs import runs

    got: dict[str, Any] = {}

    async def before(w: Any) -> None:
        # the same material sha, config id and converter version as the import below, under another owner
        manifest = json.loads(build_artifact()["manifest.json"])
        async with w.engine.begin() as conn:
            material = (await conn.execute(text(
                "INSERT INTO materials (owner_id, sha256, config_id, page_count)"
                " VALUES (:o, :s, 'synthetic', 3) RETURNING id"),
                {"o": w.users["anna"], "s": manifest["material_sha256"]})).scalar_one()
            got["other"] = (await conn.execute(text(
                "INSERT INTO ingests (owner_id, material_id, converter_version, artifact_id, span_count,"
                " outline_count, outline_source, pages_without_text)"
                " VALUES (:o, :m, :v, :a, 1, 0, 'headings', '{}') RETURNING id"),
                {"o": w.users["anna"], "m": material, "v": manifest["converter_version"], "a": "0" * 64},
            )).scalar_one()

    async def inside(w: Any, ingest_id: Any) -> None:
        async with w.engine.connect() as conn:
            got["snap"] = await runs.snapshot(conn, runs.RunsArgs(kind="ingest", id=str(ingest_id), end=1),
                                              datetime.now(UTC))

    ingest_id, enqueued, outcome = await _import(pg, tmp_path, monkeypatch, before=before, inside=inside)
    assert outcome == "succeeded" and ingest_id != got["other"]
    assert enqueued == [("obs.runs", {"kind": "ingest", "id": str(ingest_id), "end": 1}, 60)]
    snap = got["snap"]
    assert set(snap.metrics) == {"pages", "spans", "chunks", "import_s", "pages_per_min_import"}
    assert snap.metrics["chunks"] == 0 and snap.metrics["spans"] > 0 and snap.metrics["import_s"] > 0
    assert snap.metrics["pages_per_min_import"] == pytest.approx(
        snap.metrics["pages"] / snap.metrics["import_s"] * 60)
    assert (snap.status, snap.outcome) == ("FINISHED", "succeeded")


async def test_a_succeeded_import_enqueues_obs_runs_once(pg: Pg, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    ingest_id, enqueued, outcome = await _import(pg, tmp_path, monkeypatch)
    assert outcome == "succeeded"
    assert enqueued == [("obs.runs", {"kind": "ingest", "id": str(ingest_id), "end": 1}, 60)]


async def test_tracing_off_enqueues_nothing(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, enqueued, _ = await _import(pg, tmp_path, monkeypatch, enabled=False)
    assert enqueued == []


async def test_a_failing_enqueue_leaves_the_import_succeeded(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    with caplog.at_level(logging.WARNING):
        _, enqueued, outcome = await _import(pg, tmp_path, monkeypatch, fail=True)
    assert enqueued == [] and outcome == "succeeded"
    assert any("obs runs_enqueue_failed kind=ingest" in r.getMessage() for r in caplog.records)


async def test_a_dead_import_enqueues_nothing(pg: Pg, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-INGEST-26 (code review round 2): the hook of a dead import, even for a material whose ingest exists,
    enqueues nothing (a dead import's own rows rolled back; it has no Run)."""
    ingest_jobs = need("reader.ingest.jobs")

    async def inside(w: Any, ingest_id: Any) -> None:
        payload = json.loads(await w.scalar("SELECT args::text FROM job_keys WHERE kind = 'ingest.import'"))
        await ingest_jobs.after_end(w.engine, payload, "dead")

    _, enqueued, _ = await _import(pg, tmp_path, monkeypatch, inside=inside)
    assert [e[1]["end"] for e in enqueued] == [1]  # the import's own end only, none from the dead call
