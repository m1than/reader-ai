"""Overlays: ids, states, the profile overlay, profile_for_pack and the worker-start reconcile ([db]).

@trace FR-INGEST-36 FR-INGEST-35 INV-INGEST-11 FR-INGEST-37
"""

from __future__ import annotations

import hashlib
import textwrap
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from .conftest import CONFIG, MATERIAL_SHA, Pg, World, canonical, need, world
from .test_packs_db import imported

pytestmark = pytest.mark.anyio


def test_overlay_id_is_the_canonical_formula() -> None:
    overlays = need("reader.ingest.overlays")
    for kind, inputs in (("profile", {"license": "x", "language": "ru"}), ("tags", {"facets_sha256": "f"}),
                         ("vectors", {"model": "m", "params_sha256": "p"})):
        expected = hashlib.sha256(canonical({"kind": kind, "pack_id": "a" * 64, "inputs": inputs,
                                             "builder_version": "b/1"})).hexdigest()
        assert overlays.overlay_id(kind, "a" * 64, inputs, "b/1") == expected
    first, raised = (overlays.overlay_id("tags", "a" * 64, {}, b) for b in ("b/1", "b/2"))
    assert first != raised


async def vectors(conn: Any, owner: Any, pack: str, model: str = "m", builder: str = "test/1") -> str:
    overlays = need("reader.ingest.overlays")
    inputs = {"model": model, "params_sha256": "0" * 64}
    oid = overlays.overlay_id("vectors", pack, inputs, builder)
    await overlays.begin(conn, owner, oid, kind="vectors", pack_id=pack, inputs=inputs,
                         builder_version=builder)
    await overlays.seal(conn, owner, oid)
    return str(oid)


async def activate(w: World, pack: str) -> str:
    """The embed job's side of the contract (SRV-SEARCH paired), with a fake vector overlay."""
    manifest = need("reader.ingest.manifest")
    owner = w.users["ivan"]
    async with w.engine.begin() as conn:
        oid = await vectors(conn, owner, pack)
        await manifest.request_activation(conn, owner, pack, {"vectors": oid})
        assert await manifest.try_activate(conn, owner, pack) == "appended"
    return oid


async def test_states_and_the_collected_refusal(pg: Pg, tmp_path: Path) -> None:
    overlays = need("reader.ingest.overlays")
    async with world(pg, tmp_path) as w:
        pack = await imported(w)
        owner = w.users["ivan"]
        async with w.engine.begin() as conn:
            oid = await vectors(conn, owner, pack)
            with pytest.raises(overlays.OverlayError) as twice:
                await overlays.seal(conn, owner, oid)
            assert twice.value.code == "not_building"
        profile = await w.scalar("SELECT overlay_id FROM overlays WHERE kind = 'profile'")
        async with w.engine.connect() as conn:
            await conn.begin()
            with pytest.raises(DBAPIError):
                await conn.execute(text("UPDATE pack_profiles SET license = 'shareable'"
                                        " WHERE owner_id = :o AND overlay_id = :p"),
                                   {"o": owner, "p": profile})
            await conn.rollback()
        async with w.engine.begin() as conn:
            await conn.execute(text("UPDATE overlays SET collected_at = now() WHERE overlay_id = :i"),
                               {"i": oid})
        inputs = {"model": "m", "params_sha256": "0" * 64}
        async with w.engine.begin() as conn:
            with pytest.raises(overlays.OverlayError) as gone:
                await overlays.begin(conn, owner, oid, kind="vectors", pack_id=pack, inputs=inputs,
                                     builder_version="test/1")
            assert gone.value.code == "overlay_collected"
        async with w.engine.begin() as conn:
            assert await vectors(conn, owner, pack, builder="test/2") != oid  # a raised builder_version
        assert await w.scalar("SELECT count(*) FROM overlays WHERE kind = 'vectors'") == 2


async def test_profile_for_pack_and_a_role_change(pg: Pg, tmp_path: Path) -> None:
    packs = need("reader.ingest.packs")
    overlays = need("reader.ingest.overlays")
    manifest = need("reader.ingest.manifest")
    async with world(pg, tmp_path) as w:
        pack = await imported(w)
        owner = w.users["ivan"]
        rule = {"license": "personal-use", "language": "ru", "role": None, "reliability": None, "as_of": None}
        async with w.engine.connect() as conn:
            assert await packs.profile_for_pack(conn, owner, pack) == rule  # the pending (import's) one
        vec = await activate(w, pack)
        async with w.engine.begin() as conn:
            assert await packs.profile_for_pack(conn, owner, pack) == rule  # the head's
            other = await overlays.build_profile(conn, owner, pack, {**rule, "role": "primary"})
            await manifest.request_activation(conn, owner, pack, {"profile": other})
            assert await packs.profile_for_pack(conn, owner, pack) == rule  # head wins over pending
            assert await manifest.try_activate(conn, owner, pack) == "appended"
            assert (await packs.profile_for_pack(conn, owner, pack))["role"] == "primary"
            view = await manifest.active_view(conn, owner)
        assert view.entry["packs"] == [{"pack_id": pack, "overlays": {"profile": other, "vectors": vec}}]
        assert view.seq == 2


def write_config(w: World, tmp: Path, language: str | None, *, broken: bool = False) -> Path:
    path = tmp / "reader.yaml"
    body = textwrap.dedent(CONFIG.format(sha=MATERIAL_SHA, other="b" * 64))
    if language is not None:
        body = body.replace("license: personal-use, language: ru",
                            f"license: personal-use, language: {language}")
    else:
        body = body.replace(f'sha256: "{MATERIAL_SHA}"', f'sha256: "{"c" * 64}"')
    path.write_text("::: not yaml" if broken else body, encoding="utf-8")
    return path


async def test_reconcile_reuses_overlays_and_keeps_vectors(pg: Pg, tmp_path: Path) -> None:
    from reader.config import load_config

    packs = need("reader.ingest.packs")
    async with world(pg, tmp_path) as w:
        pack = await imported(w)
        vec = await activate(w, pack)
        owner = w.users["ivan"]
        calls: list[str] = []

        async def ensure(conn: Any, owner_id: Any, sha: str) -> None:
            del conn
            assert owner_id == owner
            calls.append(sha)

        async def run(language: str) -> Any:
            config = load_config(write_config(w, tmp_path, language))
            await packs.reconcile(w.engine, owner, config.materials, ensure=ensure)
            async with w.engine.connect() as conn:
                return await need("reader.ingest.manifest").active_view(conn, owner)

        first = await run("ru")  # unchanged: nothing appended, no ensure
        assert first.seq == 1 and calls == []  # the embed side's entry only
        a = first.entry["packs"][0]["overlays"]["profile"]
        b_view = await run("en")
        b = b_view.entry["packs"][0]["overlays"]["profile"]
        assert b != a and b_view.seq == 2 and calls == [MATERIAL_SHA]
        assert b_view.entry["packs"][0]["overlays"]["vectors"] == vec
        assert (await run("ru")).entry["packs"][0]["overlays"]["profile"] == a
        last = await run("en")
        assert last.entry["packs"][0]["overlays"]["profile"] == b and last.seq == 4
        assert await w.scalar("SELECT count(*) FROM overlays WHERE kind = 'profile'") == 2  # A and B, reused
        assert (await run("en")).seq == 4  # no_change

        for config in (None, load_config(write_config(w, tmp_path, None))):
            # unreadable reader.yaml, or no entry for the material
            materials = None if config is None else config.materials
            await packs.reconcile(w.engine, owner, materials, ensure=ensure)
        assert await w.scalar("SELECT max(seq) FROM manifest_entries") == 4


async def test_reconcile_never_deadlocks_with_an_activation(pg: Pg, tmp_path: Path) -> None:
    """Code review of unit 2, blocking 4: another session holds the second pack's lock and activates it, while
    the reconcile appends for the first pack (both profiles change, so the first-locked pack appends)."""
    import asyncio
    import types

    packs, manifest = need("reader.ingest.packs"), need("reader.ingest.manifest")
    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]
        a, b = await imported(w), await imported(w, "b" * 64)
        await activate(w, a)
        await activate(w, b)
        lo, hi = sorted([a, b])
        async with w.engine.connect() as conn:
            start = await manifest.active_view(conn, owner)
        profiles = {e["pack_id"]: e["overlays"]["profile"] for e in start.entry["packs"]}
        materials = [  # each material's language flips (reader.yaml: synthetic ru, other en)
            types.SimpleNamespace(sha256=MATERIAL_SHA, license="personal-use", language="en"),
            types.SimpleNamespace(sha256="b" * 64, license="shareable", language="ru"),
        ]
        other = await w.engine.connect()
        await other.begin()
        await packs.lock_packs(other, [hi])
        v2 = await vectors(other, owner, hi, model="m2")
        await manifest.request_activation(other, owner, hi, {"vectors": v2})
        before = await w.scalar("SELECT max(seq) FROM manifest_entries")
        task = asyncio.create_task(packs.reconcile(w.engine, owner, materials))
        await asyncio.sleep(1.0)  # the reconcile has committed lo and waits on hi's lock
        assert await w.scalar("SELECT max(seq) FROM manifest_entries") == before + 1  # lo appended first
        assert await manifest.try_activate(other, owner, hi) == "appended"
        await other.commit()
        await other.close()
        await asyncio.wait_for(task, 20)  # both complete
        async with w.engine.connect() as conn:
            view = await manifest.active_view(conn, owner)
        heads = {e["pack_id"]: e["overlays"] for e in view.entry["packs"]}
        assert heads[hi]["vectors"] == v2
        assert heads[lo]["profile"] != profiles[lo] and heads[hi]["profile"] != profiles[hi]


async def test_start_reconcile_and_shareable(pg: Pg, tmp_path: Path, monkeypatch: Any) -> None:
    packs, manifest, jobs = (need(f"reader.ingest.{m}") for m in ("packs", "manifest", "jobs"))
    assert not packs.shareable(None) and not packs.shareable({"license": None})
    assert not packs.shareable({"license": "personal-use"}) and packs.shareable({"license": "shareable"})
    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]
        pack = await imported(w)
        await activate(w, pack)
        b = await imported(w, "b" * 64)

        async def lost(*_: Any, **__: Any) -> None:
            raise manifest.ManifestError("manifest_conflict")

        with monkeypatch.context() as patch:
            patch.setattr(manifest, "_insert", lost)
            async with w.engine.begin() as conn:
                assert await manifest.try_activate(conn, owner, b) == "manifest_conflict"
        write_config(w, tmp_path, "en")  # READER_CONFIG names this file
        await jobs.start_reconcile(w.jobs)
        async with w.engine.connect() as conn:
            view = await manifest.active_view(conn, owner)
            profile = await packs.profile_for_pack(conn, owner, pack)
        assert profile is not None and profile["language"] == "en"
        assert {e["pack_id"] for e in view.entry["packs"]} == {pack, b}  # the stored row was swept
        assert await w.scalar("SELECT count(*) FROM pending_activations") == 0


async def test_worker_start_enqueues_the_reconcile(pg: Pg, tmp_path: Path, caplog: Any) -> None:
    """FR-INGEST-40's merge gate: the start hook only enqueues; the reconcile job reconciles and sweeps."""
    import logging

    from reader.jobs import worker as jobs_worker

    jobs = need("reader.ingest.jobs")
    names = {k.name for k in jobs.JOB_KINDS}
    assert names == {"ingest.import", "ingest.seqmap", "ingest.reanchor", "ingest.gc", "ingest.gc_pack",
                     "ingest.gc_overlay", "ingest.purge", "ingest.reconcile"}
    caplog.set_level(logging.INFO)
    async with world(pg, tmp_path) as w:
        pack = await imported(w)  # its pending {profile} row waits for an activation
        assert await w.scalar("SELECT count(*) FROM pending_activations WHERE pack_id = :p", p=pack) == 1
        await jobs.on_worker_start(w.jobs)
        assert await w.scalar("SELECT count(*) FROM job_keys WHERE kind = 'ingest.reconcile'") == 1
        assert await w.scalar("SELECT count(*) FROM manifest_entries") == 0  # the hook itself wrote nothing
        await w.work()
        assert await w.scalar("SELECT count(*) FROM pending_activations") == 0
        assert await w.scalar("SELECT count(*) FROM manifest_entries") == 1
        previous, jobs_worker._RUNNING[0] = jobs_worker._RUNNING[0], None  # the api: no worker runs
        try:
            assert await jobs.enqueue_known("ingest.seqmap", {}) is False
        finally:
            jobs_worker._RUNNING[0] = previous
        assert "ingest enqueue_skipped no_worker" in caplog.text


async def test_span_tags_cannot_move_into_a_sealed_overlay(pg: Pg, tmp_path: Path) -> None:
    """Code review of unit 2, blocking 1: the overlay-row guard checks the new row's overlay too."""
    overlays = need("reader.ingest.overlays")
    async with world(pg, tmp_path) as w:
        pack, owner = await imported(w), w.users["ivan"]
        ids = []
        async with w.engine.begin() as conn:
            for version in ("t/1", "t/2"):
                oid = overlays.overlay_id("tags", pack, {}, version)
                await overlays.begin(conn, owner, oid, kind="tags", pack_id=pack, inputs={},
                                     builder_version=version)
                ids.append(oid)
            await conn.execute(text(
                "INSERT INTO span_tags (owner_id, overlay_id, pack_id, locator, facet, value, by, confidence,"
                " tagger_version) VALUES (:o, :i, :p, 'spn:x', 'license', 'shareable', 'rule', 1, 't/1')"),
                {"o": owner, "i": ids[0], "p": pack})
            await overlays.seal(conn, owner, ids[1])
        async with w.engine.connect() as conn:
            await conn.begin()
            with pytest.raises(DBAPIError, match="immutable"):
                await conn.execute(text("UPDATE span_tags SET overlay_id = :s WHERE overlay_id = :b"),
                                   {"s": ids[1], "b": ids[0]})
            await conn.rollback()


async def test_tag_writes_are_checked_against_the_facets_file(pg: Pg, tmp_path: Path) -> None:
    """FR-INGEST-37: unknown facet or value and disabled facets are refused with nothing written."""
    overlays, facets = need("reader.ingest.overlays"), need("reader.ingest.facets")
    from ruamel.yaml import YAML

    data = YAML(typ="safe").load(facets.FACETS_FILE.read_text(encoding="utf-8"))
    data["facets"]["form"].update(enabled=True, consumer="profile_for_pack")
    path = tmp_path / "facets.yaml"
    with path.open("w", encoding="utf-8") as out:
        YAML(typ="safe").dump(data, out)
    enabled = facets.load(path)
    async with world(pg, tmp_path) as w:
        pack, owner = await imported(w), w.users["ivan"]
        loc = await w.scalar("SELECT locator FROM material_spans ORDER BY seq LIMIT 1")
        oid = overlays.overlay_id("tags", pack, {}, "t/1")
        async with w.engine.begin() as conn:
            await overlays.begin(conn, owner, oid, kind="tags", pack_id=pack, inputs={},
                                 builder_version="t/1")
        for loaded, facet, value, code in ((facets.load(), "form", "code", "facet_disabled"),
                                           (enabled, "form", "poem", "tag_value_unknown"),
                                           (enabled, "mood", "calm", "tag_value_unknown")):
            async with w.engine.begin() as conn:
                with pytest.raises(facets.FacetsError) as refused:
                    await overlays.write_tags(conn, owner, oid, pack, loaded, [
                        (loc, "form", "prose", "rule", 1.0, "t/1"), (loc, facet, value, "rule", 1.0, "t/1")])
            assert refused.value.code == code
            assert await w.scalar("SELECT count(*) FROM span_tags") == 0
        async with w.engine.begin() as conn:
            tag = (loc, "form", "code", "rule", 1.0, "t/1")
            await overlays.write_tags(conn, owner, oid, pack, enabled, [tag])
        assert await w.rows("SELECT facet, value, by, confidence, tagger_version FROM span_tags") == [
            ("form", "code", "rule", 1.0, "t/1")]


async def test_profile_for_pack_reads_the_pending_profile(pg: Pg, tmp_path: Path) -> None:
    """Code review of unit 2, blocking 7: a pack outside the head whose pending row names a second profile."""
    packs, overlays, manifest = (need(f"reader.ingest.{m}") for m in ("packs", "overlays", "manifest"))
    async with world(pg, tmp_path) as w:
        pack, owner = await imported(w), w.users["ivan"]
        async with w.engine.begin() as conn:
            rule = await packs.profile_for_pack(conn, owner, pack)
            second = await overlays.build_profile(conn, owner, pack, {**rule, "role": "secondary"})
            await manifest.request_activation(conn, owner, pack, {"profile": second})
            assert (await packs.profile_for_pack(conn, owner, pack))["role"] == "secondary"
