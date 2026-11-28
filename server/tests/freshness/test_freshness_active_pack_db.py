# @trace FR-FRESH-2 FR-FRESH-3 FR-FRESH-10
"""SRV-FRESH [db]: the sample is drawn from the material's active pack only (FR-FRESH-2's paired change,
ADR-0039 / SRV-INGEST INV-INGEST-16, V.kl-active-view-readers).

The world's material MAT holds its fixture's r2 pack and an r3 re-conversion with other texts: the current
ingest, spans, outline and stored span ids come from one pack, never both."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ingest.packseed import R3, activate_pack, add_pack

from .conftest import MAT, OUTLINE, SPANS, FakeProvider, Pg, client, need, world
from .test_freshness_db import _replies, full_check, path, report_row

pytestmark = pytest.mark.anyio
R3_SPANS = [
    (1, "heading", "Re-converted ledgers"),
    (8, "text", "Re-converted: the protocol version 2.0 was released in 2024."),
]


async def test_two_packs_sample_the_active_pack_only(pg: Pg, tmp_path: Path) -> None:
    store = need("reader.freshness.store")
    check = need("reader.freshness.check")
    locator = need("reader.ingest.locator")
    w = await world(pg, tmp_path)
    ivan = w.users["ivan"]
    async with w.engine.begin() as conn:
        r3 = await add_pack(conn, ivan, MAT, R3, R3_SPANS)

    async with w.engine.connect() as conn:
        material = await store.material(conn, ivan, w.materials["ivan"])
        assert material.ingest_id == w.ingests["ivan"]  # the r2 pack's ingest
        spans = await store.spans(conn, ivan, material.ingest_id)
        assert [s.text for s in spans] == [t for _, _, t in SPANS]  # one pack, not both
        assert len(await store.outline(conn, ivan, material.ingest_id)) == len(OUTLINE)
        prepared = await check.prepare(conn, w.config, ivan, material)
    stored = prepared.sample.stored(MAT)
    assert stored
    assert [s["span"] for s in stored] == [f"{MAT}:{p.seq}" for p in prepared.sample.spans]
    r2_hash = prepared.scan_inputs_sha256

    async with w.engine.begin() as conn:
        await activate_pack(conn, ivan, r3)
    async with w.engine.connect() as conn:
        material = await store.material(conn, ivan, w.materials["ivan"])
        assert material.ingest_id not in (None, w.ingests["ivan"])
        spans = await store.spans(conn, ivan, material.ingest_id)
        assert [s.text for s in spans] == [t for _, _, t in R3_SPANS]
        assert await store.outline(conn, ivan, material.ingest_id) == []
        prepared = await check.prepare(conn, w.config, ivan, material)
        # the stale report's ingest (the r2 pack's) still reads its own rows
        assert len(await store.spans(conn, ivan, w.ingests["ivan"])) == len(SPANS)
    expected = {p: locator.page_locators(MAT, p, [t])[0] for p, _, t in R3_SPANS}
    hashes = {p: locator.quote_sha256(t) for p, _, t in R3_SPANS}
    picked = prepared.sample.spans
    assert prepared.sample.stored(MAT) == [  # r3_gates_revision: a locator entry carries its stored hash
        {"span": expected[p.page], "page": p.page, "quote_sha256": hashes[p.page]} for p in picked
    ]
    assert prepared.scan_inputs_sha256 != r2_hash


async def _claims_over(w: Any, tmp: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """A fake-provider check: the stored claims and the GET read shape's claims."""
    provider = FakeProvider(w.engine, _replies())
    report_id, _ = await full_check(w, tmp, provider)
    stored = (await report_row(w, report_id))["claims"]
    async with client(w) as c:
        shown = (await c.get(path(w) + "/freshness")).json()["report"]["claims"]
    return stored, shown


async def test_r3_claims_store_the_quote_hash_and_the_read_shape_is_unchanged(pg: Pg, tmp_path: Path) -> None:
    """FR-FRESH-3 and FR-FRESH-10 (r3_gates_revision): a scan over the r3 pack stores claims whose span is a
    locator with its span's quote_sha256; the read shape keeps exactly the approved keys."""
    locator = need("reader.ingest.locator")
    w = await world(pg, tmp_path)
    ivan = w.users["ivan"]
    async with w.engine.begin() as conn:  # the same synthetic spans re-converted: r3 ids, the same texts
        r3 = await add_pack(conn, ivan, MAT, R3, list(SPANS))
        await activate_pack(conn, ivan, r3)
    stored, shown = await _claims_over(w, tmp_path)
    page, _, body = SPANS[2]  # CLAIM cites s3
    loc = locator.page_locators(MAT, page, [t for p, _, t in SPANS if p == page])[0]
    assert stored[0]["span"] == loc
    assert stored[0]["quote_sha256"] == locator.quote_sha256(body)
    assert shown[0]["span"] == loc
    assert set(shown[0]) == {"id", "subject", "kind", "confidence", "page", "span", "cell"}


async def test_legacy_claims_store_no_quote_hash(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    stored, shown = await _claims_over(w, tmp_path)
    assert stored[0]["span"] == f"{MAT}:2"
    assert "quote_sha256" not in stored[0]
    assert set(shown[0]) == {"id", "subject", "kind", "confidence", "page", "span", "cell"}


async def test_no_active_pack_is_not_imported(pg: Pg, tmp_path: Path) -> None:
    store = need("reader.freshness.store")
    manifest = need("reader.ingest.manifest")
    w = await world(pg, tmp_path)
    ivan = w.users["ivan"]
    async with w.engine.begin() as conn:
        seq, _ = await manifest.head(conn, ivan)
        await manifest.append(conn, ivan, seq, manifest.make_entry({}), "test_remove")
    async with w.engine.connect() as conn:
        assert (await store.material(conn, ivan, w.materials["ivan"])).ingest_id is None
