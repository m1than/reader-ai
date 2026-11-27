"""Base packs, their triggers, the import's seal and migration ingest_0004_packs (SRV-INGEST [db]).

@trace FR-INGEST-35 FR-INGEST-34 FR-INGEST-13 FR-INGEST-14 INV-INGEST-11
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from .conftest import MATERIAL_SHA, Pg, World, build_artifact, canonical, need, tar_of, world

pytestmark = pytest.mark.anyio
R2 = "docling-2.151.1+r2"


async def imported(w: World, sha: str = MATERIAL_SHA) -> str:
    receive = need("reader.ingest.receive")
    stream = io.BytesIO(tar_of(build_artifact(material_sha256=sha)))
    await receive.receive(stream, config=w.config, engine=w.engine, jobs=w.jobs, blobs_root=w.blobs)
    await w.work()
    return str(await w.scalar("SELECT pack_id FROM base_packs WHERE material_sha256 = :s", s=sha))


def expected_pack_id(sha: str, version: str) -> str:
    config = need("reader.ingest").PARSE_CONFIGS[version]
    return hashlib.sha256(canonical({"material_sha256": sha, "parse_config": dict(config)})).hexdigest()


async def refused(w: World, sql: str, *, pack_delete: bool = False, **params: Any) -> None:
    async with w.engine.connect() as conn:
        await conn.begin()
        if pack_delete:
            await conn.execute(text("SET LOCAL reader.pack_delete = 'on'"))
        with pytest.raises(DBAPIError):
            await conn.execute(text(sql), params)
        await conn.rollback()


async def test_pack_id_and_the_import_seal(pg: Pg, tmp_path: Path) -> None:
    packs = need("reader.ingest.packs")
    locator = need("reader.ingest.locator")
    async with world(pg, tmp_path) as w:
        pack = await imported(w)
        assert pack == expected_pack_id(MATERIAL_SHA, R2) == packs.pack_id(MATERIAL_SHA, R2)
        row = (await w.rows("SELECT state, sealed_at IS NOT NULL, owner_id, parse_config FROM base_packs"))[0]
        assert (row[0], row[1], row[2]) == ("sealed", True, w.users["ivan"])
        assert row[3] == dict(need("reader.ingest").PARSE_CONFIGS[R2])
        spans = await w.rows("SELECT page, seq, text, locator, quote_sha256,"
                             " encode(sha256(convert_to(text, 'UTF8')), 'hex') FROM material_spans"
                             " ORDER BY page, seq")
        for page in {s[0] for s in spans}:
            on_page = [s for s in spans if s[0] == page]
            assert [s[3] for s in on_page] == locator.page_locators(MATERIAL_SHA, page, [s[2] for s in on_page])
        assert all(s[4] == s[5] == locator.quote_sha256(s[2]) for s in spans)
        # FR-INGEST-13 revision 3 first part: sealed profile overlay, a pending row desiring it, no entry
        profile = await w.scalar("SELECT overlay_id FROM overlays WHERE kind = 'profile' AND state = 'sealed'")
        assert profile is not None
        assert await w.scalar("SELECT count(*) FROM overlays") == 1
        pending = await w.rows("SELECT pack_id, overlays FROM pending_activations")
        assert [(p[0], p[1]) for p in pending] == [(pack, {"profile": profile})]
        assert await w.scalar("SELECT count(*) FROM manifest_entries") == 0
        assert await w.scalar("SELECT count(*) FROM span_tags") == 0


async def test_triggers_keep_sealed_packs_immutable(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        pack = await imported(w)
        ingest = await w.scalar("SELECT ingest_id FROM base_packs")
        await refused(w, "UPDATE base_packs SET parse_config = '{}'::jsonb WHERE pack_id = :p", p=pack)
        await refused(w, "UPDATE base_packs SET state = 'building' WHERE pack_id = :p", p=pack)
        await refused(w, "INSERT INTO material_spans (ingest_id, owner_id, seq, page, kind, text, x0, y0, x1, y1,"
                      " locator, quote_sha256) SELECT ingest_id, owner_id, 99, 1, 'text', 'x', 0, 0, 1, 1,"
                      " 'spn:' || :s || ':p1:900-901', repeat('0', 64) FROM base_packs", s=MATERIAL_SHA)
        await refused(w, "UPDATE material_spans SET text = 'y' WHERE ingest_id = :i", i=ingest)
        await refused(w, "DELETE FROM material_spans WHERE ingest_id = :i", i=ingest)
        await refused(w, "DELETE FROM material_outline WHERE ingest_id = :i", i=ingest)
        await refused(w, "DELETE FROM base_packs WHERE pack_id = :p", pack_delete=True, p=pack)
        await refused(w, "DELETE FROM ingests WHERE id = :i", pack_delete=True, i=ingest)
        await refused(w, "UPDATE ingests SET span_count = 0 WHERE id = :i", i=ingest)
        async with w.engine.begin() as conn:  # the deleters' path: allowed with the setting
            await conn.execute(text("SET LOCAL reader.pack_delete = 'on'"))
            await conn.execute(text("DELETE FROM material_spans WHERE ingest_id = :i"), {"i": ingest})
            await conn.execute(text("UPDATE base_packs SET collected_at = now() WHERE pack_id = :p"), {"p": pack})
        await refused(w, "UPDATE base_packs SET collected_at = now() WHERE pack_id = :p", p=pack)
        async with w.engine.begin() as conn:
            await conn.execute(text("UPDATE base_packs SET purged_at = now() WHERE pack_id = :p"), {"p": pack})
        await refused(w, "UPDATE base_packs SET purged_at = now() WHERE pack_id = :p", p=pack)
        assert await w.scalar("SELECT count(*) FROM ingests") == 1


async def test_building_pack_is_not_in_the_active_view(pg: Pg, tmp_path: Path) -> None:
    manifest = need("reader.ingest.manifest")
    async with world(pg, tmp_path) as w:
        pack = await imported(w)
        owner = w.users["ivan"]
        async with w.engine.begin() as conn:
            await conn.execute(text("UPDATE ingests SET material_id = material_id WHERE false"))
            with pytest.raises(manifest.ManifestError) as refusal:
                await manifest.append(conn, owner, 0, {"packs": [{"pack_id": "c" * 64, "overlays": {}}],
                                                       "registry_view": None}, "test")
            assert refusal.value.code == "not_sealed"
        async with w.engine.connect() as conn:
            view = await manifest.active_view(conn, owner)
        assert view.entry["packs"] == [] and pack


async def test_migration_registers_the_legacy_ingest(pg: Pg, tmp_path: Path) -> None:
    from alembic import command

    from reader.db import alembic_config

    locator = need("reader.ingest.locator")
    async with world(pg, tmp_path) as w:
        def down(connection: Any) -> None:
            config = alembic_config()
            config.attributes["connection"] = connection
            command.downgrade(config, "ingest_0003")

        def up(connection: Any) -> None:
            config = alembic_config()
            config.attributes["connection"] = connection
            command.upgrade(config, "heads")

        async with w.engine.connect() as conn:
            await conn.run_sync(down)
            await conn.commit()
        cols = await w.scalar("SELECT count(*) FROM information_schema.columns"
                              " WHERE table_name = 'material_spans' AND column_name = 'locator'")
        assert cols == 0 and await w.scalar("SELECT to_regclass('base_packs')") is None
        files = build_artifact()
        spans = [json.loads(x) for x in files["spans.jsonl"].splitlines()]
        owner = w.users["ivan"]
        async with w.engine.begin() as conn:
            material = (await conn.execute(text(
                "INSERT INTO materials (owner_id, sha256, config_id, page_count) VALUES (:o, :s, 'synthetic', 2)"
                " RETURNING id"), {"o": owner, "s": MATERIAL_SHA})).scalar_one()
            ingest = (await conn.execute(text(
                "INSERT INTO ingests (owner_id, material_id, converter_version, artifact_id, span_count,"
                " outline_count, outline_source, pages_without_text) VALUES (:o, :m, :v, :a, :n, 0,"
                " 'headings', '{}') RETURNING id"),
                {"o": owner, "m": material, "v": R2, "a": "d" * 64, "n": len(spans)})).scalar_one()
            for s in spans:
                await conn.execute(text(
                    "INSERT INTO material_spans (ingest_id, owner_id, seq, page, kind, level, decoded, text,"
                    " x0, y0, x1, y1) VALUES (:i, :o, :seq, :page, :kind, :level, :decoded, :text, 0, 0, 1, 1)"),
                    {"i": ingest, "o": owner, **{k: s[k] for k in ("seq", "page", "kind", "level", "decoded",
                                                                   "text")}})
        async with w.engine.connect() as conn:
            await conn.run_sync(up)
            await conn.commit()
        rows = await w.rows("SELECT pack_id, state, ingest_id FROM base_packs")
        assert [tuple(r) for r in rows] == [(expected_pack_id(MATERIAL_SHA, R2), "sealed", ingest)]
        stored = await w.rows("SELECT page, text, locator, quote_sha256 FROM material_spans ORDER BY page, seq")
        for page in {s[0] for s in stored}:
            on_page = [s for s in stored if s[0] == page]
            assert [s[2] for s in on_page] == locator.page_locators(MATERIAL_SHA, page, [s[1] for s in on_page])
        assert all(s[3] == locator.quote_sha256(s[1]) for s in stored)
        entries = await w.rows("SELECT seq, parent_seq, entry FROM manifest_entries")
        profile = await w.scalar("SELECT overlay_id FROM overlays WHERE kind = 'profile' AND state = 'sealed'")
        assert [tuple(e) for e in entries] == [(1, 0, {"packs": [{"pack_id": rows[0][0],
                                                                    "overlays": {"profile": profile}}],
                                                         "registry_view": None})]
        assert await w.scalar("SELECT count(*) FROM pending_activations") == 0
        nulls = await w.scalar("SELECT count(*) FROM information_schema.columns WHERE table_name = 'material_spans'"
                               " AND column_name IN ('locator', 'quote_sha256') AND is_nullable = 'NO'")
        assert nulls == 2
        unique = await w.scalar("SELECT count(*) FROM pg_indexes WHERE tablename = 'ingests'"
                                " AND indexdef LIKE '%UNIQUE%(material_id, converter_version)%'")
        assert unique == 1


async def test_downgrade_refuses_two_ingests_of_one_material(pg: Pg, tmp_path: Path) -> None:
    from alembic import command

    from reader.db import alembic_config

    store = need("reader.ingest.store")
    async with world(pg, tmp_path) as w:
        await imported(w)
        files = build_artifact(converter_version="docling-2.151.1+r3")
        manifest = json.loads(files["manifest.json"])
        async with w.engine.begin() as conn:
            assert await store.import_rows(conn, w.users["ivan"], "synthetic", "e" * 64, manifest,
                                           files["spans.jsonl"], json.loads(files["outline.json"]))
        assert await w.scalar("SELECT count(DISTINCT pack_id) FROM base_packs") == 2

        def down(connection: Any) -> None:
            config = alembic_config()
            config.attributes["connection"] = connection
            command.downgrade(config, "ingest_0003")

        async with w.engine.connect() as conn:
            with pytest.raises(Exception, match="more than one ingest"):
                await conn.run_sync(down)
            await conn.rollback()
        assert await w.scalar("SELECT to_regclass('base_packs')") is not None
