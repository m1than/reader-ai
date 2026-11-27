"""Owner purge of one pack: the job and the two routes (SRV-INGEST [db]).

@trace FR-INGEST-45
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from .conftest import MATERIAL_SHA, Pg, World, client, need, world
from .test_gc_db import Calls, pack, plan_and_report, rows_of
from .test_manage_db import add_owner
from .test_overlays_db import activate
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


def test_the_purge_kind_is_declared_with_its_key(tmp_path: Path) -> None:
    kinds: dict[str, Any] = {k.name: k for k in need("reader.ingest.jobs").make_kinds(tmp_path)}
    assert kinds["ingest.purge"].key == ("owner_id", "pack_id")
    assert kinds["ingest.gc"].key == ("slot",)
    assert kinds["ingest.gc_pack"].key == ("owner_id", "pack_id", "slot")
    assert kinds["ingest.gc_overlay"].key == ("owner_id", "overlay_id", "slot")
