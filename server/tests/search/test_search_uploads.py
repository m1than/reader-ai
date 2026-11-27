"""FR-SEARCH-10 (ADR-0039 revision): an uploaded material's full-text configuration comes from its upload's
language through the profile overlay SRV-INGEST builds (FR-INGEST-23, -36); search reads no reader.yaml.

@trace FR-SEARCH-10
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text

from .conftest import EmbedServer, Pg, World, add_material, embed_material, need, scalar, world

pytestmark = pytest.mark.anyio

SPANS = [(1, "Блокчейн хранит цепочку блоков, каждый блок ссылается на предыдущий по хешу, и это всё.")]
UP_RU, UP_OTHER = "1" * 64, "2" * 64


@pytest.fixture
async def w(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, embed_server: EmbedServer
) -> AsyncIterator[World]:
    monkeypatch.setenv("READER_EMBED_URL", embed_server.url)
    built = await world(pg, tmp_path, monkeypatch)
    try:
        yield built
    finally:
        await built.engine.dispose()


async def uploaded_material(w: World, sha: str, language: str) -> uuid.UUID:
    """An upload's material whose pack carries the profile SRV-INGEST's import builds from the upload row."""
    from reader.ingest import manifest, overlays, packs, views

    owner = w.users["ivan"]
    async with w.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO uploads (id, owner_id, sha256, size, filename, language, state)"
                " VALUES (gen_random_uuid(), :o, :s, 100, 'x.pdf', :l, 'received')"
            ),
            {"o": owner, "s": sha, "l": language},
        )
    ingest = await add_material(w, "ivan", sha, SPANS, config_id=f"up-{sha[:12]}")
    async with w.engine.begin() as conn:
        values = await packs.rule_values(conn, sha, None)
        assert values == {"license": packs.UPLOAD_LICENSE, "language": language}
        pack = await views.active_pack(conn, owner, sha)
        assert pack is not None
        oid = await overlays.build_profile(conn, owner, pack.pack_id, values)
        await manifest.request_activation(conn, owner, pack.pack_id, {"profile": oid})
        await manifest.try_activate(conn, owner, pack.pack_id)
    return ingest


async def test_uploaded_materials_take_their_upload_language(
    w: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    jobs = need("reader.search.jobs")
    ru = await uploaded_material(w, UP_RU, "ru")
    other = await uploaded_material(w, UP_OTHER, "other")
    monkeypatch.setenv("READER_CONFIG", str(tmp_path / "missing.yaml"))  # never consulted
    owner = w.users["ivan"]
    async with w.engine.connect() as conn:
        for sha, expected in ((UP_RU, "russian"), (UP_OTHER, "simple")):
            pack = await jobs.buildable_pack(conn, owner, sha)
            assert await jobs.fts_config_of(conn, owner, pack.pack_id) == expected
    await embed_material(w, ru, UP_RU)
    await embed_material(w, other, UP_OTHER)
    configs = await scalar(
        w.engine, "SELECT string_agg(DISTINCT fts_config, ',' ORDER BY fts_config) FROM search_span_vectors"
    )
    assert configs == "russian,simple"
    store = need("reader.search.store")
    found = await store.search(w.engine, owner, "блок", k=5, material_sha256=UP_RU)
    assert found.results  # a missing reader.yaml does not block an uploaded material's lookup
