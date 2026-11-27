"""SRV-SEARCH [db]: forget_legacy deletes one ingest's chunk and embed rows of one owner.

@trace FR-SEARCH-21
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text

from .conftest import MAT_RU, Pg, World, need, scalar, world

pytestmark = pytest.mark.anyio


@pytest.fixture
async def w(pg: Pg, tmp_path: Path) -> AsyncIterator[World]:
    built = await world(pg, tmp_path)
    try:
        yield built
    finally:
        await built.engine.dispose()


async def rows(w: World, owner: uuid.UUID, ingest: uuid.UUID, chunks: int) -> None:
    async with w.engine.begin() as conn:
        for chunk_no in range(chunks):
            await conn.execute(
                text(
                    "INSERT INTO search_chunks (owner_id, ingest_id, material_sha256, model, params_sha256,"
                    " fts_config, page, chunk_no, char_start, char_stop, span_seqs, primary_seq, text,"
                    " embedding)"
                    " VALUES (:o, :i, :s, 'm', :p, 'russian', 1, :n, 0, 1, '{0}', 0, 'x',"
                    " array_fill(0.0::real, ARRAY[1024])::vector)"
                ),
                {"o": owner, "i": ingest, "s": MAT_RU, "p": "e" * 64, "n": chunk_no},
            )
        await conn.execute(
            text(
                "INSERT INTO search_embeds (owner_id, ingest_id, model, params_sha256, chunk_count)"
                " VALUES (:o, :i, 'm', :p, :n)"
            ),
            {"o": owner, "i": ingest, "p": "e" * 64, "n": chunks},
        )


async def counts(w: World, owner: uuid.UUID, ingest: uuid.UUID) -> tuple[int, int]:
    return (
        await scalar(w.engine, "SELECT count(*) FROM search_chunks WHERE owner_id = :o AND ingest_id = :i",
                     o=owner, i=ingest),
        await scalar(w.engine, "SELECT count(*) FROM search_embeds WHERE owner_id = :o AND ingest_id = :i",
                     o=owner, i=ingest),
    )


async def test_forget_legacy_deletes_exactly_that_ingests_rows_of_the_owner(w: World) -> None:
    store = need("reader.search.store")
    ivan, anna = w.users["ivan"], w.users["anna"]
    gone, kept, foreign = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await rows(w, ivan, gone, 3)
    await rows(w, ivan, kept, 2)
    await rows(w, anna, foreign, 1)
    async with w.engine.begin() as conn:
        assert await store.forget_legacy(conn, ivan, gone) == 4  # three chunks and one embed row
        assert await store.forget_legacy(conn, anna, kept) == 0  # another owner's id: nothing
    assert await counts(w, ivan, gone) == (0, 0)
    assert await counts(w, ivan, kept) == (2, 1)
    assert await counts(w, anna, foreign) == (1, 1)
    async with w.engine.begin() as conn:
        assert await store.forget_legacy(conn, ivan, gone) == 0
