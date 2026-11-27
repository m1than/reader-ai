"""`views.active_span_rows`: span id → (page, text, quote_sha256) from the owner's active packs only
(SRV-INGEST INV-INGEST-16, paired with SRV-TUTOR FR-TUTOR-5/-8, V.kl-tutor-find) [db].

@trace INV-INGEST-16
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from .conftest import Pg, World, need, world
from .test_overlays_db import activate
from .test_seqmap_db import R2, R3, ingest_of, sha_of, synthetic_pack

pytestmark = pytest.mark.anyio


def q(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


async def rows(w: World, ids: list[str], owner: str = "ivan") -> tuple[dict[str, Any], dict[str, Any]]:
    views = need("reader.ingest.views")
    async with w.engine.connect() as conn:
        got = await views.active_span_rows(conn, w.users[owner], ids)
        texts = await views.active_span_texts(conn, w.users[owner], ids)
    return got, texts


async def test_span_rows_hashes_and_scope(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]
        sha = sha_of("span-rows")
        legacy = await synthetic_pack(w, sha, R2, [(1, "a"), (1, "b"), (2, "c")])
        await activate(w, legacy)
        async with w.engine.begin() as conn:  # a legacy row whose stored hash was never backfilled (triggers off:
            await conn.execute(text("SET LOCAL session_replication_role = replica"))  # sealed rows are immutable)
            await conn.execute(text("UPDATE material_spans SET quote_sha256 = NULL WHERE ingest_id = :i AND seq = 1"),
                               {"i": await ingest_of(w, legacy)})
        r3 = await synthetic_pack(w, sha, R3, [(1, "a"), (1, "b"), (2, "c")])
        b3 = str(await w.scalar("SELECT locator FROM material_spans WHERE ingest_id = :i AND text = 'b'",
                                i=await ingest_of(w, r3)))
        stored0 = await w.scalar("SELECT quote_sha256 FROM material_spans WHERE ingest_id = :i AND seq = 0",
                                 i=await ingest_of(w, legacy))

        got, texts = await rows(w, [f"{sha}:0", f"{sha}:1", b3])
        assert got == {f"{sha}:0": (1, "a", stored0 or q("a")), f"{sha}:1": (1, "b", q("b"))}  # null → sha256(text)
        assert b3 not in got  # the r3 pack is not active
        assert texts == {k: v[:2] for k, v in got.items()}  # active_span_texts is the (page, text) projection
        assert (await rows(w, [f"{sha}:0", b3], owner="anna"))[0] == {}  # another owner reads nothing

        seqmap = need("reader.ingest.seqmap")
        async with w.engine.begin() as conn:
            assert await seqmap.build(conn, owner, await ingest_of(w, legacy), r3) == "accepted"
        await activate(w, r3)
        stored_b3 = await w.scalar("SELECT quote_sha256 FROM material_spans WHERE locator = :l", l=b3)
        got, texts = await rows(w, [b3, f"{sha}:1"])
        assert got == {b3: (1, "b", stored_b3), f"{sha}:1": (1, "b", stored_b3)}  # mapped legacy id: the r3 hash
        assert texts == {k: v[:2] for k, v in got.items()}
