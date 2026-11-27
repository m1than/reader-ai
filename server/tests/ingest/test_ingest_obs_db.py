"""SRV-OBS [db]: the `ingest.import` handler's `ingest.parse` child span (FR-OBS-13) on the real job
machinery: the page count and the artifact's bytes, OK on success and ERROR (no description, no counts) when
the stored artifact is gone, with no material text in any attribute (INV-OBS-2).

@trace FR-OBS-13 INV-OBS-2 INV-OBS-3
"""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from obs.conftest import finished, obs_setup

from .conftest import CANARY, Pg, build_artifact, need, tar_of, world

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("r3_converter")]  # r3 artifacts, deliberately


def _files() -> dict[str, bytes]:
    spans = [json.loads(x) for x in build_artifact()["spans.jsonl"].splitlines()]
    spans[1]["text"] = f"Абзац {CANARY}"
    return build_artifact(spans=spans)


async def _receive(w: Any, files: dict[str, bytes]) -> dict[str, Any]:
    mod = need("reader.ingest.receive")
    result: dict[str, Any] = await mod.receive(
        io.BytesIO(tar_of(files)), config=w.config, engine=w.engine, jobs=w.jobs, blobs_root=w.blobs
    )
    return result


def _parse(spans: list[Any]) -> tuple[Any, Any]:
    by_id = {s.context.span_id: s for s in spans}
    (parse,) = [s for s in spans if s.name == "ingest.parse"]
    return parse, by_id[parse.parent.span_id]


async def test_parse_span_counts_pages_and_bytes(pg: Pg, tmp_path: Path) -> None:
    files = _files()
    with obs_setup("worker") as (handle, memory):
        async with world(pg, tmp_path) as w:
            await _receive(w, files)
            await w.work()
            spans = finished(handle, memory)
    parse, root = _parse(spans)
    assert root.name == "job ingest.import"
    assert parse.attributes["reader.pages"] == json.loads(files["manifest.json"])["page_count"] == 2
    assert parse.attributes["reader.bytes"] == sum(len(v) for v in files.values())
    assert parse.status.status_code.name == "OK"
    assert CANARY not in json.dumps([dict(s.attributes) for s in spans], ensure_ascii=False)


async def test_parse_span_is_error_when_the_artifact_is_gone(pg: Pg, tmp_path: Path) -> None:
    with obs_setup("worker") as (handle, memory):
        async with world(pg, tmp_path) as w:
            result = await _receive(w, _files())
            shutil.rmtree(w.blobs / "ingest" / result["artifact_id"])
            await w.work()
            spans = finished(handle, memory)
    parse, _ = _parse(spans)  # ArtifactMissing is permanent: one attempt
    assert parse.status.status_code.name == "ERROR"
    assert parse.status.description is None
    assert "reader.pages" not in parse.attributes and "reader.bytes" not in parse.attributes
