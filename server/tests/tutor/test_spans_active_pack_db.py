"""SRV-TUTOR [db]: candidate spans are loaded from the owner's active packs only (FR-TUTOR-5's paired change,
ADR-0039 / SRV-INGEST INV-INGEST-16, V.kl-active-view-readers).

The world's material MAT holds the r2 pack of its fixture (seq 0..2) and an r3 re-conversion with other texts.

@trace FR-TUTOR-5
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ingest.packseed import R3, activate_pack, add_pack

from .conftest import MAT, SPANS, Pg, need, world

pytestmark = pytest.mark.anyio
R3_SPANS = [(61, "text", "Re-converted: the nonce varies."), (62, "text", "Re-converted: the target moves.")]


async def test_two_packs_answer_each_id_from_the_active_pack_only(pg: Pg, tmp_path: Path) -> None:
    store = need("reader.tutor.store")
    locator = need("reader.ingest.locator")
    w = await world(pg, tmp_path)
    ivan = w.users["ivan"]
    async with w.engine.begin() as conn:
        r3 = await add_pack(conn, ivan, MAT, R3, R3_SPANS)
    r3_ids = [locator.page_locators(MAT, p, [t])[0] for p, _, t in R3_SPANS]
    legacy = [f"{MAT}:{i}" for i in range(3)]

    async with w.engine.connect() as conn:
        loaded = await store.spans_by_ids(conn, ivan, legacy + r3_ids)
    assert loaded == {f"{MAT}:{i}": (page, body) for i, (page, body) in enumerate(SPANS)}  # r2 only

    async with w.engine.begin() as conn:
        await activate_pack(conn, ivan, r3)
    async with w.engine.connect() as conn:
        loaded = await store.spans_by_ids(conn, ivan, legacy + r3_ids)
    # The r3 pack's locators read its spans; a legacy id has no accepted seq map here, so it is never answered with
    # the r3 span of the same seq (FR-INGEST-42 `unmapped`).
    assert loaded == {i: (p, t) for i, (p, _, t) in zip(r3_ids, R3_SPANS, strict=True)}


async def test_another_owners_active_pack_is_never_read(pg: Pg, tmp_path: Path) -> None:
    store = need("reader.tutor.store")
    w = await world(pg, tmp_path)
    anna = w.users["anna"]
    async with w.engine.begin() as conn:
        manifest = need("reader.ingest.manifest")
        seq, _ = await manifest.head(conn, anna)
        await manifest.append(conn, anna, seq, manifest.make_entry({}), "test_remove")
    async with w.engine.connect() as conn:
        assert await store.spans_by_ids(conn, anna, [f"{MAT}:0"]) == {}
        assert await store.spans_by_ids(conn, w.users["ivan"], [f"{MAT}:0", "junk", "a:b"]) == {
            f"{MAT}:0": SPANS[0]
        }
