"""FR-SEARCH-10: the full-text configuration of uploaded materials comes from their upload's language.

@trace FR-SEARCH-10
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text

from .conftest import EmbedServer, Pg, World, add_material, need, scalar, world

pytestmark = pytest.mark.anyio

SPANS = [(1, "Блокчейн хранит цепочку блоков, каждый блок ссылается на предыдущий по хешу, и это всё.")]
UP_RU, UP_OTHER, UP_GONE = "1" * 64, "2" * 64, "3" * 64


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


async def uploaded_material(w: World, sha: str, language: str | None) -> uuid.UUID:
    if language is not None:
        async with w.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO uploads (id, owner_id, sha256, size, filename, language, state)"
                    " VALUES (gen_random_uuid(), :o, :s, 100, 'x.pdf', :l, 'received')"
                ),
                {"o": w.users["ivan"], "s": sha, "l": language},
            )
    return await add_material(w, "ivan", sha, SPANS, config_id=f"up-{sha[:12]}")


async def test_uploaded_materials_take_their_upload_language(
    w: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = need("reader.search.store")
    jobs = need("reader.search.jobs")
    from reader.jobs.model import JobContext

    ru = await uploaded_material(w, UP_RU, "ru")
    other = await uploaded_material(w, UP_OTHER, "other")
    gone = await uploaded_material(w, UP_GONE, None)
    monkeypatch.setenv("READER_CONFIG", str(tmp_path / "missing.yaml"))  # never consulted for up- ids
    async with w.engine.connect() as conn:
        assert await store.fts_config_of(conn, UP_RU) == "russian"
        assert await store.fts_config_of(conn, UP_OTHER, other) == "simple"
        assert await store.fts_config_of(conn, UP_GONE) == "simple"  # no upload row
    for ingest, sha in ((ru, UP_RU), (other, UP_OTHER)):
        async with w.engine.connect() as conn:
            fts = await store.fts_config_of(conn, sha, ingest)
        async with w.engine.begin() as conn:
            await jobs.handle_embed(jobs.embed_args(sha, ingest, fts), conn, JobContext(1, "k", 0))
    configs = await scalar(
        w.engine, "SELECT string_agg(DISTINCT fts_config, ',' ORDER BY fts_config) FROM search_chunks"
    )
    assert configs == "russian,simple"
    del gone


async def test_current_keys_use_the_upload_language(
    w: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = need("reader.search.store")
    params = need("reader.search.params")
    jobs = need("reader.search.jobs")
    from reader.jobs.model import JobContext

    ingest = await uploaded_material(w, UP_RU, "ru")
    async with w.engine.begin() as conn:
        await jobs.handle_embed(jobs.embed_args(UP_RU, ingest, "russian"), conn, JobContext(1, "k", 0))
    monkeypatch.setenv("READER_CONFIG", str(tmp_path / "missing.yaml"))
    async with w.engine.connect() as conn:
        keys = await store.current_keys(conn, w.users["ivan"])
    assert keys == {"ci": [ingest], "cp": [params.params_sha256("russian")]}
