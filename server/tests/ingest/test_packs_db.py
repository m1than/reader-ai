"""Base packs, their triggers, the import's seal and migration ingest_0004_packs (SRV-INGEST [db]).

@trace FR-INGEST-35 FR-INGEST-34 FR-INGEST-13 FR-INGEST-14 INV-INGEST-11 FR-INGEST-37
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
PROFILE_SQL = "SELECT overlay_id FROM overlays WHERE kind = 'profile' AND state = 'sealed'"


async def imported(w: World, sha: str = MATERIAL_SHA) -> str:
    receive = need("reader.ingest.receive")
    ingest = need("reader.ingest")
    stream = io.BytesIO(tar_of(build_artifact(material_sha256=sha, converter_version=R2)))
    current, ingest.CONVERTER_VERSION = ingest.CONVERTER_VERSION, R2  # the legacy pack these tests start from
    try:
        await receive.receive(stream, config=w.config, engine=w.engine, jobs=w.jobs, blobs_root=w.blobs)
    finally:
        ingest.CONVERTER_VERSION = current
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
            texts = [s[2] for s in on_page]
            assert [s[3] for s in on_page] == locator.page_locators(MATERIAL_SHA, page, texts)
        assert all(s[4] == s[5] == locator.quote_sha256(s[2]) for s in spans)
        # FR-INGEST-13 revision 3 first part: sealed profile overlay, a pending row desiring it, no entry
        profile = await w.scalar(PROFILE_SQL)
        assert profile is not None
        assert await w.scalar("SELECT count(*) FROM overlays") == 1
        pending = await w.rows("SELECT pack_id, overlays FROM pending_activations")
        assert [(p[0], p[1]) for p in pending] == [(pack, {"profile": profile})]
        assert await w.scalar("SELECT count(*) FROM manifest_entries") == 0
        assert await w.scalar("SELECT count(*) FROM span_tags") == 0


async def test_an_invalid_facets_file_fails_the_import(pg: Pg, tmp_path: Path, monkeypatch: Any) -> None:
    """FR-INGEST-37: the committed file writes no span_tags row (above); a broken one fails the import."""
    facets = need("reader.ingest.facets")
    broken = tmp_path / "facets.yaml"
    broken.write_text("version: 1\nfacets: {}\n", encoding="utf-8")
    monkeypatch.setattr(facets, "FACETS_FILE", broken)
    async with world(pg, tmp_path) as w:
        await imported(w)
        row = await w.rows("SELECT outcome, error_code FROM job_keys WHERE kind = 'ingest.import'")
        assert [tuple(r) for r in row] == [("dead", "facets_invalid")]
        assert await w.scalar("SELECT count(*) FROM base_packs") == 0


async def test_triggers_keep_sealed_packs_immutable(pg: Pg, tmp_path: Path) -> None:
    async with world(pg, tmp_path) as w:
        pack = await imported(w)
        ingest = await w.scalar("SELECT ingest_id FROM base_packs")
        await refused(w, "UPDATE base_packs SET parse_config = '{}'::jsonb WHERE pack_id = :p", p=pack)
        await refused(w, "UPDATE base_packs SET state = 'building' WHERE pack_id = :p", p=pack)
        await refused(w, "INSERT INTO material_spans (ingest_id, owner_id, seq, page, kind, text, x0, y0, x1,"
                      " y1, locator, quote_sha256) SELECT ingest_id, owner_id, 99, 1, 'text', 'x', 0, 0, 1,"
                      " 1,"
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
            await conn.execute(text("UPDATE base_packs SET collected_at = now() WHERE pack_id = :p"),
                               {"p": pack})
        await refused(w, "UPDATE base_packs SET collected_at = now() WHERE pack_id = :p", p=pack)
        async with w.engine.begin() as conn:
            await conn.execute(text("UPDATE base_packs SET purged_at = now() WHERE pack_id = :p"),
                               {"p": pack})
        await refused(w, "UPDATE base_packs SET purged_at = now() WHERE pack_id = :p", p=pack)
        assert await w.scalar("SELECT count(*) FROM ingests") == 1


async def test_an_update_cannot_move_a_row_into_a_sealed_pack(pg: Pg, tmp_path: Path) -> None:
    """Code review of unit 2, blocking 1: the guard checks the new row's pack too."""
    async with world(pg, tmp_path) as w:
        await imported(w)
        owner, sealed = w.users["ivan"], await w.scalar("SELECT ingest_id FROM base_packs")
        async with w.engine.begin() as conn:
            m = (await conn.execute(text(
                "INSERT INTO materials (owner_id, sha256, config_id, page_count) VALUES (:o, :s, 'x', 1)"
                " RETURNING id"), {"o": owner, "s": "c" * 64})).scalar_one()
            bare = (await conn.execute(text(
                "INSERT INTO ingests (owner_id, material_id, converter_version, artifact_id, span_count,"
                " outline_count, outline_source, pages_without_text)"
                " VALUES (:o, :m, 'v', :a, 1, 0, 'headings', '{}') RETURNING id"),
                {"o": owner, "m": m, "a": "d" * 64})).scalar_one()
            await conn.execute(text(
                "INSERT INTO material_spans (ingest_id, owner_id, seq, page, kind, text, x0, y0, x1, y1)"
                " VALUES (:i, :o, 9999, 1, 'text', 'injected', 0, 0, 1, 1)"), {"i": bare, "o": owner})
        async with w.engine.connect() as conn:
            await conn.begin()
            with pytest.raises(DBAPIError, match="immutable"):
                await conn.execute(text("UPDATE material_spans SET ingest_id = :s WHERE ingest_id = :i"),
                                   {"s": sealed, "i": bare})
            await conn.rollback()
        assert await w.scalar("SELECT count(*) FROM material_spans WHERE locator IS NULL AND ingest_id = :i",
                              i=sealed) == 0


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
                "INSERT INTO materials (owner_id, sha256, config_id, page_count)"
                " VALUES (:o, :s, 'synthetic', 2) RETURNING id"),
                {"o": owner, "s": MATERIAL_SHA})).scalar_one()
            gone = (await conn.execute(text(  # a soft-deleted material: registered, not in the entry
                "INSERT INTO materials (owner_id, sha256, config_id, page_count, deleted_at)"
                " VALUES (:o, :s, 'gone', 1, now()) RETURNING id"), {"o": owner, "s": "c" * 64})).scalar_one()
            await conn.execute(text(
                "INSERT INTO ingests (owner_id, material_id, converter_version, artifact_id, span_count,"
                " outline_count, outline_source, pages_without_text) VALUES (:o, :m, :v, :a, 0, 0,"
                " 'headings', '{}')"), {"o": owner, "m": gone, "v": R2, "a": "d" * 64})
            ingest = (await conn.execute(text(
                "INSERT INTO ingests (owner_id, material_id, converter_version, artifact_id, span_count,"
                " outline_count, outline_source, pages_without_text) VALUES (:o, :m, :v, :a, :n, 0,"
                " 'headings', '{}') RETURNING id"),
                {"o": owner, "m": material, "v": R2, "a": "d" * 64, "n": len(spans)})).scalar_one()
            for s in spans:
                await conn.execute(text(
                    "INSERT INTO material_spans (ingest_id, owner_id, seq, page, kind, level, decoded, text,"
                    " x0, y0, x1, y1) VALUES (:i, :o, :seq, :page, :kind, :level, :decoded, :text,"
                    " 0, 0, 1, 1)"),
                    {"i": ingest, "o": owner, **{k: s[k] for k in ("seq", "page", "kind", "level", "decoded",
                                                                   "text")}})
        async with w.engine.connect() as conn:
            await conn.run_sync(up)
            await conn.commit()
        gone_pack = expected_pack_id("c" * 64, R2)
        pending = await w.rows("SELECT pack_id, overlays FROM pending_activations")
        gone_profile = await w.scalar(PROFILE_SQL + " AND pack_id = :p", p=gone_pack)
        assert [tuple(r) for r in pending] == [(gone_pack, {"profile": gone_profile})]
        async with w.engine.begin() as conn:  # the deleted material leaves the rest of this test's view
            await conn.execute(text("DELETE FROM pending_activations"))
        rows = await w.rows("SELECT pack_id, state, ingest_id FROM base_packs WHERE pack_id <> :g",
                            g=gone_pack)
        assert [tuple(r) for r in rows] == [(expected_pack_id(MATERIAL_SHA, R2), "sealed", ingest)]
        stored = await w.rows("SELECT page, text, locator, quote_sha256 FROM material_spans"
                              " ORDER BY page, seq")
        for page in {s[0] for s in stored}:
            on_page = [s for s in stored if s[0] == page]
            texts = [s[1] for s in on_page]
            assert [s[2] for s in on_page] == locator.page_locators(MATERIAL_SHA, page, texts)
        assert all(s[3] == locator.quote_sha256(s[1]) for s in stored)
        entries = await w.rows("SELECT seq, parent_seq, entry FROM manifest_entries")
        profile = await w.scalar(PROFILE_SQL + " AND pack_id = :p", p=rows[0][0])
        assert [tuple(e) for e in entries] == [(1, 0, {"packs": [{"pack_id": rows[0][0],
                                                                    "overlays": {"profile": profile}}],
                                                         "registry_view": None})]
        assert await w.scalar("SELECT count(*) FROM pending_activations") == 0
        missing = "SELECT count(*) FROM material_spans WHERE locator IS NULL OR quote_sha256 IS NULL"
        assert await w.scalar(missing) == 0
        async with w.engine.begin() as conn:  # a pack whose spans lack a locator cannot seal
            m = (await conn.execute(text(
                "INSERT INTO materials (owner_id, sha256, config_id, page_count) VALUES (:o, :s, 'other', 1)"
                " RETURNING id"), {"o": owner, "s": "b" * 64})).scalar_one()
            i = (await conn.execute(text(
                "INSERT INTO ingests (owner_id, material_id, converter_version, artifact_id, span_count,"
                " outline_count, outline_source, pages_without_text)"
                " VALUES (:o, :m, :v, :a, 1, 0, 'headings', '{}') RETURNING id"),
                {"o": owner, "m": m, "v": R2, "a": "d" * 64})).scalar_one()
            await conn.execute(text(
                "INSERT INTO material_spans (ingest_id, owner_id, seq, page, kind, text, x0, y0, x1, y1)"
                " VALUES (:i, :o, 0, 1, 'text', 'x', 0, 0, 1, 1)"), {"i": i, "o": owner})
            await conn.execute(text(
                "INSERT INTO base_packs (owner_id, pack_id, material_sha256, ingest_id, parse_config, state)"
                " VALUES (:o, :p, :s, :i, '{}'::jsonb, 'building')"),
                {"o": owner, "p": "b" * 64, "s": "b" * 64, "i": i})
        await refused(w, "UPDATE base_packs SET state = 'sealed', sealed_at = now() WHERE pack_id = :p",
                      p="b" * 64)
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
