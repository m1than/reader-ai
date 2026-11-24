"""SRV-OBS [db]: the `search.embed` handler's child spans (FR-OBS-13): one `ingest.chunk` with the chunk count
and one `embed.batch` per passage batch with its size and the vectors returned, under the job's span, with no
span text in any attribute (INV-OBS-2).

@trace FR-OBS-13 INV-OBS-2
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
