"""Overlays: ids, states, the profile overlay, profile_for_pack and the worker-start reconcile ([db]).

@trace FR-INGEST-36 FR-INGEST-35 INV-INGEST-11
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
    """Code review of unit 2, blocking 4: another session holds the second pack's lock and activates it."""
    import asyncio

    from reader.config import load_config

    packs, manifest = need("reader.ingest.packs"), need("reader.ingest.manifest")
    async with world(pg, tmp_path) as w:
        owner = w.users["ivan"]
        a, b = await imported(w), await imported(w, "b" * 64)
        await activate(w, a)
        await activate(w, b)
        hi = max(a, b)
        config = load_config(write_config(w, tmp_path, "en"))  # the profiles change
        other = await w.engine.connect()
        await other.begin()
        await packs.lock_packs(other, [hi])
        v2 = await vectors(other, owner, hi, model="m2")
        await manifest.request_activation(other, owner, hi, {"vectors": v2})
        task = asyncio.create_task(packs.reconcile(w.engine, owner, config.materials))
        await asyncio.sleep(1.0)  # the reconcile has committed the first pack and waits on hi's lock
        assert await manifest.try_activate(other, owner, hi) == "appended"
        await other.commit()
        await other.close()
        await asyncio.wait_for(task, 20)
        async with w.engine.connect() as conn:
            view = await manifest.active_view(conn, owner)
        assert {e["pack_id"]: e["overlays"]["vectors"] for e in view.entry["packs"]}[hi] == v2


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
