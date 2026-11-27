"""SRV-SEARCH [db]: candidates read the active pack's vector overlay only, and the embed activates a pack
awaiting activation (FR-SEARCH-5, ADR-0039 revision, phase 2 unit 4b).

One material holds two packs (its fixture's r2 import over backfilled locators and an r3 re-conversion): only
the buildable pack is embedded, search ranks the head entry's pack only and every span id is a locator.

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


async def test_only_the_head_entrys_pack_is_searched(w: World) -> None:
    store = need("reader.search.store")
    locator = need("reader.ingest.locator")
    ivan = w.users["ivan"]
    r2_ingest = await add_material(w, "ivan", MAT_EN, R2_SPANS, config_id="book-en")
    await embed_material(w, r2_ingest, MAT_EN)
    async with w.engine.begin() as conn:
        r3 = await add_pack(conn, ivan, MAT_EN, R3, [(1, "text", t) for t in R3_TEXTS])

    found = (await store.search(w.engine, ivan, "blockchain chain of blocks", k=10)).results
    r2_loc = locator.page_locators(MAT_EN, 1, [R2_SPANS[0][1]])[0]
    assert [(h.span_id, h.span_ids) for h in found] == [(r2_loc, [r2_loc])]  # the legacy pack, by locator

    async with w.engine.begin() as conn:  # the r3 pack enters the head entry without vectors: dark
        await activate_pack(conn, ivan, r3)
    assert (await store.search(w.engine, ivan, "blockchain chain of blocks", k=10)).results == []
    async with w.engine.connect() as conn:
        (r3_pack,) = [p for p in await need("reader.ingest.views").live_packs(conn, ivan) if p.pack_id == r3]
    await embed_material(w, r3_pack.ingest_id, MAT_EN)
    # the r3 overlay is sealed, but its activation waits for SRV-INGEST's seq map of the legacy pack
    from .conftest import scalar

    assert await scalar(w.engine, "SELECT reason FROM pending_activations WHERE pack_id = :p", p=r3) == (
        "seqmap_missing")
    assert await scalar(w.engine, "SELECT count(*) FROM overlays WHERE pack_id = :p AND kind = 'vectors'"
                        " AND state = 'sealed'", p=r3) == 1
    assert (await store.search(w.engine, ivan, "blockchain chain of blocks", k=10)).results == []


async def test_the_embed_activates_a_pack_awaiting_activation(w: World) -> None:
    manifest = need("reader.ingest.manifest")
    views = need("reader.ingest.views")
    locator = need("reader.ingest.locator")
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
    r2_loc = locator.page_locators(MAT_EN, 1, [R2_SPANS[0][1]])[0]
    assert [h.span_id for h in (await store.search(w.engine, ivan, "blockchain", k=10)).results] == [r2_loc]
