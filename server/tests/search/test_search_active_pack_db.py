"""SRV-SEARCH [db]: the legacy chunk path reads through SRV-INGEST's active view (FR-SEARCH-5's paired change,
ADR-0039 / SRV-INGEST INV-INGEST-16, V.kl-active-view-readers).

One material holds two embedded packs (its fixture's r2 import and an r3 re-conversion): search ranks the
active pack's chunks only, never both, with the span ids the view mints; an embedded pack awaiting activation
becomes active through the embed's `try_activate`.

@trace FR-SEARCH-5
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from ingest.packseed import R3, activate_pack, add_pack

from .conftest import MAT_EN, EmbedServer, Pg, World, add_material, embed_material, need, world

pytestmark = pytest.mark.anyio
FILL = " Nothing further is said here about anything else in this paragraph."
R2_SPANS = [(1, "A blockchain keeps a chain of blocks, each block linked to the previous one." + FILL)]
R3_TEXTS = ["Re-converted: a blockchain keeps a chain of blocks linked by hashes." + FILL]


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


async def test_two_embedded_packs_search_the_active_pack_only(w: World) -> None:
    store = need("reader.search.store")
    locator = need("reader.ingest.locator")
    ivan = w.users["ivan"]
    r2_ingest = await add_material(w, "ivan", MAT_EN, R2_SPANS, config_id="book-en")
    await embed_material(w, r2_ingest, MAT_EN)
    async with w.engine.begin() as conn:
        r3 = await add_pack(conn, ivan, MAT_EN, R3, [(1, "text", t) for t in R3_TEXTS])
    async with w.engine.connect() as conn:
        (r3_pack,) = [p for p in await need("reader.ingest.views").live_packs(conn, ivan) if p.pack_id == r3]
    await embed_material(w, r3_pack.ingest_id, MAT_EN)  # no pending activation: the r3 pack stays inactive

    found = (await store.search(w.engine, ivan, "blockchain chain of blocks", k=10)).results
    assert [h.span_id for h in found] == [f"{MAT_EN}:0"]  # the r2 pack's chunk only, its legacy id

    async with w.engine.begin() as conn:
        await activate_pack(conn, ivan, r3)
    found = (await store.search(w.engine, ivan, "blockchain chain of blocks", k=10)).results
    loc = locator.page_locators(MAT_EN, 1, R3_TEXTS)[0]
    assert [(h.span_id, h.span_ids) for h in found] == [(loc, [loc])]  # the r3 chunk, its locator


async def test_the_embed_activates_a_pack_awaiting_activation(w: World) -> None:
    manifest = need("reader.ingest.manifest")
    views = need("reader.ingest.views")
    ivan = w.users["ivan"]
    r2_ingest = await add_material(w, "ivan", MAT_EN, R2_SPANS, config_id="book-en")
    async with w.engine.begin() as conn:  # as a fresh import: out of the head, a pending activation
        seq, _ = await manifest.head(conn, ivan)
        await manifest.append(conn, ivan, seq, manifest.make_entry({}), "test_remove")
        pack = await views.pack_of_ingest(conn, r2_ingest)
        await manifest.request_activation(conn, ivan, pack.pack_id, {})
    store = need("reader.search.store")
    assert (await store.search(w.engine, ivan, "blockchain", k=10)).results == []
    await embed_material(w, r2_ingest, MAT_EN)
    async with w.engine.connect() as conn:
        assert (await views.active_pack(conn, ivan, MAT_EN)) == pack
    assert [h.span_id for h in (await store.search(w.engine, ivan, "blockchain", k=10)).results] == [
        f"{MAT_EN}:0"
    ]
