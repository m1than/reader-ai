"""SRV-SEARCH [db] property tests: owner isolation, citations, total order, no operator injection.

@trace INV-SEARCH-1 INV-SEARCH-2 INV-SEARCH-4 INV-SEARCH-7 FR-SEARCH-3 FR-SEARCH-7

Each example empties the search and test-only ingest tables, builds generated corpora (one English material
per owner), embeds them through the real job handler and the fake embed service, and checks the invariant.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text

from .conftest import (
    EmbedServer,
    Pg,
    World,
    add_material,
    embed_material,
    hash_vector,
    need,
    reset,
    world,
)

pytestmark = pytest.mark.anyio

WORDS = ("block", "chain", "miner", "wallet", "key", "hash", "node", "ledger", "token", "fork")
FILL = " Nothing further is said here about anything else in this paragraph."
SHA = {"ivan": "c" * 64, "anna": "d" * 64}
PROPS = settings(
    max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)

span = st.tuples(
    st.sampled_from(("ivan", "anna")),
    st.integers(1, 4),
    st.lists(st.sampled_from(WORDS), min_size=1, max_size=8),
)
corpora = st.lists(span, min_size=1, max_size=10)
queries = st.lists(st.sampled_from(WORDS), min_size=1, max_size=3).map(" ".join)


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


async def _build(w: World, corpus: list[tuple[str, int, list[str]]]) -> dict[str, Any]:
    """One English material per owner that has spans; spans in page order. Returns owner → ingest id."""
    await reset(w)
    ingests: dict[str, Any] = {}
    for owner in ("ivan", "anna"):
        own = [(page, " ".join(words) + FILL) for o, page, words in corpus if o == owner]
        spans = sorted(own, key=lambda s: s[0])
        if spans:
            ingests[owner] = await add_material(w, owner, SHA[owner], spans, config_id="book-en")
            await embed_material(w, ingests[owner], SHA[owner])
    return ingests


async def _rows(w: World, sql: str, **params: Any) -> list[Any]:
    async with w.engine.connect() as conn:
        return list(await conn.execute(text(sql), params))


@PROPS
@given(corpus=corpora, query=queries)
async def test_owner_isolation(w: World, corpus: list[tuple[str, int, list[str]]], query: str) -> None:
    store = need("reader.search.store")
    await _build(w, corpus)
    for owner in ("ivan", "anna"):
        uid = w.users[owner]
        result = await store.search(w.engine, uid, query, k=50)
        async with w.engine.connect() as conn:
            fts = await store.fts_list(conn, uid, query, None)
            dense = await store.dense_list(conn, uid, hash_vector(query), None)
        ids = {r.chunk_id for r in result.results} | set(fts) | set(dense)
        rows = await _rows(w, "SELECT owner_id FROM search_chunks WHERE id = ANY(:i)", i=list(ids))
        owners = {r[0] for r in rows}
        assert owners <= {uid}, owner
        assert all(r.material_sha256 == SHA[owner] for r in result.results)


@PROPS
@given(corpus=corpora, query=queries)
async def test_citations_resolve(w: World, corpus: list[tuple[str, int, list[str]]], query: str) -> None:
    store = need("reader.search.store")
    ingests = await _build(w, corpus)
    for owner, ingest in ingests.items():
        result = await store.search(w.engine, w.users[owner], query, k=50)
        rows = await _rows(w, "SELECT seq, page, text FROM material_spans WHERE ingest_id = :i", i=ingest)
        spans = {int(r[0]): (int(r[1]), str(r[2])) for r in rows}
        for hit in result.results:
            seqs = [int(s.split(":")[1]) for s in hit.span_ids]
            assert all(s.split(":")[0] == SHA[owner] for s in hit.span_ids)
            assert all(spans[q][0] == hit.page for q in seqs)
            assert int(hit.span_id.split(":")[1]) in seqs
            page_text = " ".join(
                " ".join(body.split()) for q, (page, body) in sorted(spans.items()) if page == hit.page
            )
            (chunk,) = await _rows(w, "SELECT text FROM search_chunks WHERE id = :i", i=hit.chunk_id)
            assert chunk[0] in page_text


@PROPS
@given(corpus=corpora, query=queries)
async def test_order_is_total_and_deterministic(
    w: World, corpus: list[tuple[str, int, list[str]]], query: str
) -> None:
    store = need("reader.search.store")
    await _build(w, corpus)
    uid = w.users["ivan"]
    first = await store.search(w.engine, uid, query, k=50)
    second = await store.search(w.engine, uid, query, k=50)
    assert [h.chunk_id for h in first.results] == [h.chunk_id for h in second.results]
    async with w.engine.connect() as conn:
        dense = await store.dense_list(conn, uid, hash_vector(query), None)
        fts = await store.fts_list(conn, uid, query, None)
        vector = store.vector_literal(hash_vector(query))
        distance = {
            int(r[0]): float(r[1])
            for r in await conn.execute(
                text("SELECT id, embedding <=> CAST(:q AS vector) FROM search_chunks WHERE id = ANY(:i)"),
                {"q": vector, "i": dense},
            )
        }
        rank = {
            int(r[0]): float(r[1])
            for r in await conn.execute(
                text(
                    "SELECT id, ts_rank_cd(fts, CAST(:t AS tsquery)) FROM search_chunks WHERE id = ANY(:i)"
                ),
                {"t": need("reader.search.fusion").tsquery_text(sorted(set(query.split()))), "i": fts},
            )
        }
    assert dense == sorted(dense, key=lambda i: (round(distance[i], 9), i))  # (distance, id)
    assert fts == sorted(fts, key=lambda i: (-rank[i], i))  # (ts_rank_cd desc, id)


operator_text = st.lists(
    st.sampled_from([*"&|!:*<->()'\\ \t", "block ", "miner ", "wallet ", "key "]), max_size=20
).map("".join)


@PROPS
@given(corpus=corpora, query=operator_text)
async def test_full_text_matches_shared_lexemes_and_never_raises(
    w: World, corpus: list[tuple[str, int, list[str]]], query: str
) -> None:
    store = need("reader.search.store")
    await _build(w, corpus)
    uid = w.users["ivan"]
    async with w.engine.connect() as conn:
        got = await store.fts_list(conn, uid, query, None)
    expected = {
        int(r[0])
        for r in await _rows(
            w,
            "SELECT id FROM search_chunks WHERE owner_id = :o"
            " AND tsvector_to_array(fts) && tsvector_to_array(to_tsvector('english', :q))",
            o=uid,
            q=query,
        )
    }
    assert set(got) == expected
