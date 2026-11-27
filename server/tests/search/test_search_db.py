"""SRV-SEARCH [db]: tables, the embed job on vector overlays, full-text configuration, candidates, the legacy
search(), the route, status and ensure (ADR-0039 revision, phase 2 unit 4b).

@trace FR-SEARCH-4 FR-SEARCH-5 FR-SEARCH-6 FR-SEARCH-7 FR-SEARCH-8 FR-SEARCH-9
@trace INV-SEARCH-3 INV-SEARCH-5 VEC-SEARCH-5
"""

from __future__ import annotations

import logging
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
    embed_args,
    embed_material,
    fixture,
    need,
    run_embed,
    scalar,
    world,
)

pytestmark = pytest.mark.anyio

FILL_RU = " Здесь больше ничего не сказано ни о чём другом, это просто заполнитель для абзаца."
FILL_EN = " Nothing further is said here about anything else in this paragraph."
SPV = "SELECT count(*) FROM search_span_vectors"

RU_SPANS = [
    (1, "Блокчейн хранит цепочку блоков, каждый блок ссылается на предыдущий по хешу."),
    (1, "Майнер проверяет транзакции и получает награду за новый блок."),
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


async def _head_vectors(w: World, handle: str, sha: str) -> str | None:
    from reader.ingest import manifest, views

    async with w.engine.connect() as conn:
        pack = await views.active_pack(conn, w.users[handle], sha)
        view = await manifest.active_view(conn, w.users[handle])
    assert pack is not None
    named = {e["pack_id"]: e["overlays"] for e in view.entry["packs"]}
    value = named[pack.pack_id].get("vectors")
    return None if value is None else str(value)


async def _raises(w: World, args: Any) -> str:
    errors = need("reader.search.errors")
    with pytest.raises(errors.SearchError) as raised:
        await run_embed(w, args)
    return str(raised.value.code)


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
    indexes = "SELECT count(*) FROM pg_indexes WHERE tablename = :t AND indexdef ILIKE :p"
    for table in ("search_chunks", "search_span_vectors"):
        assert await scalar(w.engine, indexes, t=table, p="%gin%") == 1
        # declared deviation: an exact scan, no vector index
        assert await scalar(w.engine, indexes, t=table, p="%hnsw%") == 0


# ---- FR-SEARCH-5, -6, INV-SEARCH-3: the vector overlay build ----------------------------------------------


async def test_a_build_seals_one_overlay_and_activates_it(
    w: World, embed_server: EmbedServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reader.ingest import manifest

    calls: list[tuple[str, Any]] = []
    real_request, real_try = manifest.request_activation, manifest.try_activate

    async def request(conn: Any, owner: Any, pid: str, named: Any) -> None:
        calls.append(("request", dict(named)))
        await real_request(conn, owner, pid, named)

    async def try_(conn: Any, owner: Any, pid: str) -> Any:
        calls.append(("try", pid))
        return await real_try(conn, owner, pid)

    monkeypatch.setattr(manifest, "request_activation", request)
    monkeypatch.setattr(manifest, "try_activate", try_)
    ingest = await add_material(w, "ivan", MAT_RU, RU_SPANS)
    args = await embed_args(w, w.users["ivan"], MAT_RU)
    assert set(args.model_dump()) == {"owner_id", "pack_id", "overlay_id"}
    jobs_before = await scalar(w.engine, "SELECT count(*) FROM job_keys")
    await run_embed(w, args)
    assert calls == [("request", {"vectors": args.overlay_id}), ("try", args.pack_id)]
    assert await scalar(w.engine, SPV + " WHERE overlay_id = :v AND owner_id = :o", v=args.overlay_id,
                        o=w.users["ivan"]) == 4  # four short spans, one window each
    assert await scalar(w.engine, "SELECT DISTINCT fts_config FROM search_span_vectors") == "russian"
    state = "SELECT state || ' ' || kind FROM overlays WHERE overlay_id = :v"
    assert await scalar(w.engine, state, v=args.overlay_id) == "sealed vectors"
    assert await _head_vectors(w, "ivan", MAT_RU) == args.overlay_id
    assert await scalar(w.engine, "SELECT count(*) FROM pending_activations") == 0
    assert await scalar(w.engine, "SELECT count(*) FROM job_keys") == jobs_before  # enqueues nothing
    passages = [n for k, n in embed_server.encoder.calls if k == "passage"]
    assert passages and max(passages) <= 8
    # INV-SEARCH-3: a rerun of the key makes no embed call, keeps one row set and repeats the two calls only
    seen = len(embed_server.encoder.calls)
    await run_embed(w, args)
    assert len(embed_server.encoder.calls) == seen
    assert await scalar(w.engine, SPV) == 4
    assert calls[2:] == [("request", {"vectors": args.overlay_id}), ("try", args.pack_id)]
    del ingest


async def test_windows_and_batches(w: World, embed_server: EmbedServer) -> None:
    long = " ".join(f"слово{k}" for k in range(400))[:2500]
    spans = [(p, f"страница {p}." + FILL_RU) for p in range(1, 11)] + [(11, long), (12, "")]
    await _ingested(w, spans=spans)
    windows = "SELECT char_start, char_stop FROM search_span_vectors WHERE window_no > 0 OR char_stop >= 1200"
    async with w.engine.connect() as conn:
        rows = sorted(tuple(r) for r in await conn.execute(text(windows)))
    assert rows == [(0, 1200), (1000, 2200), (2000, 2500)]  # the long span's windows; the empty span has none
    assert await scalar(w.engine, SPV) == 13
    assert [n for k, n in embed_server.encoder.calls if k == "passage"] == [8, 5]


async def test_two_owners_of_the_same_bytes_build_their_own_rows(w: World) -> None:
    store = need("reader.search.store")
    await _ingested(w, "ivan")
    await _ingested(w, "anna")
    ivan = await embed_args(w, w.users["ivan"], MAT_RU)
    anna = await embed_args(w, w.users["anna"], MAT_RU)
    assert (ivan.pack_id, ivan.overlay_id) == (anna.pack_id, anna.overlay_id)
    by_owner = "SELECT count(*) FROM search_span_vectors WHERE owner_id = :o"
    assert await scalar(w.engine, by_owner, o=w.users["ivan"]) == 4
    assert await scalar(w.engine, by_owner, o=w.users["anna"]) == 4
    async with w.engine.begin() as conn:
        assert await store.forget_overlay(conn, w.users["anna"], anna.overlay_id) == 4
    assert await scalar(w.engine, by_owner, o=w.users["ivan"]) == 4


async def test_predicate_failures_are_overlay_superseded(w: World) -> None:
    from ingest.packseed import R3, add_pack

    jobs = need("reader.search.jobs")
    await add_material(w, "ivan", MAT_RU, RU_SPANS)
    owner = w.users["ivan"]
    head = await embed_args(w, owner, MAT_RU)
    wrong = jobs.EmbedArgs(owner_id=owner, pack_id=head.pack_id, overlay_id="f" * 64)
    assert await _raises(w, wrong) == "overlay_superseded"
    async with w.engine.begin() as conn:  # a newer sealed pack outside the head entry
        newer = await add_pack(conn, owner, MAT_RU, R3, [(1, "text", "Новый текст про блок." + FILL_RU)])
        assert (await jobs.buildable_pack(conn, owner, MAT_RU)).pack_id == head.pack_id
        newer_args = await jobs.args_for(conn, owner, newer)
    assert await _raises(w, newer_args) == "overlay_superseded"
    assert await scalar(w.engine, SPV) == 0
    await run_embed(w, head)  # the head entry's pack is buildable although a newer sealed pack exists
    assert await scalar(w.engine, SPV) == 4
    async with w.engine.begin() as conn:  # a collected overlay id is never built again
        await conn.execute(text("UPDATE overlays SET collected_at = now() WHERE overlay_id = :v"),
                           {"v": head.overlay_id})
    assert await _raises(w, head) == "overlay_superseded"


async def test_newest_live_pack_by_converter_order_outside_the_head(w: World) -> None:
    from ingest.packseed import R2, R3, add_pack

    jobs = need("reader.search.jobs")
    ingest = await add_material(w, "ivan", MAT_EN, [(1, "Miners validate blocks." + FILL_EN)],
                                config_id="book-en")
    owner = w.users["ivan"]
    async with w.engine.begin() as conn:
        newer = await add_pack(conn, owner, MAT_EN, R3, [(1, "text", "Blocks link by hash." + FILL_EN)])
        from reader.ingest import manifest

        await manifest.remove_material(conn, owner, MAT_EN)  # no pack of the material in the head entry
        await conn.execute(text("UPDATE materials SET deleted_at = NULL WHERE owner_id = :o"), {"o": owner})
        pack = await jobs.buildable_pack(conn, owner, MAT_EN)
    assert pack is not None and pack.pack_id == newer and pack.converter_version == R3 != R2
    del ingest


async def test_try_activate_refusals_roll_back_and_waits_commit(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reader.ingest import manifest

    await add_material(w, "ivan", MAT_RU, RU_SPANS)
    args = await embed_args(w, w.users["ivan"], MAT_RU)
    real = manifest.try_activate
    for refusal in ("pack_purged", "pack_collected", "not_sealed"):
        async def refused(conn: Any, owner: Any, pid: str, code: str = refusal) -> str:
            return code

        monkeypatch.setattr(manifest, "try_activate", refused)
        assert await _raises(w, args) == "overlay_superseded"
        assert await scalar(w.engine, SPV) == 0
        assert await scalar(w.engine, "SELECT count(*) FROM overlays WHERE kind = 'vectors'") == 0
        assert await scalar(w.engine, "SELECT count(*) FROM pending_activations") == 0

    async def waiting(conn: Any, owner: Any, pid: str) -> str:
        await real(conn, owner, pid)
        return "seqmap_missing"

    monkeypatch.setattr(manifest, "try_activate", waiting)
    await run_embed(w, args)
    assert await scalar(w.engine, SPV) == 4
    state = "SELECT state FROM overlays WHERE overlay_id = :v"
    assert await scalar(w.engine, state, v=args.overlay_id) == "sealed"  # a waiting reason commits


async def test_a_purge_committed_during_the_build_leaves_no_row(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reader.search import embed_client

    await add_material(w, "ivan", MAT_RU, RU_SPANS)
    args = await embed_args(w, w.users["ivan"], MAT_RU)
    real = embed_client.embed

    async def embed_and_purge(kind: str, texts: Any, timeout: float) -> Any:  # noqa: ASYNC109
        async with w.engine.begin() as other:  # a second connection commits the purge mid-build
            await other.execute(text("UPDATE base_packs SET purged_at = now() WHERE pack_id = :p"),
                                {"p": args.pack_id})
        return await real(kind, texts, timeout)

    monkeypatch.setattr(embed_client, "embed", embed_and_purge)
    assert await _raises(w, args) == "overlay_superseded"
    assert await scalar(w.engine, SPV + " WHERE overlay_id = :v", v=args.overlay_id) == 0


async def test_fts_config_comes_from_the_pack_profile(w: World) -> None:
    from ingest.packseed import R3, add_pack
    from reader.ingest import overlays

    jobs = need("reader.search.jobs")
    await add_material(w, "ivan", MAT_RU, RU_SPANS)  # the head entry's profile: ru
    await add_material(w, "anna", MAT_RU, RU_SPANS, config_id="no-entry")  # no profile at all: simple
    owner = w.users["ivan"]
    async with w.engine.begin() as conn:
        head = (await jobs.buildable_pack(conn, owner, MAT_RU)).pack_id
        assert await jobs.fts_config_of(conn, owner, head) == "russian"
        assert await jobs.fts_config_of(conn, w.users["anna"], head) == "simple"
        newer = await add_pack(conn, owner, MAT_RU, R3, [(1, "text", "Новый текст." + FILL_RU)])
        await overlays.build_profile(conn, owner, newer, {"language": "en"})  # the import's, never activated
        assert await jobs.fts_config_of(conn, owner, newer) == "english"


async def test_a_language_change_is_a_new_overlay_and_the_old_one_serves(w: World) -> None:
    from reader.ingest import manifest, overlays, views

    store = need("reader.search.store")
    await _ingested(w, sha=MAT_EN, spans=[(1, "Miners validate blocks and earn rewards." + FILL_EN)])
    owner = w.users["ivan"]
    first = await embed_args(w, owner, MAT_EN)
    assert await scalar(w.engine, "SELECT DISTINCT fts_config FROM search_span_vectors") == "english"
    async with w.engine.begin() as conn:  # the profile's language changes (as SRV-INGEST's reconcile does)
        pack = await views.active_pack(conn, owner, MAT_EN)
        assert pack is not None
        oid = await overlays.build_profile(conn, owner, pack.pack_id, {"language": "ru"})
        await manifest.request_activation(conn, owner, pack.pack_id, {"profile": oid})
        assert await manifest.try_activate(conn, owner, pack.pack_id) == "appended"
    second = await embed_args(w, owner, MAT_EN)
    assert second.overlay_id != first.overlay_id  # a new vector overlay id; the old overlay stays active
    async with w.engine.connect() as conn:
        found = await store.candidates(conn, owner, "miners", limit=50)
    assert found.hits and await _head_vectors(w, "ivan", MAT_EN) == first.overlay_id
    await run_embed(w, second)
    assert await _head_vectors(w, "ivan", MAT_EN) == second.overlay_id
    assert await scalar(w.engine, "SELECT fts_config FROM search_span_vectors WHERE overlay_id = :v LIMIT 1",
                        v=second.overlay_id) == "russian"


# ---- FR-SEARCH-7: candidates and the legacy search() -------------------------------------------------------


async def test_candidates_are_dense_spans_of_active_packs(w: World) -> None:
    store = need("reader.search.store")
    await _ingested(w)
    async with w.engine.connect() as conn:
        found = await store.candidates(conn, w.users["ivan"], "кошелёк ключи", limit=50)
    assert not found.degraded and found.hits
    top = found.hits[0]
    assert top.locator.startswith(f"spn:{MAT_RU}:p2:")
    assert [h.dense_rank for h in found.hits] == list(range(1, len(found.hits) + 1))
    assert all(h.fts_rank is None for h in found.hits)
    assert [h.score for h in found.hits] == sorted((h.score for h in found.hits), reverse=True)
    assert "l:ключ" in top.covers and found.lexemes >= {"ключ"}


async def test_a_span_is_scored_by_its_best_window(w: World) -> None:
    store = need("reader.search.store")
    params = need("reader.search.params")
    filler = " ".join(f"шум{k}" for k in range(300))[:2300]
    await _ingested(w, spans=[(1, filler + " кошелёк ключи"), (2, "Другой текст про блок." + FILL_RU)])
    async with w.engine.connect() as conn:
        found = await store.candidates(conn, w.users["ivan"], "кошелёк ключи", limit=50)
        vector = await store.embed_query("кошелёк ключи")
        rows = (await conn.execute(text(
            "SELECT locator, window_no, embedding <=> CAST(:q AS vector) FROM search_span_vectors"),
            {"q": store.vector_literal(vector)})).all()
    best: dict[str, float] = {}
    for loc, _, d in rows:
        best[loc] = min(best.get(loc, 2.0), float(d))
    assert len({r[0] for r in rows}) == 2 and len(rows) > 2 and params.CHUNK_SIZE == 1200  # a long span
    assert {h.locator: h.score for h in found.hits} == pytest.approx({k: 1 - v for k, v in best.items()})
    assert found.hits[0].locator.startswith(f"spn:{MAT_RU}:p1:")


async def test_rrf_and_the_degraded_full_text(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    store = need("reader.search.store")
    await _ingested(w)
    async with w.engine.connect() as conn:
        fused = await store.candidates(conn, w.users["ivan"], "кошелёк ключи", limit=50, use_rrf=True)
    assert any(h.fts_rank is not None for h in fused.hits) and fused.hits[0].dense_rank is not None
    monkeypatch.setenv("READER_EMBED_URL", "http://127.0.0.1:9")
    async with w.engine.connect() as conn:
        down = await store.candidates(conn, w.users["ivan"], "кошелёк ключи", limit=50)
        ranks = {r[0]: float(r[1]) for r in await conn.execute(text(
            "SELECT locator, max(ts_rank_cd(fts, to_tsquery('russian', 'кошелёк | ключи')))"
            " FROM search_span_vectors GROUP BY locator"))}
    assert down.degraded and down.hits
    assert all(h.dense_rank is None for h in down.hits)
    assert [h.score for h in down.hits] == [ranks[h.locator] for h in down.hits]


async def test_filters_and_exclusions_apply_before_the_scan(w: World) -> None:
    from reader.ingest import manifest, overlays, views

    store = need("reader.search.store")
    await _ingested(w)
    await _ingested(w, sha=MAT_EN, spans=[(1, "кошелёк ключи кошелёк ключи" + FILL_RU)])
    owner = w.users["ivan"]
    async with w.engine.begin() as conn:  # tag one Russian span form=definition in an active tags overlay
        pack = await views.active_pack(conn, owner, MAT_RU)
        assert pack is not None
        spans = await views.pack_spans(conn, pack)
        inputs = {"tagger": "test"}
        oid = overlays.overlay_id("tags", pack.pack_id, inputs, "tags/1")
        await overlays.begin(conn, owner, oid, kind="tags", pack_id=pack.pack_id, inputs=inputs,
                             builder_version="tags/1")
        await conn.execute(text(
            "INSERT INTO span_tags (owner_id, overlay_id, pack_id, locator, facet, value, by, confidence,"
            " tagger_version) VALUES (:o, :v, :p, :l, 'form', 'definition', 'owner', 1, 't')"),
            {"o": owner, "v": oid, "p": pack.pack_id, "l": spans[0].locator})
        await overlays.seal(conn, owner, oid)
        seq, top = await manifest.head(conn, owner)
        elements = {e["pack_id"]: dict(e["overlays"]) for e in top["packs"]}
        elements[pack.pack_id]["tags"] = oid
        await manifest.append(conn, owner, seq, manifest.make_entry(elements), "test_fixture")
        en = await views.active_pack(conn, owner, MAT_EN)
        assert en is not None
        form = {"form": "definition"}
        tagged = await store.candidates(conn, owner, "кошелёк ключи", limit=50, filters=form)
        excluded = await store.candidates(conn, owner, "кошелёк ключи", limit=50, exclude_packs=[en.pack_id])
        only = await store.candidates(conn, owner, "кошелёк ключи", limit=50, material_sha256=MAT_EN)
    assert [h.locator for h in tagged.hits] == [spans[0].locator]  # the nearer English span never appears
    assert excluded.hits and {h.material_sha256 for h in excluded.hits} == {MAT_RU}
    assert only.hits and {h.material_sha256 for h in only.hits} == {MAT_EN}


async def test_store_corpus_is_finds_candidates_port(w: World) -> None:
    corpus_mod = need("reader.search.corpus")
    find = need("reader.search.find")
    policy = need("reader.search.policy").policy()
    await _ingested(w)
    caller = find.Caller(w.users["ivan"], ("owner",))
    async with w.engine.connect() as conn:
        deps = find.Deps(corpus=corpus_mod.StoreCorpus(conn, policy), policy=policy,
                         policy_version=lambda: "p" * 64)
        owner = await find.find(caller, "owner", "кошелёк ключи", deps=deps)
        mcp = await find.find(caller, "mcp", "кошелёк ключи", deps=deps)
        corpus = corpus_mod.StoreCorpus(conn, policy)
        route = find.route_for(policy, "owner", "thematic", findings=False)
        found = await corpus.candidates(caller, "owner", route, "кошелёк ключи", {}, ())
        near = await corpus.neighbours(caller, "owner", found.cards[:1])
    assert owner["cards"] and owner["cards"][0]["id"].startswith(f"spn:{MAT_RU}:p2:")
    assert owner["cards"][0]["layer"] == "L1" and owner["degraded"] is False
    assert mcp["cards"] == []  # a personal-use pack's L1 never reaches mcp: the pack is left out of the scan
    top = found.cards[0]
    assert [c.id for c in near[top.id]] == list(top.next) and top.covers
    assert top.text in {s for _, s in RU_SPANS}  # full span text; find serves the cut


async def test_search_returns_locator_hits(w: World) -> None:
    store = need("reader.search.store")
    from reader.ingest import views

    await _ingested(w)
    result = await store.search(w.engine, w.users["ivan"], "кошелёк ключи", k=10)
    assert not result.degraded and result.results
    top = result.results[0]
    assert top.page == 2 and top.span_ids == [top.span_id] and top.span_id.startswith("spn:")
    assert set(top.to_json()) == {"material_sha256", "page", "span_id", "span_ids", "snippet", "score",
                                  "dense_rank", "fts_rank"}
    async with w.engine.connect() as conn:  # every span id is an active span's locator on the hit's page
        texts = await views.active_span_texts(conn, w.users["ivan"], [h.span_id for h in result.results])
    assert [texts[h.span_id][0] for h in result.results] == [h.page for h in result.results]
    assert len(top.snippet) <= 300


# ---- VEC-SEARCH-5, INV-SEARCH-7 over windows --------------------------------------------------------------


async def test_full_text_is_or_over_windows_with_the_query_elements(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = need("reader.search.store")
    data = fixture("fts-or.yaml")
    spans = [(i + 1, c["text"]) for i, c in enumerate(data["spans"])]
    await _ingested(w, sha=MAT_EN, spans=spans)
    monkeypatch.setenv("READER_EMBED_URL", "http://127.0.0.1:9")  # degraded: the full-text list alone
    page_of = {c["id"]: i + 1 for i, c in enumerate(data["spans"])}
    for case in data["cases"]:
        async with w.engine.connect() as conn:
            found = await store.candidates(conn, w.users["ivan"], case["query"], limit=50)
        pages = {int(h.locator.split(":p")[1].split(":")[0]) for h in found.hits}
        assert pages == {page_of[c] for c in case["expected_ids"]}, case["name"]
        assert found.lexemes == frozenset(case["lexemes"]), case["name"]
        covers = {page_of[c]: {f"l:{x}" for x in xs} for c, xs in case["covers"].items()}
        assert {int(h.locator.split(":p")[1].split(":")[0]): set(h.covers) for h in found.hits} == covers


OPERATOR_QUERIES = ["&", "|", "!", ":*", "<->", "'", "\\", "a & b | !c", "''\\''", "x:* <2> y"]


@pytest.mark.parametrize("query", OPERATOR_QUERIES)
async def test_operator_strings_never_raise(w: World, query: str, monkeypatch: pytest.MonkeyPatch) -> None:
    store = need("reader.search.store")
    await _ingested(w, sha=MAT_EN, spans=[(1, "a b c x y" + FILL_EN)])
    for use_rrf in (False, True):
        async with w.engine.connect() as conn:
            await store.candidates(conn, w.users["ivan"], query, limit=50, use_rrf=use_rrf)
    monkeypatch.setenv("READER_EMBED_URL", "http://127.0.0.1:9")
    async with w.engine.connect() as conn:
        await store.candidates(conn, w.users["ivan"], query, limit=50)


# ---- FR-SEARCH-8, INV-SEARCH-5 ----------------------------------------------------------------------------


async def test_route_returns_results_for_the_owner(w: World) -> None:
    await _ingested(w)
    await _ingested(w, sha=MAT_EN, spans=[(1, "кошелёк ключи по-английски" + FILL_RU)])
    async with client(w) as c:
        r = await c.post("/api/search", json={"q": "кошелёк ключи", "k": 3})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["degraded"] is False and 1 <= len(body["results"]) <= 3
        assert set(body["results"][0]) == {
            "material_sha256", "page", "span_id", "span_ids", "snippet", "score", "dense_rank", "fts_rank"
        }
        only = await c.post("/api/search", json={"q": "кошелёк ключи", "k": 50, "material": MAT_RU})
        assert {x["material_sha256"] for x in only.json()["results"]} == {MAT_RU}
        unknown = await c.post("/api/search", json={"q": "кошелёк", "material": "c" * 64})
        assert (unknown.status_code, unknown.json()) == (404, {"error": "not_found"})


async def test_route_is_owner_only_and_validates(w: World) -> None:
    async with client(w, "anna") as c:
        assert (await c.post("/api/search", json={"q": "x"})).status_code == 404
    async with client(w) as c:
        for body in ({"q": ""}, {"q": "x" * 1001}, {"q": "x", "k": 0}, {"q": "x", "k": 51},
                     {"q": "x", "material": "zz"}, {"k": 3}, [1]):
            r = await c.post("/api/search", json=body)
            assert r.status_code == 400, body
            assert set(r.json()) == {"error"}


async def test_a_broken_reader_yaml_no_longer_blocks_search(w: World) -> None:
    await _ingested(w)
    async with client(w) as c:
        w.config_path.write_text("version: 1\nusers: [ : broken\n", encoding="utf-8")
        r = await c.post("/api/search", json={"q": "кошелёк"})
        assert r.status_code == 200 and r.json()["results"]


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
        async with w.engine.begin() as conn:  # a forced SQL error with the canary in the bound parameters
            await conn.execute(text("ALTER TABLE search_span_vectors RENAME COLUMN fts TO fts_gone"))
        failed = await c.post("/api/search", json={"q": f"{canary} блок"})
        assert failed.status_code >= 500
    assert canary not in caplog.text
    for record in caplog.records:
        assert canary not in record.getMessage()
        assert canary not in repr(record.args)


# ---- FR-SEARCH-9 ------------------------------------------------------------------------------------------


@asynccontextmanager
async def _queue(w: World, pg: Pg) -> AsyncIterator[Any]:
    jobs = need("reader.search.jobs")
    from reader.config import Secret
    from reader.db import DbTarget
    from reader.jobs.catalog import Catalog
    from reader.jobs.queue import Jobs
    from reader.jobs.worker import make_app

    app = make_app(DbTarget(pg.host, pg.port, pg.user, pg.database, Secret(pg.password)))
    await app.open_async()
    try:
        yield Jobs(w.engine, app, Catalog(jobs.JOB_KINDS))
    finally:
        await app.close_async()


async def _kill(w: World, key: str) -> None:
    async with w.engine.begin() as conn:
        for sql in ("UPDATE job_keys SET outcome = 'dead', attempts = 3 WHERE key = :k",
                    "UPDATE procrastinate_jobs SET status = 'failed' WHERE lock = :k"):
            await conn.execute(text(sql), {"k": key})


async def test_status_and_ensure_on_overlay_keys(w: World, pg: Pg) -> None:
    jobs = need("reader.search.jobs")
    owner = w.users["ivan"]
    async with _queue(w, pg) as queue:
        assert await jobs.search_status(queue, owner, MAT_RU) == "none"
        assert await jobs.ensure(queue, owner, MAT_RU) == "no_ingest"
        await add_material(w, "ivan", MAT_RU, RU_SPANS)
        args = await embed_args(w, owner, MAT_RU)
        assert await jobs.ensure(queue, owner, MAT_RU) == "queued"
        key = jobs.job_key(args)
        assert await scalar(w.engine, "SELECT args FROM job_keys WHERE key = :k", k=key) == args.model_dump(
            mode="json")
        await _kill(w, key)
        assert await jobs.search_status(queue, owner, MAT_RU) == "dead"
        assert await jobs.ensure(queue, owner, MAT_RU) == "queued"  # a dead key is requeued, attempts reset
        assert await scalar(w.engine, "SELECT attempts FROM job_keys WHERE key = :k", k=key) == 0
        await run_embed(w, args)
        assert await jobs.search_status(queue, owner, MAT_RU) == "ready"
        async with w.engine.begin() as conn:
            await conn.execute(text("UPDATE overlays SET collected_at = now() WHERE overlay_id = :v"),
                               {"v": args.overlay_id})
        assert await jobs.search_status(queue, owner, MAT_RU) == "collected"


async def test_worker_start_enqueues_buildable_packs_without_a_sealed_overlay(w: World, pg: Pg) -> None:
    from reader.ingest import manifest

    jobs = need("reader.search.jobs")
    async with _queue(w, pg) as queue:
        await add_material(w, "ivan", MAT_RU, RU_SPANS)  # never enqueued: enqueued at start
        await _ingested(w, "ivan", MAT_EN, [(1, "Miners validate blocks." + FILL_EN)])  # sealed: skipped
        await add_material(w, "anna", MAT_RU, RU_SPANS)  # a dead key: left dead
        await add_material(w, "anna", MAT_EN, RU_SPANS, config_id="book-en")  # deleted material: skipped
        dead = await embed_args(w, w.users["anna"], MAT_RU)
        assert await jobs.ensure(queue, w.users["anna"], MAT_RU) == "queued"
        await _kill(w, jobs.job_key(dead))
        async with w.engine.begin() as conn:
            await manifest.remove_material(conn, w.users["anna"], MAT_EN)
            deleted = "UPDATE materials SET deleted_at = now() WHERE owner_id = :o AND sha256 = :s"
            await conn.execute(text(deleted), {"o": w.users["anna"], "s": MAT_EN})
        count = "SELECT count(*) FROM job_keys WHERE kind = 'search.embed'"
        await jobs.on_worker_start(queue)
        assert await jobs.search_status(queue, w.users["ivan"], MAT_RU) == "queued"
        assert await jobs.search_status(queue, w.users["ivan"], MAT_EN) == "ready"
        assert await jobs.search_status(queue, w.users["anna"], MAT_RU) == "dead"
        assert await scalar(w.engine, count) == 2
        await jobs.on_worker_start(queue)  # a second start adds nothing
        assert await scalar(w.engine, count) == 2


# ---- code review of 0eddd7d (blocking 1, 3) ----


async def test_an_uncommitted_pack_is_not_ready_and_the_import_requeues_a_dead_key(
    w: World, pg: Pg, embed_server: EmbedServer
) -> None:
    jobs, errors = need("reader.search.jobs"), need("reader.search.errors")
    owner = w.users["ivan"]
    missing = jobs.EmbedArgs(owner_id=owner, pack_id="e" * 64, overlay_id="f" * 64)
    with pytest.raises(errors.IngestNotReady):  # its import rolled back or has not committed
        await run_embed(w, missing)
    async with _queue(w, pg) as queue:
        await add_material(w, "ivan", MAT_RU, RU_SPANS)
        args = await embed_args(w, owner, MAT_RU)
        assert (await jobs.enqueue_current(queue, args)).kind == "queued"
        await _kill(w, jobs.job_key(args))  # the key died behind the rolled-back import
        await jobs.enqueue_current(queue, args)  # the next successful import of the same pack
        assert await jobs.search_status(queue, owner, MAT_RU) == "queued"
    assert embed_server.encoder.calls == []


async def test_refusals_make_no_embed_call(w: World, embed_server: EmbedServer) -> None:
    jobs = need("reader.search.jobs")
    await add_material(w, "ivan", MAT_RU, RU_SPANS)
    owner = w.users["ivan"]
    head = await embed_args(w, owner, MAT_RU)
    wrong = jobs.EmbedArgs(owner_id=owner, pack_id=head.pack_id, overlay_id="f" * 64)
    assert await _raises(w, wrong) == "overlay_superseded"
    assert embed_server.encoder.calls == []  # the overlay-id refusal embeds nothing
    await run_embed(w, head)
    calls = len(embed_server.encoder.calls)
    async with w.engine.begin() as conn:
        await conn.execute(text("UPDATE overlays SET collected_at = now() WHERE overlay_id = :v"),
                           {"v": head.overlay_id})
    assert await _raises(w, head) == "overlay_superseded"
    assert len(embed_server.encoder.calls) == calls and calls > 0  # none after the first build


async def test_newest_by_converter_order_where_pack_ids_disagree(w: World) -> None:
    from ingest.packseed import R2, R3, add_pack
    from reader.ingest import PARSE_CONFIGS, manifest
    from reader.ingest.artifact import pack_id

    jobs = need("reader.search.jobs")
    sha = next(s for s in (c * 64 for c in "0123456789cdef")
               if pack_id(s, dict(PARSE_CONFIGS[R3])) < pack_id(s, dict(PARSE_CONFIGS[R2])))
    await add_material(w, "ivan", sha, [(1, "Miners validate blocks." + FILL_EN)], config_id="book-x")
    owner = w.users["ivan"]
    async with w.engine.begin() as conn:
        newer = await add_pack(conn, owner, sha, R3, [(1, "text", "Blocks link by hash." + FILL_EN)])
        await manifest.remove_material(conn, owner, sha)
        await conn.execute(text("UPDATE materials SET deleted_at = NULL WHERE owner_id = :o"), {"o": owner})
        pack = await jobs.buildable_pack(conn, owner, sha)
    assert pack is not None and pack.pack_id == newer  # R3 wins although its pack id sorts first


async def test_sealed_or_collected_current_overlays_are_left_alone(w: World, pg: Pg) -> None:
    jobs = need("reader.search.jobs")
    owner = w.users["ivan"]
    keys = "SELECT count(*) FROM job_keys WHERE kind = 'search.embed'"
    async with _queue(w, pg) as queue:
        await add_material(w, "ivan", MAT_RU, RU_SPANS)
        args = await embed_args(w, owner, MAT_RU)
        await run_embed(w, args)  # built and sealed without a job: the job status stays none
        assert await jobs.ensure(queue, owner, MAT_RU) == "ready"
        assert await scalar(w.engine, keys) == 0
        async with w.engine.begin() as conn:
            await conn.execute(text("UPDATE overlays SET collected_at = now() WHERE overlay_id = :v"),
                               {"v": args.overlay_id})
        await jobs.on_worker_start(queue)  # the current overlay was collected: skipped although none
        await jobs.ensure(queue, owner, MAT_RU)
        assert await scalar(w.engine, keys) == 0
