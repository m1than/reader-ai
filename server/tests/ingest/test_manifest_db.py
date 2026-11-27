"""The manifest: CAS appends, refusals, locks, views and the activation contract (SRV-INGEST [db]).

@trace FR-INGEST-38 FR-INGEST-39 INV-INGEST-13
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from .conftest import MATERIAL_SHA, Pg, World, build_artifact, canonical, need, world
from .test_overlays_db import activate, vectors
from .test_packs_db import imported

pytestmark = pytest.mark.anyio
OTHER = "b" * 64
R3 = "docling-2.151.1+r3"


def entry(*elements: tuple[str, dict[str, str]]) -> dict[str, Any]:
    return {"packs": sorted(({"pack_id": p, "overlays": o} for p, o in elements), key=lambda e: e["pack_id"]),
            "registry_view": None}


async def head_element(w: World, pack: str) -> dict[str, str]:
    async with w.engine.connect() as conn:
        view = await need("reader.ingest.manifest").active_view(conn, w.users["ivan"])
    return next(e["overlays"] for e in view.entry["packs"] if e["pack_id"] == pack)


async def r3_pack(w: World) -> str:
    """A second pack of MATERIAL_SHA under the next converter version (import_rows, bypassing receive)."""
    files = build_artifact(converter_version=R3)
    async with w.engine.begin() as conn:
        assert await need("reader.ingest.store").import_rows(
            conn, w.users["ivan"], "synthetic", "e" * 64, json.loads(files["manifest.json"]),
            files["spans.jsonl"], json.loads(files["outline.json"]))
    return str(need("reader.ingest.packs").pack_id(MATERIAL_SHA, R3))


async def sql(w: World, statement: str, **params: Any) -> None:
    async with w.engine.begin() as conn:
        await conn.execute(text(statement), params)


async def code_of(w: World, parent: int, content: dict[str, Any]) -> str:
    manifest = need("reader.ingest.manifest")
    async with w.engine.begin() as conn:
        with pytest.raises(manifest.ManifestError) as refusal:
            await manifest.append(conn, w.users["ivan"], parent, content, "test")
    return str(refusal.value.code)


def test_brain_version_is_the_canonical_hash() -> None:
    manifest = need("reader.ingest.manifest")
    e = entry(("1" * 64, {"profile": "2" * 64}))
    assert manifest.brain_version(e) == hashlib.sha256(canonical(e)).hexdigest()
    assert manifest.brain_version(json.loads(json.dumps(e))) == manifest.brain_version(e)
    assert manifest.VIEWS == ("active_view", "profile_for_pack")


async def test_chain_refusals_and_append_only(pg: Pg, tmp_path: Path) -> None:
    manifest = need("reader.ingest.manifest")
    overlays = need("reader.ingest.overlays")
    async with world(pg, tmp_path) as w:
        a, b = await imported(w), await imported(w, OTHER)
        va = await activate(w, a)
        pa = (await head_element(w, a))["profile"]
        owner = w.users["ivan"]
        async with w.engine.begin() as conn:
            building = overlays.overlay_id("vectors", b, {"x": 1}, "t/1")
            await overlays.begin(conn, owner, building, kind="vectors", pack_id=b, inputs={"x": 1},
                                 builder_version="t/1")
            vb = await vectors(conn, owner, b)
        base = (a, {"profile": pa, "vectors": va})
        assert await code_of(w, 1, entry(base, (b, {"vectors": building}))) == "not_sealed"
        assert await code_of(w, 1, entry(base, (b, {"vectors": va}))) == "overlay_mismatch"
        assert await code_of(w, 1, entry(base)) == "no_change"
        assert await code_of(w, 0, entry(base, (b, {"vectors": vb}))) == "manifest_conflict"
        r3 = await r3_pack(w)
        assert await code_of(w, 1, entry(base, (r3, {}))) == "material_twice"
        await sql(w, "UPDATE overlays SET collected_at = now() WHERE overlay_id = :i", i=vb)
        assert await code_of(w, 1, entry(base, (b, {"vectors": vb}))) == "overlay_collected"
        await sql(w, "UPDATE base_packs SET collected_at = now() WHERE pack_id = :p", p=b)
        assert await code_of(w, 1, entry(base, (b, {}))) == "pack_collected"
        await sql(w, "UPDATE base_packs SET purged_at = now() WHERE pack_id = :p", p=r3)
        assert await code_of(w, 1, entry((r3, {}))) == "pack_purged"
        assert await w.scalar("SELECT count(*) FROM manifest_entries") == 1
        for statement in ("UPDATE manifest_entries SET reason = 'x'", "DELETE FROM manifest_entries",
                          "TRUNCATE manifest_entries"):
            async with w.engine.connect() as conn:
                await conn.begin()
                with pytest.raises(DBAPIError):
                    await conn.execute(text(statement))
                await conn.rollback()
        async with w.engine.connect() as conn:  # another owner's manifest is its own
            assert (await manifest.active_view(conn, w.users["anna"])).seq == 0


async def test_concurrent_appenders_never_fork(pg: Pg, tmp_path: Path) -> None:
    manifest = need("reader.ingest.manifest")
    async with world(pg, tmp_path) as w:
        a = await imported(w)
        owner = w.users["ivan"]
        pa = (await w.rows("SELECT overlays FROM pending_activations"))[0][0]["profile"]
        async with w.engine.begin() as conn:
            ids = [await vectors(conn, owner, a, model=f"m{i}") for i in range(8)]

        async def one(vid: str) -> str:
            async with w.engine.begin() as conn:
                try:
                    await manifest.append(conn, owner, 0, entry((a, {"profile": pa, "vectors": vid})), "t")
                    return "won"
                except manifest.ManifestError as error:
                    return str(error.code)

        results = await asyncio.gather(*(one(v) for v in ids))
        assert sorted(results) == ["manifest_conflict"] * 7 + ["won"]
        rows = await w.rows("SELECT seq, parent_seq FROM manifest_entries WHERE owner_id = :o ORDER BY seq",
                            o=owner)
        assert [tuple(r) for r in rows] == [(1, 0)]


async def _blocks(w: World, held: str, parent: int, content: dict[str, Any]) -> bool:
    """True when the append waits on `held`'s pack lock (lock_timeout while another session holds it)."""
    manifest = need("reader.ingest.manifest")
    packs = need("reader.ingest.packs")
    holder = await w.engine.connect()
    await holder.begin()
    await packs.lock_packs(holder, [held])
    try:
        async with w.engine.connect() as conn:
            await conn.begin()
            await conn.execute(text("SET LOCAL lock_timeout = '300ms'"))
            try:
                await manifest.append(conn, w.users["ivan"], parent, content, "probe")
            except DBAPIError as error:
                timed_out = "lock timeout" in str(error)
                if not timed_out:
                    raise
                return True
            finally:
                await conn.rollback()
            return False
    finally:
        await holder.rollback()
        await holder.close()


async def test_append_locks_exactly_the_changed_packs(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        a, b = await imported(w), await imported(w, OTHER)
        va = await activate(w, a)
        ea = await head_element(w, a)
        add_b = entry((a, ea), (b, {"profile": (await w.rows(
            "SELECT overlays FROM pending_activations WHERE pack_id = :p", p=b))[0][0]["profile"]}))
        assert await _blocks(w, b, 1, add_b)  # an added pack
        assert not await _blocks(w, a, 1, add_b)  # a kept, unchanged pack
        async with w.engine.begin() as conn:
            await need("reader.ingest.manifest").append(conn, w.users["ivan"], 1, add_b, "t")
            va2 = await vectors(conn, w.users["ivan"], a, model="m2")
        changed = entry((a, {**ea, "vectors": va2}))  # a's overlay changes, b is removed
        assert await _blocks(w, a, 2, changed)
        assert not await _blocks(w, b, 2, changed)
        assert va != va2


async def test_activation_contract(pg: Pg, tmp_path: Path) -> None:
    manifest = need("reader.ingest.manifest")
    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]
        a = await imported(w)
        pa = (await w.rows("SELECT overlays FROM pending_activations"))[0][0]["profile"]
        async with w.engine.begin() as conn:
            older = await vectors(conn, owner, a, model="old")
            await vectors(conn, owner, a, model="new")  # sealed later, never chosen by recency
            await manifest.request_activation(conn, owner, a, {"vectors": older})
            assert await manifest.try_activate(conn, owner, a) == "appended"
            assert await manifest.try_activate(conn, owner, a) is None  # no pending row
            await manifest.request_activation(conn, owner, a, {"vectors": older})
            assert await manifest.try_activate(conn, owner, a) == "no_change"
        assert await head_element(w, a) == {"profile": pa, "vectors": older}
        assert await w.scalar("SELECT count(*) FROM pending_activations") == 0

        r3 = await r3_pack(w)  # replaces a: needs its own vectors, then the seq map
        async with w.engine.begin() as conn:
            assert await manifest.try_activate(conn, owner, r3) == "vectors_missing"
        async with w.engine.begin() as conn:
            v3 = await vectors(conn, owner, r3)
            await manifest.request_activation(conn, owner, r3, {"vectors": v3})
            assert await manifest.try_activate(conn, owner, r3) == "seqmap_missing"
        reason = await w.scalar("SELECT reason FROM pending_activations WHERE pack_id = :p", p=r3)
        assert reason == "seqmap_missing"
        assert await w.scalar("SELECT count(*) FROM manifest_entries") == 1
        legacy = await w.scalar("SELECT ingest_id FROM base_packs WHERE pack_id = :p", p=a)
        async with w.engine.begin() as conn:  # the seq map and the anchor run, as unit 3's jobs write them
            await conn.execute(text(
                "INSERT INTO seq_maps (owner_id, from_ingest_id, to_pack_id, state, span_count)"
                " VALUES (:o, :i, :p, 'accepted', 8)"), {"o": owner, "i": legacy, "p": r3})
            await conn.execute(text(
                "INSERT INTO anchor_runs (owner_id, from_pack_id, to_pack_id, mapped, orphaned,"
                " finished_at) VALUES (:o, :f, :t, 8, 0, now())"),
                               {"o": owner, "f": a, "t": r3})
            assert await manifest.try_activate(conn, owner, r3) == "appended"
            await manifest.request_activation(conn, owner, a, {"vectors": older})
            assert await manifest.try_activate(conn, owner, a) == "superseded"
        assert await w.scalar("SELECT count(*) FROM pending_activations") == 0
        async with w.engine.connect() as conn:
            assert [e["pack_id"] for e in (await manifest.active_view(conn, owner)).entry["packs"]] == [r3]

        b = await imported(w, OTHER)
        async with w.engine.begin() as conn:
            await conn.execute(text("UPDATE materials SET deleted_at = now() WHERE sha256 = :s"),
                               {"s": OTHER})
            assert await manifest.try_activate(conn, owner, b) == "deleted"


async def test_conflict_is_stored_and_the_sweep_appends(pg: Pg, tmp_path: Path, monkeypatch: Any) -> None:
    manifest = need("reader.ingest.manifest")
    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]
        a = await imported(w)

        async def lost(*_: Any, **__: Any) -> None:
            raise manifest.ManifestError("manifest_conflict")

        with monkeypatch.context() as patch:
            patch.setattr(manifest, "_insert", lost)
            async with w.engine.begin() as conn:
                assert await manifest.try_activate(conn, owner, a) == "manifest_conflict"
        assert await w.scalar("SELECT reason FROM pending_activations") == "manifest_conflict"
        await manifest.sweep(w.engine)
        assert await w.scalar("SELECT count(*) FROM manifest_entries") == 1
        assert await w.scalar("SELECT count(*) FROM pending_activations") == 0


async def test_delete_appends_one_entry_only_when_named(pg: Pg, tmp_path: Path) -> None:
    manifest = need("reader.ingest.manifest")
    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]
        a, b = await imported(w), await imported(w, OTHER)
        await activate(w, a)
        async with w.engine.begin() as conn:
            assert await manifest.remove_material(conn, owner, MATERIAL_SHA) is True
            assert await manifest.remove_material(conn, owner, MATERIAL_SHA) is False
            assert await manifest.remove_material(conn, owner, OTHER) is False  # never activated
            view = await manifest.active_view(conn, owner)
        assert view.seq == 2 and view.entry["packs"] == []
        assert await w.scalar("SELECT count(*) FROM pending_activations WHERE pack_id = :p", p=b) == 1
