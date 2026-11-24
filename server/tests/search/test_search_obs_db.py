"""SRV-OBS [db]: the `search.embed` handler's child spans (FR-OBS-13): one `ingest.chunk` with the chunk count
and one `embed.batch` per passage batch with its size and the vectors returned, under the job's span, with no
span text in any attribute (INV-OBS-2). The embed's late end (FR-OBS-20 d, FR-INGEST-26): a succeeded embed
enqueues the import's `obs.runs` with end 2, and the import's snapshot then counts the chunks and ends at the
embed.

@trace FR-OBS-13 FR-OBS-20 FR-INGEST-26 INV-OBS-2
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from obs.conftest import finished, obs_setup, require

from .conftest import MAT_RU, EmbedServer, Pg, World, add_material, embed_material, world

pytestmark = pytest.mark.anyio
BOOK = "bookcanary-7f3a9c"
FILL = " Здесь больше ничего не сказано ни о чём другом, это просто заполнитель для абзаца."


@pytest.fixture
async def w(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, embed_server: EmbedServer
) -> AsyncIterator[World]:
    monkeypatch.setenv("READER_EMBED_URL", embed_server.url)
    built = await world(pg, tmp_path, monkeypatch)
    try:
        yield built
    finally:
        await built.engine.dispose()


async def test_chunk_and_batch_spans(w: World, embed_server: EmbedServer) -> None:
    pages = [(p, f"страница {p} {BOOK}." + FILL) for p in range(1, 13)]  # 12 pages, one chunk each
    ingest = await add_material(w, "ivan", MAT_RU, pages)
    with obs_setup("worker") as (handle, memory):
        with require("reader.obs").tracer().start_as_current_span("job search.embed") as root:
            await embed_material(w, ingest, MAT_RU)
        spans: list[Any] = finished(handle, memory)
    root_id = root.get_span_context().span_id
    (chunk,) = [s for s in spans if s.name == "ingest.chunk"]
    assert chunk.attributes["reader.chunks"] == 12
    batches = [s for s in spans if s.name == "embed.batch"]
    assert [(b.attributes["reader.embed.batch"], b.attributes["reader.embed.vectors"]) for b in batches] == [
        (8, 8), (4, 4)
    ]
    for span in (chunk, *batches):
        assert span.parent is not None and span.parent.span_id == root_id
        assert span.status.status_code.name == "OK"
    assert BOOK not in json.dumps([dict(s.attributes) for s in spans])


async def test_a_succeeded_embed_is_the_imports_late_end(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    from sqlalchemy import text

    import reader.jobs

    enqueued: list[Any] = []

    class Queue:
        async def enqueue(self, kind: str, args: Any, schedule_in: int = 0) -> None:
            enqueued.append((kind, dict(args), schedule_in))

    monkeypatch.setattr(reader.jobs, "running", lambda: Queue())
    search_jobs = require("reader.search.jobs")
    runs = require("reader.obs.runs")
    pages = [(p, f"страница {p}." + FILL) for p in range(1, 13)]
    ingest = await add_material(w, "ivan", MAT_RU, pages)
    with obs_setup("worker"):
        payload = {"ingest_id": str(ingest), "material_sha256": MAT_RU}
        await search_jobs.after_end(w.engine, payload, "dead")  # only a succeeded embed is an end
        assert enqueued == []
        await embed_material(w, ingest, MAT_RU)
        await search_jobs.after_end(w.engine, payload, "succeeded")
    assert enqueued == [("obs.runs", {"kind": "ingest", "id": str(ingest), "end": 2}, 60)]
    assert search_jobs.JOB_KINDS[0].after_end is search_jobs.after_end
    async with w.engine.connect() as conn:
        args = runs.RunsArgs(kind="ingest", id=str(ingest), end=2)
        snap = await runs.snapshot(conn, args, datetime.now(UTC))
        embedded = (await conn.execute(text("SELECT embedded_at FROM search_embeds WHERE ingest_id = :i"),
                                       {"i": ingest})).scalar_one()
    assert snap.metrics["chunks"] == 12 and snap.metrics["pages"] == 12 and snap.end == 2
    assert snap.end_ms == int(embedded.timestamp() * 1000)  # the late end's time, not the import's
