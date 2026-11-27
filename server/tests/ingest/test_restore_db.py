"""Activation run-through, restore and rollback with the seq map and re-anchoring (SRV-INGEST [db]).

@trace FR-INGEST-39 FR-INGEST-43 FR-INGEST-40
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from .conftest import Pg, World, need, world
from .test_overlays_db import activate, vectors
from .test_seqmap_db import R2, R3, ingest_of, sha_of, synthetic_pack
from .test_spanref_db import R4, replace, states, versions

pytestmark = pytest.mark.anyio
ROWS = [(1, "a"), (1, "b")]


async def head_packs(w: World) -> dict[str, dict[str, str]]:
    async with w.engine.connect() as conn:
        view = await need("reader.ingest.manifest").active_view(conn, w.users["ivan"])
    return {e["pack_id"]: e["overlays"] for e in view.entry["packs"]}


async def reason_of(w: World, pack: str) -> Any:
    return await w.scalar("SELECT reason FROM pending_activations WHERE pack_id = :p", p=pack)


async def entries(w: World) -> int:
    return int(await w.scalar("SELECT count(*) FROM manifest_entries"))


async def with_vectors(w: World, pack: str, expect: str) -> str:
    manifest = need("reader.ingest.manifest")
    async with w.engine.begin() as conn:
        v = await vectors(conn, w.users["ivan"], pack)
        await manifest.request_activation(conn, w.users["ivan"], pack, {"vectors": v})
        assert await manifest.try_activate(conn, w.users["ivan"], pack) == expect
    return v


async def mapped(w: World, legacy: str, new: str, expect: str = "accepted") -> None:
    async with w.engine.begin() as conn:
        legacy_ingest = await ingest_of(w, legacy)
        built = await need("reader.ingest.seqmap").build(conn, w.users["ivan"], legacy_ingest, new)
        assert built == expect


async def delete(w: World, sha: str) -> None:
    async with w.engine.begin() as conn:
        await conn.execute(text("UPDATE materials SET deleted_at = now() WHERE sha256 = :s"), {"s": sha})
        await need("reader.ingest.manifest").remove_material(conn, w.users["ivan"], sha)


async def restore(w: World, sha: str, ensure: Any = None) -> int:
    """The material row comes back, then FR-INGEST-39's restore; returns the pack locks held at its end."""
    async with w.engine.begin() as conn:
        await conn.execute(text("UPDATE materials SET deleted_at = NULL WHERE sha256 = :s"), {"s": sha})
        await need("reader.ingest.manifest").restore_material(conn, w.users["ivan"], sha, ensure=ensure)
        return int((await conn.execute(text(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
            " AND classid = :ns"), {"ns": need("reader.ingest.packs").LOCK_NAMESPACE})).scalar_one())


async def test_a_waiting_activation_runs_through(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        sha = sha_of("run-through")
        legacy = await synthetic_pack(w, sha, R2, ROWS)
        await activate(w, legacy)
        r3 = await synthetic_pack(w, sha, R3, ROWS)
        p3 = await w.scalar("SELECT overlays ->> 'profile' FROM pending_activations WHERE pack_id = :p", p=r3)
        v3 = await with_vectors(w, r3, "seqmap_missing")
        await mapped(w, legacy, r3)  # its try_activate: anchors_missing, ingest.reanchor enqueued
        assert await reason_of(w, r3) == "anchors_missing"
        await w.work()
        assert await head_packs(w) == {r3: {"profile": p3, "vectors": v3}}
        assert await w.scalar("SELECT count(*) FROM pending_activations") == 0


async def test_delete_while_waiting_then_restore(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        sha = sha_of("delete-waiting")
        legacy = await synthetic_pack(w, sha, R2, ROWS)
        await activate(w, legacy)
        r3 = await synthetic_pack(w, sha, R3, ROWS)
        v3 = await with_vectors(w, r3, "seqmap_missing")
        await delete(w, sha)
        assert await head_packs(w) == {} and await entries(w) == 2
        await mapped(w, legacy, r3)
        assert await w.scalar("SELECT reason FROM pending_activations WHERE pack_id = :p", p=r3) == "deleted"
        assert await restore(w, sha) == 2  # both packs' locks, taken in one first call
        assert list(await head_packs(w)) == [r3] and (await head_packs(w))[r3]["vectors"] == v3
        assert await w.scalar("SELECT count(*) FROM pending_activations") == 0
        assert await w.scalar("SELECT count(*) FROM anchor_runs WHERE from_pack_id = :f AND to_pack_id = :t",
                              f=legacy, t=r3) == 1  # re-anchored inline


async def test_restore_without_a_worker_stores_the_wait_and_commits(pg: Pg, tmp_path: Path) -> None:
    """Code review of unit B, blocking 1: the api runs no worker; the follow-up is skipped, not raised."""
    from reader.jobs import worker as jobs_worker

    async with world(pg, tmp_path) as w:
        sha = sha_of("no worker")
        legacy = await synthetic_pack(w, sha, R2, ROWS)
        await activate(w, legacy)
        r3 = await synthetic_pack(w, sha, R3, ROWS)
        await with_vectors(w, r3, "seqmap_missing")
        await delete(w, sha)
        previous, jobs_worker._RUNNING[0] = jobs_worker._RUNNING[0], None
        try:
            await restore(w, sha)
        finally:
            jobs_worker._RUNNING[0] = previous
        restored = await w.scalar("SELECT deleted_at FROM materials WHERE sha256 = :s", s=sha)
        assert restored is None  # the restore committed
        assert await reason_of(w, r3) == "seqmap_missing"
        assert list(await head_packs(w)) == [legacy]


async def test_one_seq_map_suffices_and_restore_skips_older_rows(
    pg: Pg, tmp_path: Path, monkeypatch: Any
) -> None:
    versions(monkeypatch)
    jobs = need("reader.ingest.jobs")
    async with world(pg, tmp_path) as w:
        sha = sha_of("one-map")
        legacy = await synthetic_pack(w, sha, R2, ROWS)
        await activate(w, legacy)
        r3 = await synthetic_pack(w, sha, R3, ROWS)
        await mapped(w, legacy, r3)
        r3 = await replace_existing(w, legacy, r3)
        r4 = await replace(w, sha, r3, R4, [(1, "z"), (1, "a"), (1, "b")])
        assert list(await head_packs(w)) == [r4]
        assert await w.scalar("SELECT count(*) FROM seq_maps") == 1
        async with w.engine.begin() as conn:
            assert "ingest.seqmap" not in await jobs.enqueue_followups(conn, w.users["ivan"], r4)
        assert await states(w, [(f"{sha}:0", None)]) == [("reanchored", "a")]  # the map, then one hop

        other = sha_of("mapped-never-active")  # r3 mapped but never activated; r4 replaces the legacy pack
        legacy2 = await synthetic_pack(w, other, R2, ROWS)
        await activate(w, legacy2)
        r3b = await synthetic_pack(w, other, R3, ROWS)
        await mapped(w, legacy2, r3b)
        r4b = await replace(w, other, legacy2, R4, [(1, "a"), (1, "b2")])
        got = await states(w, [(f"{other}:0", None), (f"{other}:1", None)])
        assert got == [("active", "a"), ("inactive", "b")]
        assert await w.scalar("SELECT count(*) FROM pending_activations WHERE pack_id = :p", p=r3b) == 1
        await delete(w, other)
        await restore(w, other)  # r3b's row is older than the last element's pack: skipped and deleted
        assert set(await head_packs(w)) == {r4, r4b}
        assert await w.scalar("SELECT count(*) FROM pending_activations WHERE pack_id = :p", p=r3b) == 0


async def replace_existing(w: World, old: str, new: str) -> str:
    """Activate an already imported pack over `old`: its vectors, the inline re-anchoring, try_activate."""
    manifest = need("reader.ingest.manifest")
    async with w.engine.begin() as conn:
        v = await vectors(conn, w.users["ivan"], new)
        await manifest.request_activation(conn, w.users["ivan"], new, {"vectors": v})
        await need("reader.ingest.reanchor").run(conn, w.users["ivan"], old, new)
        assert await manifest.try_activate(conn, w.users["ivan"], new) == "appended"
    return new


async def test_rollback_drops_deleted_materials_and_reanchors_inline(pg: Pg, tmp_path: Path) -> None:
    manifest = need("reader.ingest.manifest")
    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]
        a, b = sha_of("A"), sha_of("B")
        pa = await synthetic_pack(w, a, R2, ROWS)
        await activate(w, pa)
        pb = await synthetic_pack(w, b, R2, ROWS)
        await activate(w, pb)  # entry 2 names A and B
        await delete(w, b)  # entry 3 names A
        async with w.engine.begin() as conn:
            va2 = await vectors(conn, owner, pa, model="m2")
            await manifest.request_activation(conn, owner, pa, {"vectors": va2})
            assert await manifest.try_activate(conn, owner, pa) == "appended"  # entry 4: A switched
            assert await manifest.rollback(conn, owner, 2) == 5
        assert list(await head_packs(w)) == [pa] and (await head_packs(w))[pa]["vectors"] != va2
        await restore(w, b)
        assert set(await head_packs(w)) == {pa, pb}

        c = sha_of("C")
        legacy = await synthetic_pack(w, c, R2, ROWS)
        await activate(w, legacy)
        before = await w.scalar("SELECT max(seq) FROM manifest_entries")
        r3 = await synthetic_pack(w, c, R3, ROWS)
        await mapped(w, legacy, r3)
        await replace_existing(w, legacy, r3)
        after = await w.scalar("SELECT max(seq) FROM manifest_entries")
        async with w.engine.begin() as conn:  # back to the legacy pack: the reverse pair is anchored inline
            await manifest.rollback(conn, owner, before)
        async with w.engine.begin() as conn:
            await manifest.rollback(conn, owner, after)
        async with w.engine.begin() as conn:
            await manifest.rollback(conn, owner, before)
            with pytest.raises(manifest.ManifestError) as same:
                await manifest.rollback(conn, owner, before)
        assert same.value.code == "no_change"
        runs = await w.rows("SELECT from_pack_id, to_pack_id FROM anchor_runs WHERE owner_id = :o", o=owner)
        assert sorted(tuple(r) for r in runs) == sorted([(legacy, r3), (r3, legacy)])
        assert legacy in await head_packs(w)


async def test_restore_of_a_collected_pack_calls_ensure(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        sha = sha_of("collected")
        legacy = await synthetic_pack(w, sha, R2, ROWS)
        await activate(w, legacy)
        r3 = await synthetic_pack(w, sha, R3, ROWS)  # waiting with its profile only
        await delete(w, sha)
        async with w.engine.begin() as conn:
            await conn.execute(text("UPDATE base_packs SET collected_at = now() WHERE pack_id = :p"),
                               {"p": legacy})
        calls: list[str] = []

        async def ensure(conn: Any, owner_id: Any, material: str) -> None:
            calls.append(material)

        count = await entries(w)
        await restore(w, sha, ensure)
        assert await entries(w) == count  # no request for the collected pack; r3 waits on its seq map
        assert await reason_of(w, r3) == "seqmap_missing"
        assert calls == [sha]

        never = sha_of("never")
        await synthetic_pack(w, never, R2, ROWS)
        async with w.engine.begin() as conn:
            await conn.execute(text("DELETE FROM pending_activations WHERE material_sha256 = :s"),
                               {"s": never})
        await restore(w, never)
        assert await entries(w) == count  # never activated, nothing waiting: nothing appended
