"""SRV-SEARCH [db] property tests over span windows: owner isolation, citations, total order, no operator
injection (ADR-0039 revision, phase 2 unit 4b).

@trace INV-SEARCH-1 INV-SEARCH-2 INV-SEARCH-4 INV-SEARCH-7 FR-SEARCH-3 FR-SEARCH-7

Each example empties the search and test-only ingest tables, builds generated corpora (the same English
material sha256 for both owners, so they share a pack id and an overlay id), embeds them through the real job
handler and the fake embed service, and checks the invariant on the candidates and the legacy search().
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text

from .conftest import EmbedServer, Pg, World, add_material, embed_material, need, reset, world

pytestmark = pytest.mark.anyio

WORDS = ("block", "chain", "miner", "wallet", "key", "hash", "node", "ledger", "token", "fork")
FILL = " Nothing further is said here about anything else in this paragraph."
SHA = "c" * 64
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


async def _build(w: World, corpus: list[tuple[str, int, list[str]]]) -> list[str]:
    """The same material for each owner that has spans; spans in page order. Returns the owners built."""
    await reset(w)
    built = []
    for owner in ("ivan", "anna"):
        own = sorted([(page, " ".join(words) + FILL) for o, page, words in corpus if o == owner],
                     key=lambda s: s[0])
        if own:
            await embed_material(w, await add_material(w, owner, SHA, own, config_id="book-en"), SHA)
            built.append(owner)
    return built


async def _rows(w: World, sql: str, **params: Any) -> list[Any]:
    async with w.engine.connect() as conn:
        return list(await conn.execute(text(sql), params))


async def _candidates(w: World, owner: str, query: str, **kw: Any) -> Any:
    store = need("reader.search.store")
    async with w.engine.connect() as conn:
        return await store.candidates(conn, w.users[owner], query, limit=50, **kw)


@PROPS
@given(corpus=corpora, query=queries)
async def test_owner_isolation(w: World, corpus: list[tuple[str, int, list[str]]], query: str) -> None:
    store = need("reader.search.store")
    await _build(w, corpus)
    for owner in ("ivan", "anna"):
        uid = w.users[owner]
        own = {str(r[0]) for r in await _rows(
            w, "SELECT locator FROM search_span_vectors WHERE owner_id = :o", o=uid)}
        found = await _candidates(w, owner, query)
        fused = await _candidates(w, owner, query, use_rrf=True)
        result = await store.search(w.engine, uid, query, k=50)
        emitted = {h.locator for h in [*found.hits, *fused.hits]} | {h.span_id for h in result.results}
        assert emitted <= own, owner


@PROPS
@given(corpus=corpora, query=queries)
async def test_citations_resolve(w: World, corpus: list[tuple[str, int, list[str]]], query: str) -> None:
    from reader.ingest import views

    store = need("reader.search.store")
    for owner in await _build(w, corpus):
        uid = w.users[owner]
        found = await _candidates(w, owner, query)
        result = await store.search(w.engine, uid, query, k=50)
        async with w.engine.connect() as conn:
            texts = await views.active_span_texts(conn, uid, [h.locator for h in found.hits])
        assert set(texts) == {h.locator for h in found.hits}
        for hit in result.results:
            page, body = texts[hit.span_id]
            assert hit.page == page and hit.span_ids == [hit.span_id] and body.startswith(hit.snippet)


@PROPS
@given(corpus=corpora, query=queries)
async def test_order_is_total_and_deterministic(
    w: World, corpus: list[tuple[str, int, list[str]]], query: str
) -> None:
    await _build(w, corpus)
    for kw in ({}, {"use_rrf": True}):
        assert await _candidates(w, "ivan", query, **kw) == await _candidates(w, "ivan", query, **kw)
    plain = await _candidates(w, "ivan", query)
    keys = [(-round(h.score, 9), h.locator) for h in plain.hits]
    assert keys == sorted(keys)  # equal distances fall back to the locator


operator_text = st.lists(
    st.sampled_from([*"&|!:*<->()'\\ \t", "block ", "miner ", "wallet ", "key "]), max_size=20
).map("".join)


@PROPS
@given(corpus=corpora, query=operator_text)
async def test_full_text_matches_shared_lexemes_and_never_raises(
    w: World, corpus: list[tuple[str, int, list[str]]], query: str
) -> None:
    store = need("reader.search.store")
    errors = need("reader.search.errors")
    await _build(w, corpus)
    uid = w.users["ivan"]

    async def down(_query: str) -> list[float]:
        raise errors.EmbedUnavailable()

    real = store.embed_query
    store.embed_query = down  # the degraded path: the full-text list alone
    try:
        found = await _candidates(w, "ivan", query)
    finally:
        store.embed_query = real
    expected = {
        str(r[0])
        for r in await _rows(
            w,
            "SELECT locator FROM search_span_vectors WHERE owner_id = :o"
            " AND tsvector_to_array(fts) && tsvector_to_array(to_tsvector('english', :q))",
            o=uid,
            q=query,
        )
    }
    assert {h.locator for h in found.hits} == expected
    assert found.degraded
