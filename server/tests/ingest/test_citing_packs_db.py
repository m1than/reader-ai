"""The one pack each citation names, for SRV-LESSON's revision_packs (SRV-INGEST [db]).

@trace FR-INGEST-49
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, text

from .conftest import Pg, World, build_artifact, need, world
from .packseed import activate_pack
from .test_seqmap_db import R2, R3, sha_of, spans_of, synthetic_pack
from .test_spanref_db import loc, q

pytestmark = pytest.mark.anyio


async def citing(w: World, refs: list[tuple[str, str | None]], owner: str = "ivan") -> set[str]:
    views = need("reader.ingest.views")
    async with w.engine.begin() as conn:
        got: set[str] = await views.citing_packs(conn, w.users[owner], refs)
        xid = (await conn.execute(text("SELECT pg_current_xact_id_if_assigned()"))).scalar()
        assert xid is None  # reads only
    return got


async def activate(w: World, pack: str) -> None:
    async with w.engine.begin() as conn:
        await activate_pack(conn, w.users["ivan"], pack)


async def test_one_pack_per_locator(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        sha = sha_of("citing")
        legacy = await synthetic_pack(w, sha, R2, [(1, "a"), (1, "b"), (2, "c")])
        r3 = await synthetic_pack(w, sha, R3, [(1, "a"), (1, "b"), (2, "c2")])
        a, c2 = await loc(w, r3, "a"), await loc(w, r3, "c2")
        assert a == await loc(w, legacy, "a")  # the backfilled legacy pack holds r3's (locator, hash)

        # Neither active: two sealed packs hold it, the lower pack id is named.
        assert await citing(w, [(a, q("a"))]) == {min(legacy, r3)}
        assert await citing(w, [(c2, q("c2"))]) == {r3}  # held only by an inactive pack

        await activate(w, legacy)  # the legacy pack active, r3 sealed and waiting
        assert await citing(w, [(a, q("a"))]) == {legacy}
        assert await citing(w, [(c2, q("c2"))]) == {r3}
        await activate(w, r3)  # r3 active
        assert await citing(w, [(a, q("a"))]) == {r3}
        assert await citing(w, [(a, q("a")), (c2, q("c2"))]) == {r3}

        nothing: list[tuple[str, str | None]] = [
            (a, q("other")),  # the same locator with another hash
            (a, None),  # a locator without a hash
            (f"{sha}:0", None),  # a legacy id
            (f"{sha}:0", q("a")),
            ("nonsense", q("a")),
            (f"spn:{sha}:p1:9-3", q("a")),  # malformed (start after end)
            (f"spn:{sha}:p9:0-1", q("a")),  # no such span
        ]
        assert await citing(w, nothing) == set()
        assert await citing(w, []) == set()


async def test_collected_and_foreign_packs_are_never_named(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        sha = sha_of("collected")
        legacy = await synthetic_pack(w, sha, R2, [(1, "a")])
        r3 = await synthetic_pack(w, sha, R3, [(1, "a")])
        a = await loc(w, r3, "a")
        async with w.engine.begin() as conn:
            await conn.execute(text("UPDATE base_packs SET collected_at = now() WHERE pack_id = :p"),
                               {"p": min(legacy, r3)})
        assert await citing(w, [(a, q("a"))]) == {max(legacy, r3)}

        # anna's own copy of another material, the same (locator, hash): never named for ivan, named for her.
        other = sha_of("anna")
        files = build_artifact(spans=spans_of([(1, "z")]), material_sha256=other, converter_version=R3)
        async with w.engine.begin() as conn:
            assert await need("reader.ingest.store").import_rows(
                conn, w.users["anna"], other[:12], "e" * 64, json.loads(files["manifest.json"]),
                files["spans.jsonl"], json.loads(files["outline.json"]))
        z = str(await w.scalar("SELECT locator FROM material_spans WHERE owner_id = :o AND text = 'z'",
                               o=w.users["anna"]))
        assert await citing(w, [(z, q("z"))]) == set()
        assert len(await citing(w, [(z, q("z"))], owner="anna")) == 1


async def test_bounded_statements(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        sha = sha_of("bounded")
        rows = [(1 + i // 20, f"span {i}") for i in range(200)]
        r3 = await synthetic_pack(w, sha, R3, rows)
        await activate(w, r3)
        found = await w.rows("SELECT locator, quote_sha256 FROM material_spans WHERE ingest_id ="
                             " (SELECT ingest_id FROM base_packs WHERE pack_id = :p)", p=r3)
        refs: list[tuple[str, str | None]] = [(str(r[0]), str(r[1])) for r in found]
        assert len(refs) == 200
        statements: list[str] = []

        def count(*args: Any) -> None:
            statements.append(str(args[2]))

        event.listen(w.engine.sync_engine, "before_cursor_execute", count)
        try:
            assert await citing(w, refs) == {r3}
        finally:
            event.remove(w.engine.sync_engine, "before_cursor_execute", count)
        assert len([s for s in statements if "pg_current_xact_id" not in s]) <= 2
