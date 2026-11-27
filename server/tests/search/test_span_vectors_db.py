"""SRV-SEARCH [db]: search_0002's span-vector rows, their immutability trigger and forget_overlay.

@trace FR-SEARCH-4
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from .conftest import MAT_EN, Pg, World, add_material, need, scalar, world

pytestmark = pytest.mark.anyio
INPUTS = {"model": "m", "params_sha256": "e" * 64, "prefix_overlay_id": None}
BUILDER = "search.vectors/1"


@pytest.fixture
async def w(pg: Pg, tmp_path: Path) -> AsyncIterator[World]:
    built = await world(pg, tmp_path)
    try:
        yield built
    finally:
        await built.engine.dispose()


async def overlay(w: World, handle: str) -> tuple[uuid.UUID, str, str]:
    """The owner's building vectors overlay over the material's active pack."""
    from reader.ingest import overlays, views

    owner = w.users[handle]
    async with w.engine.begin() as conn:
        pack = await views.active_pack(conn, owner, MAT_EN)
        assert pack is not None
        oid = overlays.overlay_id("vectors", pack.pack_id, INPUTS, BUILDER)
        await overlays.begin(conn, owner, oid, kind="vectors", pack_id=pack.pack_id, inputs=INPUTS,
                             builder_version=BUILDER)
    return owner, oid, pack.pack_id


def row(owner: uuid.UUID, oid: str, pid: str, window_no: int = 0,
        fts_config: str = "english") -> dict[str, Any]:
    return {"o": owner, "v": oid, "p": pid, "l": "spn:" + MAT_EN + ":p1:0-5", "n": window_no, "f": fts_config}


INSERT = text(
    "INSERT INTO search_span_vectors (owner_id, overlay_id, pack_id, locator, window_no, char_start,"
    " char_stop, model, fts_config, fts, embedding) VALUES (:o, :v, :p, :l, :n, 0, 5, 'm', :f,"
    " to_tsvector('english', 'hello'), array_fill(0.0::real, ARRAY[1024])::vector)"
)


async def test_key_check_two_owners_and_no_text_column(w: World) -> None:
    await add_material(w, "ivan", MAT_EN, [(1, "hello world")], config_id="book-en")
    await add_material(w, "anna", MAT_EN, [(1, "hello world")], config_id="book-en")
    ivan, oid, pid = await overlay(w, "ivan")
    anna, oid_b, pid_b = await overlay(w, "anna")
    assert (oid_b, pid_b) == (oid, pid)  # same bytes: one pack id and one overlay id
    async with w.engine.begin() as conn:
        await conn.execute(INSERT, row(ivan, oid, pid))
        await conn.execute(INSERT, row(anna, oid, pid))  # the same key under another owner
    with pytest.raises(IntegrityError):
        async with w.engine.begin() as conn:
            await conn.execute(INSERT, row(ivan, oid, pid))
    with pytest.raises(IntegrityError):
        async with w.engine.begin() as conn:
            await conn.execute(INSERT, row(ivan, oid, pid, 1, "german"))
    columns = await scalar(w.engine, "SELECT array_agg(column_name::text ORDER BY column_name) FROM"
                           " information_schema.columns WHERE table_name = 'search_span_vectors'")
    assert "text" not in columns and {"owner_id", "overlay_id", "locator", "window_no", "fts"} <= set(columns)
    heads = await scalar(w.engine, "SELECT array_agg(version_num::text) FROM alembic_version")
    assert "search_0002" in heads


async def test_a_sealed_overlay_is_immutable_and_forget_overlay_deletes(w: World) -> None:
    store = need("reader.search.store")
    from reader.ingest import overlays

    await add_material(w, "ivan", MAT_EN, [(1, "hello world")], config_id="book-en")
    await add_material(w, "anna", MAT_EN, [(1, "hello world")], config_id="book-en")
    ivan, oid, pid = await overlay(w, "ivan")
    anna, _, _ = await overlay(w, "anna")
    async with w.engine.begin() as conn:
        await conn.execute(INSERT, row(ivan, oid, pid))
        await conn.execute(INSERT, row(anna, oid, pid))
        await overlays.seal(conn, ivan, oid)
    refused = (
        (INSERT, row(ivan, oid, pid, 1)),
        (text("UPDATE search_span_vectors SET window_no = 7 WHERE owner_id = :o"), {"o": ivan}),
        (text("DELETE FROM search_span_vectors WHERE owner_id = :o"), {"o": ivan}),
        (text("TRUNCATE search_span_vectors"), {}),
    )
    for statement, params in refused:
        with pytest.raises(DBAPIError):
            async with w.engine.begin() as conn:
                await conn.execute(statement, params)
    update_under_setting = text(  # the setting does not open UPDATE
        "WITH s AS (SELECT set_config('reader.search_delete', 'on', true))"
        " UPDATE search_span_vectors SET window_no = 7 FROM s WHERE owner_id = :o")
    with pytest.raises(DBAPIError):
        async with w.engine.begin() as conn:
            await conn.execute(update_under_setting, {"o": ivan})
    async with w.engine.begin() as conn:
        assert await store.forget_overlay(conn, ivan, oid) == 1
        assert await scalar_in(conn, "SELECT current_setting('reader.search_delete', true)") == "off"
    async with w.engine.begin() as conn:
        assert await store.forget_overlay(conn, ivan, oid) == 0
    count = "SELECT count(*) FROM search_span_vectors WHERE owner_id = :o"
    assert await scalar(w.engine, count, o=ivan) == 0
    assert await scalar(w.engine, count, o=anna) == 1  # the other owner's rows stay


async def scalar_in(conn: Any, sql: str) -> Any:
    return (await conn.execute(text(sql))).scalar()


async def test_gc_seam_is_bound_to_forget_overlay(w: World) -> None:
    gc = need("reader.ingest.gc")
    store = need("reader.search.store")
    await add_material(w, "ivan", MAT_EN, [(1, "hello world")], config_id="book-en")
    ivan, oid, pid = await overlay(w, "ivan")
    async with w.engine.begin() as conn:
        await conn.execute(INSERT, row(ivan, oid, pid))
        assert await gc.SEAMS[0].search_forget_overlay(conn, ivan, oid) == 1
    assert gc.Seams().search_forget_overlay.__module__ == gc.__name__
    assert callable(store.forget_overlay)


# ---- phase 2 unit 5: code review of 4b (non-blocking) ----


async def test_an_owner_purge_with_production_seams_deletes_the_built_rows(
    w: World, embed_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reader.ingest import views

    from .conftest import embed_material

    monkeypatch.setenv("READER_EMBED_URL", embed_server.url)
    gc = need("reader.ingest.gc")
    spans = [(1, "Wallet keys sign every transfer."), (2, "A miner earns the block reward.")]
    for handle in ("ivan", "anna"):  # two owners of the same bytes: one pack id, two row sets
        await embed_material(w, await add_material(w, handle, MAT_EN, spans, config_id="book-en"), MAT_EN)
    count = "SELECT count(*) FROM search_span_vectors WHERE owner_id = :o"
    ivan, anna = w.users["ivan"], w.users["anna"]
    assert await scalar(w.engine, count, o=ivan) == 2 and await scalar(w.engine, count, o=anna) == 2
    async with w.engine.begin() as conn:
        pack = await views.active_pack(conn, ivan, MAT_EN)
        assert pack is not None
        assert gc.SEAMS[0] == gc.Seams()  # the production seams, none replaced
        assert await gc.purge(conn, ivan, pack.pack_id, gc.SEAMS[0]) is True
    assert await scalar(w.engine, count, o=ivan) == 0
    assert await scalar(w.engine, count, o=anna) == 2


async def test_the_downgrade_drops_only_the_span_vectors(w: World) -> None:
    from alembic import command

    from reader.db import alembic_config

    def move(target: str, up: bool) -> Any:
        def run(connection: Any) -> None:
            config = alembic_config()
            config.attributes["connection"] = connection
            (command.upgrade if up else command.downgrade)(config, target)

        return run

    async with w.engine.connect() as conn:
        await conn.run_sync(move("search_0001", up=False))
        await conn.commit()
    regclass = "SELECT to_regclass(:t)::text"
    assert await scalar(w.engine, regclass, t="search_span_vectors") is None
    assert await scalar(w.engine, regclass, t="search_chunks") == "search_chunks"
    guard = "SELECT count(*) FROM pg_proc WHERE proname = 'search_span_vectors_guard'"
    assert await scalar(w.engine, guard) == 0
    async with w.engine.connect() as conn:
        await conn.run_sync(move("heads", up=True))
        await conn.commit()
    assert await scalar(w.engine, regclass, t="search_span_vectors") == "search_span_vectors"
