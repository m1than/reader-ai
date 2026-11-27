"""Owner purge of one pack: the job and the two routes (SRV-INGEST [db]).

@trace FR-INGEST-45
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from .conftest import MATERIAL_SHA, Pg, World, client, need, world
from .test_gc_db import Calls, delete, named, pack, plan_and_report, rows_of, waiting
from .test_manage_db import add_owner
from .test_overlays_db import activate, vectors
from .test_seqmap_db import R2, R3, sha_of
from .test_spanref_db import loc, q, states

pytestmark = pytest.mark.anyio


async def purge(w: World, pid: str, calls: Calls) -> bool:
    async with w.engine.begin() as conn:
        return bool(await need("reader.ingest.gc").purge(conn, w.users["ivan"], pid, calls.seams()))


async def head_packs(w: World) -> list[str]:
    async with w.engine.connect() as conn:
        view = await need("reader.ingest.manifest").active_view(conn, w.users["ivan"])
    return [e["pack_id"] for e in view.entry["packs"]]


async def test_purge_of_an_active_pack(pg: Pg, tmp_path: Path) -> None:
    manifest = need("reader.ingest.manifest")
    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]
        sha = sha_of("purged")
        pid = await pack(w, sha)
        await activate(w, pid)
        at = int(await w.scalar("SELECT max(seq) FROM manifest_entries"))
        ingest = await w.scalar("SELECT ingest_id FROM base_packs WHERE pack_id = :p", p=pid)
        cited = await loc(w, pid, "a")
        await plan_and_report(w, sha, pid)
        calls = Calls()
        assert await purge(w, pid, calls) is True
        assert pid not in await head_packs(w)
        assert int(await w.scalar("SELECT max(seq) FROM manifest_entries")) == at + 1
        assert await rows_of(w, pid) == {"spans": 0, "outline": 0, "profiles": 0, "live_overlays": 0,
                                         "collected": False, "ingest": 1}
        assert await w.scalar("SELECT purged_at IS NOT NULL FROM base_packs WHERE pack_id = :p", p=pid)
        for seam in ("forget_pack", "purge_judge_inputs", "purge_judge_requests"):
            assert calls.of(seam) == [pid], seam
        assert calls.of("search_forget_legacy") == [str(ingest)]
        assert len(calls.of("forget_overlay")) == 2 and len(calls.of("search_forget_overlay")) == 1
        assert await w.scalar("SELECT count(*) FROM freshness_reports WHERE ingest_id = :i", i=ingest) == 1
        assert await w.scalar("SELECT count(*) FROM study_plans WHERE ingest_id = :i", i=ingest) == 1
        assert await states(w, [(cited, q("a"))]) == [("removed", None)]
        async with w.engine.begin() as conn:
            with pytest.raises(manifest.ManifestError) as refused:
                await manifest.rollback(conn, owner, at)
        assert refused.value.code == "pack_purged"
        entries = await w.scalar("SELECT count(*) FROM manifest_entries")
        assert await purge(w, pid, Calls()) is False  # a second purge does nothing
        assert await w.scalar("SELECT count(*) FROM manifest_entries") == entries


async def test_purge_ignores_citations_and_deletes_the_pending_row(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        sha = sha_of("cited and waiting")
        legacy = await pack(w, sha, R2)
        await activate(w, legacy)
        r3 = await pack(w, sha, R3)  # inactive, waiting with its profile
        assert await w.scalar("SELECT count(*) FROM pending_activations WHERE pack_id = :p", p=r3) == 1
        head = await head_packs(w)
        assert await purge(w, r3, Calls(cited=True)) is True
        assert await w.scalar("SELECT count(*) FROM pending_activations WHERE pack_id = :p", p=r3) == 0
        assert (await rows_of(w, r3))["spans"] == 0
        assert await head_packs(w) == head  # not active: no entry appended


async def search_rows(w: World, pid: str, chunks: int = 2) -> Any:
    """Legacy search rows (SRV-SEARCH's tables) of the pack's ingest; returns the ingest id."""
    ingest = await w.scalar("SELECT ingest_id FROM base_packs WHERE pack_id = :p", p=pid)
    sha = await w.scalar("SELECT material_sha256 FROM base_packs WHERE pack_id = :p", p=pid)
    async with w.engine.begin() as conn:
        for chunk_no in range(chunks):
            await conn.execute(text(
                "INSERT INTO search_chunks (owner_id, ingest_id, material_sha256, model, params_sha256,"
                " fts_config, page, chunk_no, char_start, char_stop, span_seqs, primary_seq, text, embedding)"
                " VALUES (:o, :i, :s, 'm', :p, 'russian', 1, :n, 0, 1, '{0}', 0, 'canary text',"
                " array_fill(0.0::real, ARRAY[1024])::vector)"),
                {"o": w.users["ivan"], "i": ingest, "s": sha, "p": "e" * 64, "n": chunk_no})
        await conn.execute(text(
            "INSERT INTO search_embeds (owner_id, ingest_id, model, params_sha256, chunk_count)"
            " VALUES (:o, :i, 'm', :p, :n)"), {"o": w.users["ivan"], "i": ingest, "p": "e" * 64, "n": chunks})
    return ingest


async def search_count(w: World, ingest: Any) -> tuple[int, int]:
    return (await w.scalar("SELECT count(*) FROM search_chunks WHERE ingest_id = :i", i=ingest),
            await w.scalar("SELECT count(*) FROM search_embeds WHERE ingest_id = :i", i=ingest))


async def test_production_seams_purge_an_r2_packs_search_text(pg: Pg, tmp_path: Path) -> None:
    gc = need("reader.ingest.gc")
    async with world(pg, tmp_path) as w:
        purged = await pack(w, sha_of("legacy with chunks"), R2)
        kept = await pack(w, sha_of("another book"), R2)
        ingest, other = await search_rows(w, purged), await search_rows(w, kept)
        async with w.engine.begin() as conn:
            assert await gc.purge(conn, w.users["ivan"], purged, gc.SEAMS[0]) is True
        assert await search_count(w, ingest) == (0, 0)
        assert await search_count(w, other) == (2, 1)
        assert await w.scalar("SELECT purged_at IS NOT NULL FROM base_packs WHERE pack_id = :p", p=purged)


async def test_purge_refuses_while_a_text_seam_is_unbound(pg: Pg, tmp_path: Path) -> None:
    gc = need("reader.ingest.gc")
    async with world(pg, tmp_path) as w:
        pid = await pack(w, sha_of("with vectors"), R2)
        ingest = await search_rows(w, pid)
        async with w.engine.begin() as conn:
            await vectors(conn, w.users["ivan"], pid)  # SRV-SEARCH's forget_overlay is not bound yet
        with pytest.raises(gc.SeamUnbound):
            async with w.engine.begin() as conn:
                await gc.purge(conn, w.users["ivan"], pid, gc.SEAMS[0])
        assert await w.scalar("SELECT purged_at FROM base_packs WHERE pack_id = :p", p=pid) is None
        assert (await rows_of(w, pid))["spans"] == 3 and await search_count(w, ingest) == (2, 1)


async def test_the_routes(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        legacy = await pack(w, MATERIAL_SHA, R2, material_id="synthetic")
        await activate(w, legacy)
        r3 = await pack(w, MATERIAL_SHA, R3, material_id="synthetic")
        other = await pack(w, sha_of("another material"), R2)
        await add_owner(w)
        olga_only = await pack(w, sha_of("olga's book"), R2, owner="olga", material_id="other")
        base = "/api/materials/synthetic/packs"
        async with client(w, jobs=w.jobs) as c:
            listed = (await c.get(base)).json()
            assert [(p["pack_id"], p["converter_version"], p["state"], p["active"], p["collected_at"],
                     p["purged_at"]) for p in listed["packs"]] == [
                (legacy, R2, "sealed", True, None, None), (r3, R3, "sealed", False, None, None)]
            assert (await c.post(f"{base}/{r3}/purge", json={"confirm": "nope"})).json() == {
                "error": "confirm_mismatch"}
            assert (await c.post(f"{base}/{r3}/purge", json={"confirm": "x", "more": 1})).status_code == 400
            for foreign in (other, olga_only, "f" * 64, "zz"):
                got = await c.post(f"{base}/{foreign}/purge", json={"confirm": "synthetic"})
                assert (got.status_code, got.json()) == (404, {"error": "not_found"}), foreign
            assert await w.scalar("SELECT count(*) FROM job_keys WHERE kind = 'ingest.purge'") == 0
            spans = await w.scalar("SELECT count(*) FROM material_spans")
            got = await c.post(f"{base}/{r3}/purge", json={"confirm": "synthetic"})
            assert got.status_code == 202 and got.json() == {"pack_id": r3, "enqueued": True}
            assert await w.scalar("SELECT count(*) FROM material_spans") == spans  # the route deleted nothing
            again = await c.post(f"{base}/{r3}/purge", json={"confirm": "synthetic"})
            assert again.status_code == 202 and again.json() == {"pack_id": r3, "enqueued": False}
        await w.work()
        assert await w.scalar(
            "SELECT purged_at IS NOT NULL FROM base_packs WHERE owner_id = :o AND pack_id = :p",
            o=w.users["ivan"], p=r3)
        async with client(w, jobs=w.jobs) as c:
            listed = (await c.get(base)).json()["packs"]
            assert listed[1]["purged_at"] is not None and listed[0]["purged_at"] is None
        async with client(w, "anna", jobs=w.jobs) as c:  # a learner
            assert (await c.get(base)).json() == {"error": "owner_only"}
            got = await c.post(f"{base}/{legacy}/purge", json={"confirm": "synthetic"})
            assert (got.status_code, got.json()) == (403, {"error": "owner_only"})
        async with client(w, "olga", jobs=w.jobs) as c:  # another owner: the material is not theirs
            assert (await c.get(base)).status_code == 404
            got = await c.post(f"{base}/{legacy}/purge", json={"confirm": "synthetic"})
            assert got.status_code == 404
        async with client(w) as c:  # no job runtime in the api
            got = await c.post(f"{base}/{legacy}/purge", json={"confirm": "synthetic"})
            assert (got.status_code, got.json()) == (503, {"error": "unavailable"})
        assert await w.scalar("SELECT count(*) FROM job_keys WHERE kind = 'ingest.purge'") == 1


async def audit_rows(w: World) -> list[tuple[Any, ...]]:
    rows = await w.rows("SELECT actor_id, object_kind, object_id, detail FROM audit_log"
                        " WHERE action = 'material.purge_pack' ORDER BY id")
    return [tuple(r) for r in rows]


async def test_an_applied_purge_writes_one_audit_row(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gc, jobs = need("reader.ingest.gc"), need("reader.ingest.jobs")
    from reader.jobs.model import JobContext

    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]
        pid = await pack(w, MATERIAL_SHA, R2, material_id="synthetic")
        url = f"/api/materials/synthetic/packs/{pid}/purge"
        async with client(w, jobs=w.jobs) as c:
            assert (await c.post(url, json={"confirm": "nope"})).status_code == 400
            assert (await c.post(f"/api/materials/synthetic/packs/{'f' * 64}/purge",
                                 json={"confirm": "synthetic"})).status_code == 404
        assert await audit_rows(w) == []  # refused requests write none
        monkeypatch.setattr(gc, "SEAMS", [Calls().seams()])
        kind = next(k for k in jobs.make_kinds(w.blobs) if k.name == "ingest.purge")
        for attempt in range(2):  # the second run finds the pack purged: no row
            async with w.engine.begin() as conn:
                await kind.handler(kind.args(owner_id=owner, pack_id=pid), conn,
                                   JobContext(job_id=1, key="k", attempts=attempt))
        assert await audit_rows(w) == [(owner, "material", "synthetic", {"pack_id": pid})]


async def test_restore_racing_purge_of_the_same_pack(pg: Pg, tmp_path: Path) -> None:
    manifest = need("reader.ingest.manifest")
    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]

        async def undelete(conn: Any, sha: str) -> None:
            await conn.execute(text("UPDATE materials SET deleted_at = NULL WHERE sha256 = :s"), {"s": sha})
            await manifest.restore_material(conn, owner, sha)

        async def restore(sha: str) -> None:
            async with w.engine.begin() as conn:
                await undelete(conn, sha)

        first = sha_of("purge first")
        p1 = await pack(w, first)
        await activate(w, p1)
        await delete(w, first)
        async with w.engine.begin() as conn:  # the purge holds the pack lock first
            assert await need("reader.ingest.gc").purge(conn, owner, p1, Calls().seams())
            task = asyncio.create_task(restore(first))
            await waiting(w)
        await task
        assert await named(w, "pack", p1) == 0

        second = sha_of("restore first")
        p2 = await pack(w, second)
        await activate(w, p2)
        await delete(w, second)
        assert await named(w, "pack", p2) == 0
        async with w.engine.begin() as conn:  # the restore holds the pack lock first
            await undelete(conn, second)
            purging = asyncio.create_task(purge(w, p2, Calls()))
            await waiting(w)
        assert await purging is True
        assert await named(w, "pack", p2) == 0  # the purge appended an entry without it
        assert await w.scalar("SELECT purged_at IS NOT NULL FROM base_packs WHERE pack_id = :p", p=p2)
        assert await w.scalar(
            "SELECT count(*) FROM manifest_entries WHERE reason = 'purge'") == 1


async def test_a_dead_purge_is_requeued_by_the_next_request(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gc = need("reader.ingest.gc")
    async with world(pg, tmp_path) as w:
        pid = await pack(w, MATERIAL_SHA, R2, material_id="synthetic")
        async with w.engine.begin() as conn:
            await vectors(conn, w.users["ivan"], pid)  # the production seams refuse: seam_unbound
        url = f"/api/materials/synthetic/packs/{pid}/purge"
        async with client(w, jobs=w.jobs) as c:
            assert (await c.post(url, json={"confirm": "synthetic"})).json()["enqueued"] is True
        await w.work()
        assert await w.scalar("SELECT outcome FROM job_keys WHERE kind = 'ingest.purge'") == "dead"
        assert await w.scalar("SELECT purged_at FROM base_packs WHERE pack_id = :p", p=pid) is None
        monkeypatch.setattr(gc, "SEAMS", [Calls().seams()])  # the seam is bound
        async with client(w, jobs=w.jobs) as c:
            got = await c.post(url, json={"confirm": "synthetic"})
        assert (got.status_code, got.json()) == (202, {"pack_id": pid, "enqueued": True})
        await w.work()
        assert await w.scalar("SELECT outcome FROM job_keys WHERE kind = 'ingest.purge'") == "succeeded"
        assert await w.scalar("SELECT purged_at IS NOT NULL FROM base_packs WHERE pack_id = :p", p=pid)


async def test_a_lost_compare_and_swap_is_retried_not_dead(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gc, manifest = need("reader.ingest.gc"), need("reader.ingest.manifest")
    async with world(pg, tmp_path) as w:
        pid = await pack(w, sha_of("conflict"), R2)

        async def conflict(*args: Any) -> bool:
            raise manifest.ManifestError("manifest_conflict")

        monkeypatch.setattr(manifest, "remove_pack", conflict)
        monkeypatch.setattr(gc, "SEAMS", [Calls().seams()])
        await w.jobs.enqueue("ingest.purge", {"owner_id": str(w.users["ivan"]), "pack_id": pid})
        await w.work()
        rows = await w.rows("SELECT outcome, attempts FROM job_keys WHERE kind = 'ingest.purge'")
        assert [tuple(r) for r in rows] == [(None, 1)]  # counted, not dead: SRV-EVENTS retries it


def test_the_purge_kind_is_declared_with_its_key(tmp_path: Path) -> None:
    kinds: dict[str, Any] = {k.name: k for k in need("reader.ingest.jobs").make_kinds(tmp_path)}
    assert kinds["ingest.purge"].key == ("owner_id", "pack_id")
    assert kinds["ingest.gc"].key == ("slot",)
    assert kinds["ingest.gc_pack"].key == ("owner_id", "pack_id", "slot")
    assert kinds["ingest.gc_overlay"].key == ("owner_id", "overlay_id", "slot")
