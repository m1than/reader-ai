"""Garbage collection of packs and overlays, its races and the no-deletion-on-a-request-path rule
(SRV-INGEST [db], [unit]).

@trace FR-INGEST-44 INV-INGEST-14
"""

from __future__ import annotations

import ast
import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from .conftest import SRC, Pg, World, build_artifact, need, world
from .test_manage_db import add_owner
from .test_overlays_db import activate, vectors
from .test_seqmap_db import R2, R3, sha_of, spans_of

pytestmark = pytest.mark.anyio
ROWS = [(1, "a"), (1, "b"), (2, "c")]
LATER = timedelta(days=31)


class Calls:
    """Test doubles for reader.ingest.gc.Seams: every call recorded as (seam, argument)."""

    def __init__(self, cited: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.is_cited = cited

    def seams(self) -> Any:
        async def record(name: str, value: Any) -> None:
            self.calls.append((name, str(value)))

        async def forget_pack(conn: Any, owner: Any, pack: str) -> None:
            await record("forget_pack", pack)

        async def forget_overlay(conn: Any, owner: Any, overlay: str) -> None:
            await record("forget_overlay", overlay)

        async def search_forget_overlay(conn: Any, owner: Any, overlay: str) -> None:
            await record("search_forget_overlay", overlay)

        async def search_forget_legacy(conn: Any, owner: Any, ingest_id: Any) -> None:
            await record("search_forget_legacy", ingest_id)

        async def cited(conn: Any, owner: Any, pack: str) -> bool:
            return self.is_cited

        async def purge_judge_inputs(conn: Any, pack: str) -> None:
            await record("purge_judge_inputs", pack)

        async def purge_judge_requests(conn: Any, owner: Any, pack: str) -> None:
            await record("purge_judge_requests", pack)

        return need("reader.ingest.gc").Seams(
            forget_pack=forget_pack, forget_overlay=forget_overlay,
            search_forget_overlay=search_forget_overlay, search_forget_legacy=search_forget_legacy,
            cited=cited, purge_judge_inputs=purge_judge_inputs, purge_judge_requests=purge_judge_requests)

    def of(self, name: str) -> list[str]:
        return [v for n, v in self.calls if n == name]


def policy(keep: int = 1, later: timedelta = LATER) -> Any:
    now = datetime.now(UTC) + later
    return need("reader.ingest.gc").Policy(keep_entries=keep, age_floor_days=30, now=now)


async def pack(w: World, sha: str, version: str = R2, *, artifact: str | None = None,
               owner: str = "ivan", material_id: str | None = None) -> str:
    """A sealed pack of `sha` (import_rows, bypassing receive), its artifact directory on the blobs root."""
    artifact = artifact or sha_of(f"artifact {sha} {version}")
    files = build_artifact(spans=spans_of(ROWS), material_sha256=sha, converter_version=version)
    async with w.engine.begin() as conn:
        assert await need("reader.ingest.store").import_rows(
            conn, w.users[owner], material_id or sha[:12], artifact, json.loads(files["manifest.json"]),
            files["spans.jsonl"], json.loads(files["outline.json"]))
    (w.blobs / "ingest" / artifact).mkdir(parents=True, exist_ok=True)
    return str(need("reader.ingest.packs").pack_id(sha, version))


async def delete(w: World, sha: str) -> None:
    async with w.engine.begin() as conn:
        await conn.execute(text("UPDATE materials SET deleted_at = now() WHERE sha256 = :s"), {"s": sha})
        await need("reader.ingest.manifest").remove_material(conn, w.users["ivan"], sha)


async def collect(w: World, pid: str, pol: Any, calls: Calls) -> bool:
    async with w.engine.begin() as conn:
        gc = need("reader.ingest.gc")
        return bool(await gc.collect_pack(conn, w.users["ivan"], pid, pol, calls.seams()))


async def collect_overlay(w: World, oid: str, pol: Any, calls: Calls) -> bool:
    gc = need("reader.ingest.gc")
    async with w.engine.begin() as conn:
        return bool(await gc.collect_overlay(conn, w.users["ivan"], oid, pol, calls.seams()))


async def rows_of(w: World, pid: str) -> dict[str, Any]:
    ingest = await w.scalar("SELECT ingest_id FROM base_packs WHERE pack_id = :p", p=pid)
    return {
        "spans": await w.scalar("SELECT count(*) FROM material_spans WHERE ingest_id = :i", i=ingest),
        "outline": await w.scalar("SELECT count(*) FROM material_outline WHERE ingest_id = :i", i=ingest),
        "profiles": await w.scalar(
            "SELECT count(*) FROM pack_profiles p JOIN overlays o USING (owner_id, overlay_id)"
            " WHERE o.pack_id = :p", p=pid),
        "live_overlays": await w.scalar(
            "SELECT count(*) FROM overlays WHERE pack_id = :p AND collected_at IS NULL", p=pid),
        "collected": await w.scalar(
            "SELECT collected_at IS NOT NULL FROM base_packs WHERE pack_id = :p", p=pid),
        "ingest": await w.scalar("SELECT count(*) FROM ingests WHERE id = :i", i=ingest),
    }


async def plan_and_report(w: World, sha: str, pid: str) -> None:
    """A freshness report and a study plan that reference the pack's ingest (FK RESTRICT)."""
    owner = w.users["ivan"]
    ingest = await w.scalar("SELECT ingest_id FROM base_packs WHERE pack_id = :p", p=pid)
    material = await w.scalar("SELECT id FROM materials WHERE sha256 = :s", s=sha)
    async with w.engine.begin() as conn:
        await conn.execute(text(
            "INSERT INTO freshness_reports"
            " (owner_id, material_id, ingest_id, status, scan_inputs_sha256, sample)"
            " VALUES (:o, :m, :i, 'done', :s, '[]')"),
            {"o": owner, "m": material, "i": ingest, "s": "c" * 64})
        course = (await conn.execute(text(
            "INSERT INTO courses (owner_id, title, material_sha256, archived_at) VALUES (:o, 'c', :s, now())"
            " RETURNING id"),
            {"o": owner, "s": sha})).scalar_one()
        await conn.execute(text(
            "INSERT INTO study_plans (course_id, owner_id, plan_course_id, material_sha256, ingest_id,"
            " total_weeks, minutes_per_day, rate_centi, start_date, plan_sha256)"
            " VALUES (:c, :o, 'pc', :s, :i, 4, 30, 100, current_date, :h)"),
            {"c": course, "o": owner, "s": sha, "i": ingest, "h": "d" * 64})


async def test_a_pack_outside_the_last_entries_and_past_the_floor_is_collected(
    pg: Pg, tmp_path: Path
) -> None:
    async with world(pg, tmp_path) as w:
        sha = sha_of("collected")
        pid = await pack(w, sha)
        v = await activate(w, pid)
        profile = await w.scalar(
            "SELECT overlay_id FROM overlays WHERE pack_id = :p AND kind = 'profile'", p=pid)
        ingest = await w.scalar("SELECT ingest_id FROM base_packs WHERE pack_id = :p", p=pid)
        await plan_and_report(w, sha, pid)
        await delete(w, sha)  # entry 2 names nothing: with keep 1 the pack is outside the last entries
        tables = ("materials", "uploads")
        before = {t: await w.scalar(f"SELECT count(*) FROM {t}") for t in tables}  # noqa: S608
        calls = Calls()
        assert await collect(w, pid, policy(keep=3), calls) is False  # within N
        assert await collect(w, pid, policy(later=timedelta(0)), calls) is False  # younger than the floor
        assert await collect(w, pid, policy(), Calls(cited=True)) is False  # cited
        assert (await rows_of(w, pid))["spans"] == 3 and calls.calls == []
        assert await collect(w, pid, policy(), calls) is True
        assert await rows_of(w, pid) == {"spans": 0, "outline": 0, "profiles": 0, "live_overlays": 0,
                                         "collected": True, "ingest": 1}
        assert calls.of("forget_pack") == [pid]
        assert sorted(calls.of("forget_overlay")) == sorted([v, profile])
        assert calls.of("search_forget_overlay") == [v]
        assert calls.of("search_forget_legacy") == [str(ingest)]
        assert await w.scalar("SELECT count(*) FROM freshness_reports WHERE ingest_id = :i", i=ingest) == 1
        assert await w.scalar("SELECT count(*) FROM study_plans WHERE ingest_id = :i", i=ingest) == 1
        assert {t: await w.scalar(f"SELECT count(*) FROM {t}") for t in before} == before  # noqa: S608
        assert await collect(w, pid, policy(), calls) is False  # once


async def test_kept_when_active_pending_or_a_seq_map_target(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        active = await pack(w, sha_of("active"))
        await activate(w, active)
        assert await collect(w, active, policy(), Calls()) is False  # in the head entry

        sha = sha_of("mapped")
        legacy = await pack(w, sha)
        await activate(w, legacy)
        r3 = await pack(w, sha, R3)  # waiting: its import requested its profile
        assert await collect(w, r3, policy(), Calls()) is False  # the pack of a pending row
        async with w.engine.begin() as conn:
            ingest = await conn.execute(
                text("SELECT ingest_id FROM base_packs WHERE pack_id = :p"), {"p": legacy})
            assert await need("reader.ingest.seqmap").build(conn, w.users["ivan"], ingest.scalar(), r3) == (
                "accepted")
            await conn.execute(text("DELETE FROM pending_activations WHERE pack_id = :p"), {"p": r3})
        assert await collect(w, r3, policy(), Calls()) is False  # the target of an accepted seq map

        lone = await pack(w, sha_of("never activated"))
        assert await collect(w, lone, policy(), Calls()) is False  # its pending row keeps it
        async with w.engine.begin() as conn:
            await conn.execute(text("DELETE FROM pending_activations WHERE pack_id = :p"), {"p": lone})
        assert await collect(w, lone, policy(), Calls()) is True


async def test_overlays_desired_building_and_lone(pg: Pg, tmp_path: Path) -> None:
    manifest, overlays = need("reader.ingest.manifest"), need("reader.ingest.overlays")
    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]
        pid = await pack(w, sha_of("overlays"))
        v1 = await activate(w, pid)
        async with w.engine.begin() as conn:
            v2 = await vectors(conn, owner, pid, model="m2")
            await manifest.request_activation(conn, owner, pid, {"vectors": v2})
            assert await manifest.try_activate(conn, owner, pid) == "appended"  # entry 2: v1 left the head
            v3 = await vectors(conn, owner, pid, model="m3")
            await manifest.request_activation(conn, owner, pid, {"vectors": v3})  # desired, not activated
            inputs = {"model": "m4", "params_sha256": "0" * 64}
            building = overlays.overlay_id("vectors", pid, inputs, "test/1")
            await overlays.begin(conn, owner, building, kind="vectors", pack_id=pid, inputs=inputs,
                                 builder_version="test/1")
        calls = Calls()
        assert await collect_overlay(w, v2, policy(), calls) is False  # in the head
        assert await collect_overlay(w, v3, policy(), calls) is False  # desired by a pending row
        assert await collect_overlay(w, building, policy(), calls) is False  # building, its pack in the head
        assert await collect_overlay(w, v1, policy(keep=2), calls) is False  # within N
        assert await collect_overlay(w, v1, policy(later=timedelta(0)), calls) is False  # younger
        assert await collect_overlay(w, v1, policy(), calls) is True
        assert calls.calls == [("search_forget_overlay", v1), ("forget_overlay", v1)]
        assert await w.scalar("SELECT collected_at IS NOT NULL FROM overlays WHERE overlay_id = :i", i=v1)
        async with w.engine.begin() as conn:  # a collected id is never built again
            with pytest.raises(overlays.OverlayError) as gone:
                await vectors(conn, owner, pid)
        assert gone.value.code == "overlay_collected"

        other = await pack(w, sha_of("outside the head"))
        async with w.engine.begin() as conn:
            inputs = {"model": "m", "params_sha256": "1" * 64}
            stale = overlays.overlay_id("vectors", other, inputs, "test/1")
            await overlays.begin(conn, owner, stale, kind="vectors", pack_id=other, inputs=inputs,
                                 builder_version="test/1")
        assert await collect_overlay(w, stale, policy(), Calls()) is True  # building, pack not in the head


async def test_the_default_seams_keep_every_pack(pg: Pg, tmp_path: Path) -> None:
    gc = need("reader.ingest.gc")
    async with world(pg, tmp_path) as w:
        sha = sha_of("fail closed")
        pid = await pack(w, sha)
        await activate(w, pid)
        await delete(w, sha)
        async with w.engine.begin() as conn:  # until SRV-LESSON and SRV-KB bind `cited`, every pack is cited
            assert await gc.collect_pack(conn, w.users["ivan"], pid, policy(), gc.SEAMS[0]) is False


async def test_the_scanner_enqueues_one_job_per_candidate_once_per_slot(pg: Pg, tmp_path: Path) -> None:
    gc, overlays = need("reader.ingest.gc"), need("reader.ingest.overlays")
    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]
        a, b = sha_of("scan a"), sha_of("scan b")
        pa = await pack(w, a)
        await activate(w, pa)
        pb = await pack(w, b)
        await activate(w, pb)
        await delete(w, a)  # pa outside the last entry
        async with w.engine.begin() as conn:  # pb stays active; a stale building overlay of it is not lone
            inputs = {"model": "m9", "params_sha256": "0" * 64}
            lone = overlays.overlay_id("vectors", pb, inputs, "test/1")
            await overlays.begin(conn, owner, lone, kind="vectors", pack_id=pb, inputs=inputs,
                                 builder_version="test/1")
            await overlays.seal(conn, owner, lone)
        for _ in range(2):
            async with w.engine.begin() as conn:
                await gc.scan(conn, "2026-12-01", policy(), Calls().seams())
        keys = await w.rows("SELECT kind, args FROM job_keys WHERE kind LIKE 'ingest.gc_%' ORDER BY kind")
        assert [(k, a["pack_id"] if "pack_id" in a else a["overlay_id"]) for k, a in keys] == [
            ("ingest.gc_overlay", lone), ("ingest.gc_pack", pa)]
        async with w.engine.begin() as conn:  # the overlays of a candidate pack are collected with it
            pending, lone_overlays = await gc.candidates(conn, policy(), Calls().seams())
        assert pending == [(owner, pa)] and lone_overlays == [(owner, lone)]


async def test_the_artifact_directory_goes_only_after_commit(
    pg: Pg, tmp_path: Path, monkeypatch: Any
) -> None:
    gc = need("reader.ingest.gc")
    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]
        sha = sha_of("artifact")
        artifact = sha_of("shared artifact")
        pid = await pack(w, sha, artifact=artifact)
        await activate(w, pid)
        await delete(w, sha)
        directory = w.blobs / "ingest" / artifact
        calls = Calls()
        async def rolled_back() -> None:
            async with w.engine.begin() as conn:
                assert await gc.collect_pack(conn, owner, pid, policy(), calls.seams())
                raise RuntimeError

        with pytest.raises(RuntimeError):  # a rolled-back collection leaves the directory
            await rolled_back()
        assert await gc.remove_artifact(w.engine, w.blobs, artifact) is False and directory.is_dir()

        await add_owner(w)  # olga, another owner of the same book, holds a live ingest of it
        await pack(w, sha, artifact=artifact, owner="olga")
        assert await collect(w, pid, policy(), calls) is True
        assert await gc.remove_artifact(w.engine, w.blobs, artifact) is False and directory.is_dir()

        # the job: collect, commit, then remove when no live ingest names it (olga's pack collected by SQL)
        other = sha_of("job artifact")
        job_artifact = sha_of("job artifact dir")
        jp = await pack(w, other, artifact=job_artifact)
        await activate(w, jp)
        await delete(w, other)
        monkeypatch.setattr(gc, "SEAMS", [calls.seams()])
        monkeypatch.setattr(gc, "POLICY", [policy()])
        await w.jobs.enqueue("ingest.gc_pack", {"owner_id": str(owner), "pack_id": jp, "slot": "2026-12-01"})
        await w.work()
        assert await w.scalar("SELECT collected_at IS NOT NULL FROM base_packs WHERE pack_id = :p", p=jp)
        assert not (w.blobs / "ingest" / job_artifact).exists()

        # a leftover directory of a collected pack (a crash after the commit) goes with the next scanner
        leftover = w.blobs / "ingest" / job_artifact
        leftover.mkdir(parents=True)
        await w.jobs.enqueue("ingest.gc", {"slot": "2026-12-02"})
        await w.work()
        assert not leftover.exists() and directory.is_dir()


async def waiting(w: World) -> None:
    """Until another session waits on a pack lock."""
    ns = need("reader.ingest.packs").LOCK_NAMESPACE
    for _ in range(200):
        if await w.scalar("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = :n"
                          " AND NOT granted", n=ns):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("no session waited on a pack lock")


async def named(w: World, column: str, value: str) -> int:
    """Manifest entries naming the pack or overlay."""
    sql = {"pack": "e ->> 'pack_id' = :v",
           "overlay": "EXISTS (SELECT 1 FROM jsonb_each_text(e -> 'overlays') x WHERE x.value = :v)"}[column]
    query = ("SELECT count(*) FROM manifest_entries m, jsonb_array_elements(m.entry -> 'packs') e"
             " WHERE m.seq = (SELECT max(seq) FROM manifest_entries) AND ")
    return int(await w.scalar(query + sql, v=value))


async def test_restore_racing_gc_of_the_same_pack(pg: Pg, tmp_path: Path) -> None:
    gc, manifest = need("reader.ingest.gc"), need("reader.ingest.manifest")
    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]

        async def restore(sha: str) -> None:
            async with w.engine.begin() as conn:
                await conn.execute(
                    text("UPDATE materials SET deleted_at = NULL WHERE sha256 = :s"), {"s": sha})
                await manifest.restore_material(conn, owner, sha)

        first = sha_of("gc first")
        p1 = await pack(w, first)
        await activate(w, p1)
        await delete(w, first)
        async with w.engine.begin() as conn:  # GC holds the pack lock first
            assert await gc.collect_pack(conn, owner, p1, policy(), Calls().seams())
            task = asyncio.create_task(restore(first))
            await waiting(w)
        await task
        assert await named(w, "pack", p1) == 0
        reason = await w.scalar("SELECT reason FROM pending_activations WHERE pack_id = :p", p=p1)
        assert reason in (None, "pack_collected")

        second = sha_of("restore first")
        p2 = await pack(w, second)
        await activate(w, p2)
        await delete(w, second)
        await restore(second)  # commits first: the pack is in the head again
        assert await named(w, "pack", p2) == 1
        assert await collect(w, p2, policy(), Calls()) is False


async def test_rollback_racing_gc_overlay(pg: Pg, tmp_path: Path) -> None:
    gc, manifest = need("reader.ingest.gc"), need("reader.ingest.manifest")
    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]

        async def switched(name: str) -> tuple[str, str, int]:
            pid = await pack(w, sha_of(name))
            old = await activate(w, pid)
            at = int(await w.scalar("SELECT max(seq) FROM manifest_entries"))
            async with w.engine.begin() as conn:
                new = await vectors(conn, owner, pid, model=f"{name} 2")
                await manifest.request_activation(conn, owner, pid, {"vectors": new})
                assert await manifest.try_activate(conn, owner, pid) == "appended"
            return pid, old, at

        async def rollback(to: int) -> str:
            try:
                async with w.engine.begin() as conn:
                    await manifest.rollback(conn, owner, to)
            except manifest.ManifestError as error:
                return str(error.code)
            return "appended"

        _, old, at = await switched("gc first")
        async with w.engine.begin() as conn:  # gc_overlay holds the pack lock first
            assert await gc.collect_overlay(conn, owner, old, policy(), Calls().seams())
            task = asyncio.create_task(rollback(at))
            await waiting(w)
        assert await task == "overlay_collected"
        assert await named(w, "overlay", old) == 0

        _, old2, at2 = await switched("rollback first")
        assert await rollback(at2) == "appended"
        assert await named(w, "overlay", old2) == 1
        assert await collect_overlay(w, old2, policy(), Calls()) is False


# ---- INV-INGEST-14 ----


def test_only_the_jobs_module_reaches_the_gc_and_only_gc_deletes_pack_rows() -> None:
    reader = SRC / "reader"
    def imports_gc(node: ast.AST) -> bool:
        if not isinstance(node, ast.ImportFrom):
            return False
        named = node.module == "reader.ingest" and any(a.name == "gc" for a in node.names)
        return node.module == "reader.ingest.gc" or named

    importers = sorted(
        str(p.relative_to(SRC)) for p in reader.rglob("*.py")
        if any(imports_gc(n) for n in ast.walk(ast.parse(p.read_text(encoding="utf-8")))))
    assert importers == ["reader/ingest/jobs.py"]
    pack_tables = ("material_spans", "material_outline", "pack_profiles", "span_tags", "span_prefixes",
                   "outline_summaries")
    deleters = sorted(
        str(p.relative_to(SRC)) for p in reader.rglob("*.py")
        if "migrations" not in p.parts
        if any(f"DELETE FROM {t}" in p.read_text(encoding="utf-8") for t in pack_tables))  # noqa: S608
    assert deleters == ["reader/ingest/gc.py"]


async def test_a_delete_of_a_sealed_packs_spans_without_the_setting_is_refused(
    pg: Pg, tmp_path: Path
) -> None:
    from sqlalchemy.exc import DBAPIError

    async with world(pg, tmp_path) as w:
        pid = await pack(w, sha_of("no setting"))
        ingest = await w.scalar("SELECT ingest_id FROM base_packs WHERE pack_id = :p", p=pid)
        with pytest.raises(DBAPIError, match="immutable"):
            async with w.engine.begin() as conn:
                await conn.execute(text("DELETE FROM material_spans WHERE ingest_id = :i"), {"i": ingest})
        assert uuid.UUID(str(ingest)) and await w.scalar(
            "SELECT count(*) FROM material_spans WHERE ingest_id = :i", i=ingest) == 3
