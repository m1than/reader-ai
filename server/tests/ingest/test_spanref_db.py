"""The total read-time resolver of citation references (SRV-INGEST [db]).

@trace FR-INGEST-42 INV-INGEST-15
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from .conftest import Pg, World, need, world
from .test_overlays_db import activate, vectors
from .test_seqmap_db import R2, R3, ingest_of, sha_of, synthetic_pack

pytestmark = pytest.mark.anyio
R4, R5 = "docling-2.151.1+r4", "docling-2.151.1+r5"


def q(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def versions(monkeypatch: Any) -> None:
    """Two later converter revisions as data (PARSE_CONFIGS order is the converter-version order)."""
    ingest = need("reader.ingest")
    for version in (R4, R5):
        config = {**ingest.PARSE_CONFIGS[R3], "converter_version": version}
        monkeypatch.setitem(ingest.PARSE_CONFIGS, version, config)


async def replace(w: World, sha: str, old: str, version: str, rows: list[tuple[int, str]]) -> str:
    """A new pack of `sha` activated over `old`: its vectors, the inline re-anchoring, try_activate."""
    manifest, reanchor = need("reader.ingest.manifest"), need("reader.ingest.reanchor")
    owner = w.users["ivan"]
    new = await synthetic_pack(w, sha, version, rows)
    async with w.engine.begin() as conn:
        v = await vectors(conn, owner, new)
        await manifest.request_activation(conn, owner, new, {"vectors": v})
        await reanchor.run(conn, owner, old, new)
        assert await manifest.try_activate(conn, owner, new) == "appended"
    return new


async def loc(w: World, pack: str, body: str) -> str:
    return str(await w.scalar("SELECT locator FROM material_spans WHERE ingest_id = :i AND text = :t",
                              i=await ingest_of(w, pack), t=body))


async def states(w: World, refs: list[tuple[str, str | None]], owner: str = "ivan") -> list[tuple[Any, ...]]:
    spanref = need("reader.ingest.spanref")
    async with w.engine.begin() as conn:
        got = await spanref.resolve(conn, w.users[owner], refs)
        xid = (await conn.execute(text("SELECT pg_current_xact_id_if_assigned()"))).scalar()
        assert xid is None  # the resolver wrote nothing
    return [(r.state, r.text) for r in got]


async def test_one_ref_per_state(pg: Pg, tmp_path: Path, monkeypatch: Any) -> None:
    versions(monkeypatch)
    seqmap = need("reader.ingest.seqmap")
    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]
        sha, other = sha_of("spanref"), sha_of("other")
        legacy = await synthetic_pack(w, sha, R2, [(1, "a"), (1, "b"), (2, "c")])
        await activate(w, legacy)
        r3 = await synthetic_pack(w, sha, R3, [(1, "a"), (1, "b"), (2, "c")])
        async with w.engine.begin() as conn:
            assert await seqmap.build(conn, owner, await ingest_of(w, legacy), r3) == "accepted"
        a3, c3 = await loc(w, r3, "a"), await loc(w, r3, "c")
        assert await states(w, [("nonsense", None), (a3, None), (a3, "0" * 63)]) == [("invalid", None)] * 3
        assert await states(w, [(f"{other}:0", None), (f"{sha}:99", None)]) == [("unmapped", None)] * 2
        assert await states(w, [(f"{sha}:1", None), (a3, q("a"))]) == [("active", "b"), ("active", "a")]

        r4 = await replace(w, sha, legacy, R4, [(1, "z"), (1, "a"), (1, "b"), (2, "c2")])
        assert a3 != await loc(w, r4, "a")
        assert await states(w, [(a3, q("a")), (f"{sha}:0", None)]) == [("reanchored", "a")] * 2  # one hop
        async with w.engine.begin() as conn:  # a chain from r3 whose target hash differs stops; legacy's wins
            await conn.execute(text(
                "INSERT INTO anchor_maps (owner_id, from_pack_id, to_pack_id, from_locator, quote_sha256,"
                " to_locator, state) VALUES (:o, :f, :t, :fl, :q, :tl, 'mapped')"),
                {"o": owner, "f": r3, "t": r4, "fl": a3, "q": q("a"), "tl": await loc(w, r4, "z")})
        assert await states(w, [(a3, q("a"))]) == [("reanchored", "a")]
        assert await states(w, [(c3, q("c"))]) == [("inactive", "c")]  # orphaned by the anchors, held by r3
        missing = (f"spn:{sha}:p1:0-3", q("nope"))
        assert await states(w, [missing, (f"spn:{other}:p1:0-1", q("a"))]) == [("orphaned", None)] * 2

        r5 = await replace(w, sha, r4, R5, [(1, "y"), (1, "z"), (1, "a"), (1, "b"), (2, "c2")])
        async with w.engine.begin() as conn:  # a cycle r5 → r4 for "a" terminates
            await conn.execute(text(
                "INSERT INTO anchor_maps (owner_id, from_pack_id, to_pack_id, from_locator, quote_sha256,"
                " to_locator, state) VALUES (:o, :f, :t, :fl, :q, :tl, 'mapped')"),
                {"o": owner, "f": r5, "t": r4, "fl": await loc(w, r5, "a"), "q": q("a"),
                 "tl": await loc(w, r4, "a")})
        assert await states(w, [(a3, q("a"))]) == [("reanchored", "a")]  # two hops: legacy → r4 → r5

        assert await states(w, [(a3, q("a")), (f"{sha}:1", None)], owner="anna") == [
            ("orphaned", None), ("unmapped", None)]  # another owner's material is never served
        await sql_collect(w, [legacy, r3])
        assert await states(w, [(c3, q("c")), missing]) == [("removed", None)] * 2


async def sql_collect(w: World, pack_ids: list[str]) -> None:
    async with w.engine.begin() as conn:
        for pid in pack_ids:
            await conn.execute(text("UPDATE base_packs SET collected_at = now() WHERE pack_id = :p"),
                               {"p": pid})


async def anchor(w: World, src: str, dst: str, from_loc: str, quote: str, to_loc: str) -> None:
    async with w.engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO anchor_maps (owner_id, from_pack_id, to_pack_id, from_locator, quote_sha256,"
            " to_locator, state) VALUES (:o, :f, :t, :fl, :q, :tl, 'mapped')"),
            {"o": w.users["ivan"], "f": src, "t": dst, "fl": from_loc, "q": quote, "tl": to_loc})


async def test_code_review_unit_a_edges(pg: Pg, tmp_path: Path, monkeypatch: Any) -> None:
    """Code review of unit A: seq overflow, another hash in an inactive pack, hash-bound hops, cycles."""
    versions(monkeypatch)
    seqmap = need("reader.ingest.seqmap")
    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]
        sha = sha_of("edges")
        legacy = await synthetic_pack(w, sha, R2, [(1, "a"), (1, "zz")])
        await activate(w, legacy)
        r3 = await synthetic_pack(w, sha, R3, [(1, "zz"), (1, "a")])
        async with w.engine.begin() as conn:
            assert (await seqmap.build(conn, owner, await ingest_of(w, legacy), r3)).startswith("refused")
        assert await states(w, [(f"{sha}:2147483648", None), (f"{sha}:999999999999", None)]) == [
            ("unmapped", None)] * 2
        zz3, zz_legacy = await loc(w, r3, "zz"), await loc(w, legacy, "zz")
        assert zz3 not in (zz_legacy, await loc(w, legacy, "a"))
        got = await states(w, [(zz3, q("zz")), (zz3, q("other"))])
        assert got == [("inactive", "zz"), ("orphaned", None)]
        await anchor(w, r3, legacy, zz3, q("a"), zz_legacy)  # same from-locator, another hash: not followed
        assert await states(w, [(zz3, q("zz"))]) == [("inactive", "zz")]
        r4 = await synthetic_pack(w, sha, R4, [(1, "xyz"), (1, "zz")])  # never active; zz at 4-6
        zz4 = await loc(w, r4, "zz")
        await anchor(w, r3, r4, zz3, q("zz"), zz4)
        await anchor(w, r4, r3, zz4, q("zz"), zz3)  # a cycle between two non-active packs terminates
        assert await states(w, [(zz3, q("zz")), (zz4, q("zz"))]) == [("inactive", "zz")] * 2
