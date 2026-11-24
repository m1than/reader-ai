"""SRV-INGEST [db], paired SRV-OBS phase 2 change: a succeeded import enqueues `obs.runs` {ingest, ingest id,
1} with `schedule_in` 60 s; a failing enqueue leaves the import succeeded (INV-OBS-3).

@trace FR-INGEST-26 INV-OBS-3
"""

from __future__ import annotations

import io
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
                  enabled: bool = True) -> tuple[Any, list[Any], Any]:
    import reader.jobs

    captured = Captured(fail)
    monkeypatch.setattr(reader.jobs, "running", lambda: captured)
    with obs_setup("worker", enabled=enabled):
        async with world(pg, tmp) as w:
            await need("reader.ingest.receive").receive(
                io.BytesIO(tar_of(build_artifact())), config=w.config, engine=w.engine, jobs=w.jobs,
                blobs_root=w.blobs)
            await w.work()
            ingest_id = await w.scalar("SELECT id FROM ingests")
            outcome = await w.scalar("SELECT outcome FROM job_keys WHERE kind = 'ingest.import'")
    return ingest_id, [e for e in captured.enqueued if e[0] == "obs.runs"], outcome


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
