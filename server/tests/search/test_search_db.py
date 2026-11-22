"""SRV-SEARCH [db]: tables, the embed job, full-text configuration, hybrid search, the route, status, ensure.

@trace FR-SEARCH-4 FR-SEARCH-5 FR-SEARCH-6 FR-SEARCH-7 FR-SEARCH-8 FR-SEARCH-9
@trace INV-SEARCH-1 INV-SEARCH-2 INV-SEARCH-3 INV-SEARCH-4 INV-SEARCH-5 INV-SEARCH-7 VEC-SEARCH-5
"""

from __future__ import annotations

import logging
import time
import uuid
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
    fixture,
    need,
    scalar,
    world,
)

pytestmark = pytest.mark.anyio

FILL_RU = " Здесь больше ничего не сказано ни о чём другом, это просто заполнитель для абзаца."
FILL_EN = " Nothing further is said here about anything else in this paragraph."

RU_SPANS = [
    (1, "Блокчейн хранит цепочку блоков, каждый блок ссылается на предыдущий по хешу."),
    (1, "Майнер проверяет транзакции и получает награду за новый блок."),
    # each page's text is at least 80 characters (CHUNK_MIN), so every page gives one chunk; FILL_* pad short
    # texts with words that share no lexeme with any query here
    (2, "Кошелёк хранит закрытые ключи владельца и подписывает ими переводы, не раскрывая их сети."),
    (3, "Смарт-контракт исполняется виртуальной машиной сети, и каждый узел повторяет его вычисление."),
]


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


async def _session(w: World, handle: str) -> str:
    from reader.auth.store import _start_session

    async with w.engine.begin() as conn:
        value: str = await _start_session(conn, w.users[handle], None)
    return value


@asynccontextmanager
async def client(w: World, handle: str = "ivan") -> AsyncIterator[httpx.AsyncClient]:
    from reader.api.app import create_app

    app = create_app(w.settings, w.engine, run_cleanup_task=False)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN},
        cookies={SESSION_COOKIE: await _session(w, handle)},
    ) as c:
        yield c


async def _ingested(w: World, handle: str = "ivan", sha: str = MAT_RU, spans: Any = RU_SPANS) -> uuid.UUID:
    ingest = await add_material(w, handle, sha, spans, config_id="book-ru" if sha == MAT_RU else "book-en")
    await embed_material(w, ingest, sha)
    return ingest


# ---- FR-SEARCH-4 ------------------------------------------------------------------------------------------


async def test_migration_creates_the_tables_and_extension(w: World) -> None:
    assert await scalar(w.engine, "SELECT extversion FROM pg_extension WHERE extname = 'vector'") is not None
    cols = await scalar(
        w.engine,
        "SELECT string_agg(column_name, ',' ORDER BY column_name) FROM information_schema.columns"
        " WHERE table_name = 'search_chunks'",
    )
    names = ("owner_id", "ingest_id", "model", "params_sha256", "fts_config", "embedding", "fts", "span_seqs")
    for col in names:
        assert col in cols.split(","), col
    assert await scalar(w.engine, "SELECT to_regclass('search_embeds') IS NOT NULL") is True
    indexes = "SELECT count(*) FROM pg_indexes WHERE tablename = 'search_chunks' AND indexdef ILIKE :p"
    assert await scalar(w.engine, indexes, p="%gin%") == 1
    # declared deviation: an exact scan, no vector index
    assert await scalar(w.engine, indexes, p="%hnsw%") == 0


async def test_fts_config_is_text_with_a_check(w: World) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    assert await scalar(
        w.engine,
        "SELECT data_type FROM information_schema.columns WHERE table_name='search_chunks'"
        " AND column_name='fts_config'",
    ) == "text"
    with pytest.raises(IntegrityError):
        async with w.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO search_chunks (owner_id, ingest_id, material_sha256, model, params_sha256,"
                    " fts_config, page, chunk_no, char_start, char_stop, span_seqs, primary_seq, text,"
                    " embedding)"
                    " VALUES (:o, gen_random_uuid(), :s, 'm', 'p', 'german', 1, 0, 0, 1, '{0}', 0, 'x',"
                    " array_fill(0.0::real, ARRAY[1024])::vector)"
                ),
                {"o": w.users["ivan"], "s": MAT_RU},
            )


# ---- FR-SEARCH-5, INV-SEARCH-3 ----------------------------------------------------------------------------


async def test_embed_job_writes_chunks_and_one_embeds_row(w: World, embed_server: EmbedServer) -> None:
    ingest = await _ingested(w)
    chunks = await scalar(w.engine, "SELECT count(*) FROM search_chunks WHERE ingest_id = :i", i=ingest)
    assert chunks == 3  # pages 1, 2, 3 (each page's text under 1200 chars)
    assert await scalar(w.engine, "SELECT count(*) FROM search_embeds WHERE ingest_id = :i", i=ingest) == 1
    assert await scalar(w.engine, "SELECT chunk_count FROM search_embeds WHERE ingest_id = :i", i=ingest) == 3
    assert await scalar(w.engine, "SELECT DISTINCT fts_config FROM search_chunks") == "russian"
    passage_calls = [n for k, n in embed_server.encoder.calls if k == "passage"]
    assert passage_calls and max(passage_calls) <= 8
    calls = len(embed_server.encoder.calls)
    await embed_material(w, ingest, MAT_RU)  # same key again: no new embed call, no duplicate rows
    assert len(embed_server.encoder.calls) == calls
    assert await scalar(w.engine, "SELECT count(*) FROM search_chunks WHERE ingest_id = :i", i=ingest) == 3


async def test_more_chunks_than_one_batch(w: World, embed_server: EmbedServer) -> None:
    pages = [(p, f"страница {p}." + FILL_RU) for p in range(1, 13)]  # 12 pages, one chunk each
    await _ingested(w, spans=pages)
    assert await scalar(w.engine, "SELECT count(*) FROM search_chunks") == 12
    passage_calls = [n for k, n in embed_server.encoder.calls if k == "passage"]
    assert passage_calls == [8, 4]


async def test_ingest_not_ready_at_once(w: World) -> None:
    jobs = need("reader.search.jobs")
    errors = need("reader.search.errors")
    from reader.jobs.model import JobContext

    args = jobs.embed_args(MAT_RU, uuid.uuid4(), fts_config="russian")
    started = time.monotonic()
    with pytest.raises(errors.IngestNotReady):
        async with w.engine.begin() as conn:
            await jobs.handle_embed(args, conn, JobContext(job_id=1, key="k", attempts=0))
    assert time.monotonic() - started < 5  # no in-handler poll holds the worker slot
    # then the import commits under that id, and the job succeeds
    ingest = await add_material(w, "ivan", MAT_RU, RU_SPANS)
    args = jobs.embed_args(MAT_RU, ingest, fts_config="russian")
    async with w.engine.begin() as conn:
        await jobs.handle_embed(args, conn, JobContext(job_id=2, key="k", attempts=1))
    assert await scalar(w.engine, "SELECT count(*) FROM search_embeds") == 1


async def test_superseded_ingest_and_params_mismatch_are_permanent(w: World) -> None:
    jobs = need("reader.search.jobs")
    errors = need("reader.search.errors")
    from reader.jobs.model import JobContext

    ingest = await add_material(w, "ivan", MAT_RU, RU_SPANS)
    stale = jobs.embed_args(MAT_RU, uuid.uuid4(), fts_config="russian")
    with pytest.raises(errors.SearchError) as raised:
        async with w.engine.begin() as conn:
            await jobs.handle_embed(stale, conn, JobContext(job_id=1, key="k", attempts=0))
    assert raised.value.code == "ingest_superseded"
    wrong_language = jobs.embed_args(MAT_RU, ingest, fts_config="english")
    with pytest.raises(errors.SearchError) as raised:
        async with w.engine.begin() as conn:
            await jobs.handle_embed(wrong_language, conn, JobContext(job_id=2, key="k", attempts=0))
    assert raised.value.code == "params_mismatch"
    for cls in (errors.SearchError,):
        assert cls not in jobs.JOB_KINDS[0].transient


async def test_unhealthy_embed_service_raises_embed_unavailable(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = need("reader.search.jobs")
    errors = need("reader.search.errors")
    from reader.jobs.model import JobContext

    monkeypatch.setenv("READER_EMBED_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(jobs, "HEALTH_WAIT_S", 0.5)
    ingest = await add_material(w, "ivan", MAT_RU, RU_SPANS)
    with pytest.raises(errors.EmbedUnavailable):
        async with w.engine.begin() as conn:
            await jobs.handle_embed(
                jobs.embed_args(MAT_RU, ingest, fts_config="russian"), conn, JobContext(1, "k", 0)
            )
    assert await scalar(w.engine, "SELECT count(*) FROM search_chunks") == 0


async def test_model_mismatch_is_permanent(w: World, embed_server: EmbedServer) -> None:
    jobs = need("reader.search.jobs")
    errors = need("reader.search.errors")
    from reader.jobs.model import JobContext

    embed_server.server.model_id = "BAAI/bge-m3@other"
    ingest = await add_material(w, "ivan", MAT_RU, RU_SPANS)
    with pytest.raises(errors.SearchError) as raised:
        async with w.engine.begin() as conn:
            await jobs.handle_embed(
                jobs.embed_args(MAT_RU, ingest, fts_config="russian"), conn, JobContext(1, "k", 0)
            )
    assert raised.value.code == "model_mismatch"


# ---- FR-SEARCH-6 ------------------------------------------------------------------------------------------


async def test_fts_config_follows_the_material_language(w: World) -> None:
    await _ingested(w, sha=MAT_EN, spans=[(1, "Miners validate blocks and earn rewards." + FILL_EN)])
    assert await scalar(w.engine, "SELECT DISTINCT fts_config FROM search_chunks") == "english"
    store = need("reader.search.store")
    async with w.engine.connect() as conn:
        assert await store.fts_config_of(conn, MAT_EN) == "english"
        assert await store.fts_config_of(conn, "c" * 64) == "simple"


# ---- FR-SEARCH-7, INV-SEARCH-1, -2, -4 --------------------------------------------------------------------


async def test_hybrid_search_returns_cited_chunks(w: World) -> None:
    store = need("reader.search.store")
    await _ingested(w)
    result = await store.search(w.engine, w.users["ivan"], "кошелёк ключи", k=10)
    assert not result.degraded
    top = result.results[0]
    assert top.page == 2
    assert top.span_id == f"{MAT_RU}:2"
    assert top.span_ids == [f"{MAT_RU}:2"]
    assert top.dense_rank == 1
    # FR-SEARCH-7: the slice serves the dense order (hybrid measured below dense-only on v2), so no fts rank
    params = need("reader.search.params")
    assert params.HYBRID is False
    assert all(r.fts_rank is None for r in result.results)
    assert [r.dense_rank for r in result.results] == list(range(1, len(result.results) + 1))
    assert len(top.snippet) <= 300


def test_fusion_weight_is_the_dev_set_choice() -> None:
    import json

    from .conftest import FIXTURES

    params = need("reader.search.params")
    chosen = json.loads((FIXTURES / "results" / "fusion-weight-v1.json").read_text())["chosen_w_dense"]
    assert chosen == params.W_DENSE


async def test_search_is_owner_isolated(w: World) -> None:
    store = need("reader.search.store")
    await _ingested(w, "ivan")
    await _ingested(w, "anna", sha=MAT_EN, spans=[(1, "кошелёк ключи кошелёк" + FILL_RU)])
    result = await store.search(w.engine, w.users["ivan"], "кошелёк ключи", k=50)
    assert result.results
    assert all(r.material_sha256 == MAT_RU for r in result.results)
    other = await store.search(w.engine, w.users["anna"], "кошелёк ключи", k=50)
    assert all(r.material_sha256 == MAT_EN for r in other.results)


async def test_citations_resolve_to_spans_on_the_page(w: World) -> None:
    store = need("reader.search.store")
    ingest = await _ingested(w)
    result = await store.search(w.engine, w.users["ivan"], "блок хеш майнер", k=10)
    for r in result.results:
        for sid in r.span_ids:
            sha, seq = sid.split(":")
            page = await scalar(
                w.engine,
                "SELECT page FROM material_spans WHERE ingest_id = :i AND seq = :q",
                i=ingest,
                q=int(seq),
            )
            assert sha == MAT_RU and page == r.page


async def test_order_is_total_and_deterministic(w: World) -> None:
    store = need("reader.search.store")
    same = [(p, "одинаковый текст про блок" + FILL_RU) for p in range(1, 6)]
    await _ingested(w, spans=same)
    first = await store.search(w.engine, w.users["ivan"], "блок", k=10)
    second = await store.search(w.engine, w.users["ivan"], "блок", k=10)
    assert [r.chunk_id for r in first.results] == [r.chunk_id for r in second.results]
    ids = [r.chunk_id for r in first.results]
    assert ids == sorted(ids)  # equal distances and ranks fall back to the chunk id


async def test_degraded_when_the_embed_service_is_down(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    store = need("reader.search.store")
    await _ingested(w)
    monkeypatch.setenv("READER_EMBED_URL", "http://127.0.0.1:9")
    result = await store.search(w.engine, w.users["ivan"], "кошелёк", k=5)
    assert result.degraded
    assert result.results and all(r.dense_rank is None for r in result.results)


async def test_material_filter_through_search_and_route(w: World) -> None:
    store = need("reader.search.store")
    await _ingested(w)
    await _ingested(w, sha=MAT_EN, spans=[(1, "кошелёк ключи по-английски" + FILL_RU)])
    both = await store.search(w.engine, w.users["ivan"], "кошелёк ключи", k=50)
    assert {r.material_sha256 for r in both.results} == {MAT_RU, MAT_EN}
    only = await store.search(w.engine, w.users["ivan"], "кошелёк ключи", k=50, material_sha256=MAT_EN)
    assert only.results
    assert {r.material_sha256 for r in only.results} == {MAT_EN}
    async with client(w) as c:
        r = await c.post("/api/search", json={"q": "кошелёк ключи", "k": 50, "material": MAT_RU})
    assert r.status_code == 200, r.text
    assert {x["material_sha256"] for x in r.json()["results"]} == {MAT_RU}


async def test_only_the_current_key_is_served(w: World) -> None:
    store = need("reader.search.store")
    jobs = need("reader.search.jobs")
    params = need("reader.search.params")
    from reader.jobs.model import JobContext

    ingest = await _ingested(w)  # key 1: russian
    # the owner changes the material's language to en: the russian chunks are no longer current
    config = w.config_path.read_text(encoding="utf-8")
    w.config_path.write_text(config.replace("language: ru", "language: en"), encoding="utf-8")
    stale = await store.search(w.engine, w.users["ivan"], "кошелёк ключи", k=50)
    assert stale.results == []
    args = jobs.embed_args(MAT_RU, ingest, fts_config="english")
    async with w.engine.begin() as conn:  # key 2, embedded later: newest row, and matching
        await jobs.handle_embed(args, conn, JobContext(2, "k", 0))
    params_of = "SELECT params_sha256 FROM search_chunks WHERE id = :i"
    fresh = await store.search(w.engine, w.users["ivan"], "кошелёк ключи", k=50)
    assert fresh.results
    for hit in fresh.results:
        assert await scalar(w.engine, params_of, i=hit.chunk_id) == params.params_sha256("english")
    # back to ru: key 1 is current again although key 2's row is newer (params match, not the newest row)
    w.config_path.write_text(config, encoding="utf-8")
    again = await store.search(w.engine, w.users["ivan"], "кошелёк ключи", k=50)
    assert again.results
    for hit in again.results:
        assert await scalar(w.engine, params_of, i=hit.chunk_id) == params.params_sha256("russian")


async def test_span_id_is_the_primary_span(w: World) -> None:
    store = need("reader.search.store")
    # page 1: a short span 0 and a long span 1 in one chunk; span 1 overlaps most, so it is primary
    await _ingested(w, spans=[(1, "Кошелёк:"), (1, "закрытые ключи владельца" + FILL_RU)])
    result = await store.search(w.engine, w.users["ivan"], "кошелёк ключи", k=1)
    (hit,) = result.results
    assert hit.span_ids == [f"{MAT_RU}:0", f"{MAT_RU}:1"]
    assert hit.span_id == f"{MAT_RU}:1"


async def test_full_text_merges_configurations_by_rank_then_id(w: World) -> None:
    store = need("reader.search.store")
    word = "bitcoin"  # an ASCII word: the russian configuration stems it with the english stemmer too
    await _ingested(w, spans=[(1, f"{word} " * 3 + FILL_RU), (2, f"{word} раз" + FILL_RU)])
    await _ingested(w, sha=MAT_EN, spans=[(1, f"{word} {word}" + FILL_EN), (2, f"just one {word}" + FILL_EN)])
    async with w.engine.connect() as conn:
        got = await store.fts_list(conn, w.users["ivan"], word, None)
        ranks = {
            r[0]: float(r[1])
            for r in await conn.execute(
                text(
                    "SELECT id, ts_rank_cd(fts, to_tsquery(CAST(fts_config AS regconfig), :w))"
                    " FROM search_chunks"
                ),
                {"w": word},
            )
        }
    assert len(got) == 4
    config_of = "SELECT fts_config FROM search_chunks WHERE id = :i"
    assert {await scalar(w.engine, config_of, i=i) for i in got} == {"russian", "english"}
    assert got == sorted(got, key=lambda i: (-ranks[i], i))


# ---- INV-SEARCH-7, VEC-SEARCH-5 ---------------------------------------------------------------------------


async def test_full_text_is_or_and_parses_no_operators(w: World) -> None:
    store = need("reader.search.store")
    data = fixture("fts-or.yaml")
    spans = [(i + 1, c["text"]) for i, c in enumerate(data["chunks"])]
    await _ingested(w, sha=MAT_EN, spans=spans)
    page_of = {c["id"]: i + 1 for i, c in enumerate(data["chunks"])}
    for case in data["cases"]:
        async with w.engine.connect() as conn:
            got = await store.fts_list(conn, w.users["ivan"], case["query"], None)
        pages = {await scalar(w.engine, "SELECT page FROM search_chunks WHERE id = :i", i=i) for i in got}
        assert pages == {page_of[c] for c in case["expected_ids"]}, case["name"]


OPERATOR_QUERIES = ["&", "|", "!", ":*", "<->", "'", "\\", "a & b | !c", "''\\''", "x:* <2> y"]


@pytest.mark.parametrize("query", OPERATOR_QUERIES)
async def test_operator_strings_never_raise(w: World, query: str) -> None:
    store = need("reader.search.store")
    await _ingested(w, sha=MAT_EN, spans=[(1, "a b c x y" + FILL_EN)])
    async with w.engine.connect() as conn:
        await store.fts_list(conn, w.users["ivan"], query, None)


# ---- FR-SEARCH-8, INV-SEARCH-5 ----------------------------------------------------------------------------


async def test_route_returns_results_for_the_owner(w: World) -> None:
    await _ingested(w)
    async with client(w) as c:
        r = await c.post("/api/search", json={"q": "кошелёк ключи", "k": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["degraded"] is False
    assert 1 <= len(body["results"]) <= 3
    first = body["results"][0]
    assert set(first) == {
        "chunk_id", "material_sha256", "page", "span_id", "span_ids", "snippet", "score", "dense_rank",
        "fts_rank",
    }


async def test_route_is_owner_only_and_validates(w: World) -> None:
    async with client(w, "anna") as c:
        assert (await c.post("/api/search", json={"q": "x"})).status_code == 404
    async with client(w) as c:
        for body in ({"q": ""}, {"q": "x" * 1001}, {"q": "x", "k": 0}, {"q": "x", "k": 51},
                     {"q": "x", "material": "zz"}, {"k": 3}, [1]):
            r = await c.post("/api/search", json=body)
            assert r.status_code == 400, body
            assert set(r.json()) == {"error"}


async def test_no_text_in_logs(
    w: World, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    canary = "kanareyka" + uuid.uuid4().hex[:8]
    caplog.set_level(logging.DEBUG)  # before ingest: the embed job and the embed service log too
    await _ingested(w, spans=[(1, f"текст {canary} про блок" + FILL_RU)])
    async with client(w) as c:
        await c.post("/api/search", json={"q": f"{canary} блок"})
        await c.post("/api/search", json={"q": canary * 200})
        monkeypatch.setenv("READER_EMBED_URL", "http://127.0.0.1:9")
        await c.post("/api/search", json={"q": f"{canary} блок"})
        # a forced SQL error with the canary in the bound parameters
        async with w.engine.begin() as conn:
            await conn.execute(text("ALTER TABLE search_chunks RENAME COLUMN fts TO fts_gone"))
        failed = await c.post("/api/search", json={"q": f"{canary} блок"})
        assert failed.status_code >= 500
    assert canary not in caplog.text
    for record in caplog.records:
        assert canary not in record.getMessage()
        assert canary not in repr(record.args)


# ---- FR-SEARCH-9 ------------------------------------------------------------------------------------------


async def test_status_and_ensure(w: World, pg: Pg, monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = need("reader.search.jobs")
    from reader.config import Secret
    from reader.db import DbTarget
    from reader.jobs.catalog import Catalog
    from reader.jobs.queue import Jobs
    from reader.jobs.worker import make_app

    app = make_app(DbTarget(pg.host, pg.port, pg.user, pg.database, Secret(pg.password)))
    await app.open_async()
    try:
        queue = Jobs(w.engine, app, Catalog(jobs.JOB_KINDS))
        assert await jobs.search_status(queue, w.users["ivan"], MAT_RU) == "none"
        assert await jobs.ensure(queue, w.users["ivan"], MAT_RU) == "no_ingest"
        ingest = await add_material(w, "ivan", MAT_RU, RU_SPANS)
        assert await jobs.ensure(queue, w.users["ivan"], MAT_RU) == "queued"
        assert await jobs.search_status(queue, w.users["ivan"], MAT_RU) == "queued"
        # a dead key (3 counted attempts) is requeued by ensure with its attempts reset
        key = jobs._key(jobs.embed_args(MAT_RU, ingest, fts_config="russian"))
        async with w.engine.begin() as conn:
            await conn.execute(
                text("UPDATE job_keys SET outcome = 'dead', attempts = 3 WHERE key = :k"), {"k": key}
            )
            await conn.execute(
                text("UPDATE procrastinate_jobs SET status = 'failed' WHERE lock = :k"), {"k": key}
            )
        assert await jobs.search_status(queue, w.users["ivan"], MAT_RU) == "dead"
        assert await jobs.ensure(queue, w.users["ivan"], MAT_RU) == "queued"
        assert await scalar(w.engine, "SELECT attempts FROM job_keys WHERE key = :k", k=key) == 0
        assert await scalar(w.engine, "SELECT outcome FROM job_keys WHERE key = :k", k=key) is None
        await embed_material(w, ingest, MAT_RU)
        assert await jobs.search_status(queue, w.users["ivan"], MAT_RU) == "ready"
    finally:
        await app.close_async()
