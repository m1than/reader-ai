"""SRV-SEARCH [db]: a deleted material's chunks leave results and come back with its restore.

@trace FR-SEARCH-20 FR-INGEST-31 FR-SEARCH-9

The delete and restore go through SRV-INGEST's routes. Before SRV-LESSON's manage migration is merged,
`courses` lacks the columns the delete reads, so `course_columns` adds them with IF NOT EXISTS (a no-op after
the merge).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from .conftest import (
    MAT_EN,
    MAT_RU,
    ORIGIN,
    SESSION_COOKIE,
    EmbedServer,
    Pg,
    World,
    add_material,
    embed_material,
    need,
    world,
)

pytestmark = pytest.mark.anyio

FILL_RU = " Здесь больше ничего не сказано ни о чём другом, это просто заполнитель для абзаца."
A_SPANS = [
    (1, "Кошелёк хранит закрытые ключи владельца и подписывает ими переводы, не раскрывая их сети."),
    (2, "Майнер проверяет транзакции и получает награду за новый блок." + FILL_RU),
]
B_SPANS = [
    (1, "Узел сети хранит копию цепочки и проверяет каждый новый блок перед записью." + FILL_RU),
    (2, "Смарт-контракт исполняется виртуальной машиной сети, и ключи узла ему не нужны." + FILL_RU),
]
QUERY = "кошелёк ключи"


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


@asynccontextmanager
async def client(w: World) -> AsyncIterator[httpx.AsyncClient]:
    from reader.api.app import create_app
    from reader.auth.store import _start_session

    async with w.engine.begin() as conn:
        session = await _start_session(conn, w.users["ivan"], None)
    app = create_app(w.settings, w.engine, run_cleanup_task=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers={"Origin": ORIGIN},
        cookies={SESSION_COOKIE: session},
    ) as c:
        yield c


async def course_columns(w: World) -> None:
    """Pre-merge shim: SRV-LESSON's manage columns the material delete reads (no-op after the merge)."""
    async with w.engine.begin() as conn:
        for column in ("deleted_at timestamptz", "deleted_by_material text", "archived_at timestamptz"):
            await conn.execute(text(f"ALTER TABLE courses ADD COLUMN IF NOT EXISTS {column} NULL"))
        await conn.execute(text("ALTER TABLE lessons ADD COLUMN IF NOT EXISTS deleted_at timestamptz NULL"))


async def two_materials(w: World) -> None:
    for sha, spans, config_id in ((MAT_RU, A_SPANS, "book-ru"), (MAT_EN, B_SPANS, "book-en")):
        ingest = await add_material(w, "ivan", sha, spans, config_id=config_id)
        await embed_material(w, ingest, sha)


def ids(result: Any) -> list[tuple[str, str, float]]:
    return [(h.span_id, h.material_sha256, h.score) for h in result.results]


async def search_rows(w: World) -> list[Any]:
    async with w.engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT * FROM search_span_vectors ORDER BY owner_id, overlay_id, locator, window_no"))).all()
    return [tuple(map(str, r)) for r in rows]


async def remove(w: World, handle: str, sha: str) -> None:
    """A delete as SRV-INGEST records it: the material leaves the head entry and is soft-deleted."""
    from reader.ingest import manifest

    async with w.engine.begin() as conn:
        await manifest.remove_material(conn, w.users[handle], sha)
        deleted = "UPDATE materials SET deleted_at = now() WHERE owner_id = :o AND sha256 = :s"
        await conn.execute(text(deleted), {"o": w.users[handle], "s": sha})


async def test_a_deleted_material_leaves_results_and_its_restore_brings_them_back(w: World) -> None:
    store = need("reader.search.store")
    await course_columns(w)
    await two_materials(w)
    owner = w.users["ivan"]
    first = await store.search(w.engine, owner, QUERY, k=10)
    assert first.results[0].material_sha256 == MAT_RU  # the top hit is in material A
    b_only = await store.search(w.engine, owner, QUERY, k=10, material_sha256=MAT_EN)
    rows_before = await search_rows(w)
    async with client(w) as c:
        gone = await c.request("DELETE", "/api/materials/book-ru", json={"confirm": "book-ru"})
        assert gone.status_code == 200, gone.text
        filtered = await c.post("/api/search", json={"q": QUERY, "k": 10, "material": MAT_RU})
        routed = await c.post("/api/search", json={"q": QUERY, "k": 10})
    after = await store.search(w.engine, owner, QUERY, k=10)
    assert after.results and {h.material_sha256 for h in after.results} == {MAT_EN}
    b_after = await store.search(w.engine, owner, QUERY, k=10, material_sha256=MAT_EN)
    assert ids(b_after) == ids(b_only)  # B's results unchanged
    assert (filtered.status_code, filtered.json()) == (404, {"error": "not_found"})
    assert {x["material_sha256"] for x in routed.json()["results"]} == {MAT_EN}
    async with client(w) as c:
        back = await c.post("/api/materials/book-ru/restore", json={})
        assert back.status_code == 200, back.text
    again = await store.search(w.engine, owner, QUERY, k=10)
    assert ids(again) == ids(first)  # exactly the first result set, no re-embed
    assert await search_rows(w) == rows_before  # no row of search_chunks or search_embeds changed


async def test_the_full_text_list_excludes_a_deleted_material_too(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = need("reader.search.store")
    await two_materials(w)
    owner = w.users["ivan"]
    await remove(w, "ivan", MAT_RU)
    monkeypatch.setenv("READER_EMBED_URL", "http://127.0.0.1:9")  # embed down: the full-text list alone
    degraded = await store.search(w.engine, owner, QUERY, k=10)
    assert degraded.degraded and degraded.results
    assert {h.material_sha256 for h in degraded.results} == {MAT_EN}
    filtered = await store.search(w.engine, owner, QUERY, k=10, material_sha256=MAT_RU)
    assert filtered.results == []


async def test_another_owners_deleted_material_does_not_hide_mine(w: World) -> None:
    store = need("reader.search.store")
    await two_materials(w)
    ingest = await add_material(w, "anna", MAT_RU, A_SPANS, config_id="book-ru")
    await embed_material(w, ingest, MAT_RU)
    await remove(w, "anna", MAT_RU)
    mine = await store.search(w.engine, w.users["ivan"], QUERY, k=10)
    assert MAT_RU in {h.material_sha256 for h in mine.results}
    theirs = await store.search(w.engine, w.users["anna"], QUERY, k=10)
    assert theirs.results == []


async def test_find_and_open_leave_a_deleted_material_and_the_restore_serves_it_again(w: World) -> None:
    await course_columns(w)
    await two_materials(w)
    body = {"q": QUERY, "budget": 10500}
    async with client(w) as c:
        first = (await c.post("/api/find", json=body)).json()["cards"]
        mine = [x["id"] for x in first if x["source"]["id"] == f"src:{MAT_RU}"]
        assert mine
        gone = await c.request("DELETE", "/api/materials/book-ru", json={"confirm": "book-ru"})
        assert gone.status_code == 200
        after = (await c.post("/api/find", json=body)).json()["cards"]
        assert after and {x["source"]["id"] for x in after} == {f"src:{MAT_EN}"}
        opened = await c.post("/api/open", json={"id": mine[0]})
        assert (opened.status_code, opened.json()) == (404, {"error": "not_found"})
        assert (await c.post("/api/materials/book-ru/restore", json={})).status_code == 200
        assert (await c.post("/api/find", json=body)).json()["cards"] == first
        assert (await c.post("/api/open", json={"id": mine[0]})).status_code == 200


async def test_the_restore_route_requeues_a_dead_first_pack_key(w: World, pg: Pg) -> None:
    """Code review of 0eddd7d (blocking 2): the api has no worker; the route passes its own Jobs."""
    from reader.api.app import create_app
    from reader.auth.store import _start_session
    from reader.config import Secret
    from reader.db import DbTarget
    from reader.jobs.catalog import Catalog
    from reader.jobs.queue import Jobs
    from reader.jobs.worker import make_app

    jobs = need("reader.search.jobs")
    await course_columns(w)
    await add_material(w, "ivan", MAT_RU, A_SPANS, config_id="book-ru")  # its first pack, never embedded
    owner = w.users["ivan"]
    app = make_app(DbTarget(pg.host, pg.port, pg.user, pg.database, Secret(pg.password)))
    await app.open_async()
    try:
        queue = Jobs(w.engine, app, Catalog(jobs.JOB_KINDS))
        async with w.engine.connect() as conn:
            args = await jobs.current_args(conn, owner, MAT_RU)
        assert args is not None
        await queue.enqueue(jobs.KIND, args)
        async with w.engine.begin() as conn:
            await conn.execute(text("UPDATE job_keys SET outcome = 'dead', attempts = 3 WHERE key = :k"),
                               {"k": jobs.job_key(args)})
        async with w.engine.begin() as conn:
            session = await _start_session(conn, owner, None)
        api = create_app(w.settings, w.engine, run_cleanup_task=False, jobs=queue)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url=ORIGIN, headers={"Origin": ORIGIN},
            cookies={SESSION_COOKIE: session},
        ) as c:
            gone = await c.request("DELETE", "/api/materials/book-ru", json={"confirm": "book-ru"})
            assert gone.status_code == 200, gone.text
            back = await c.post("/api/materials/book-ru/restore", json={})
            assert back.status_code == 200, back.text
        assert await jobs.search_status(queue, owner, MAT_RU) == "queued"
        attempts = "SELECT attempts FROM job_keys WHERE key = :k"
        async with w.engine.connect() as conn:
            assert (await conn.execute(text(attempts), {"k": jobs.job_key(args)})).scalar() == 0
    finally:
        await app.close_async()
