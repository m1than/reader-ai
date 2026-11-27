"""Candidates over the owner's active vector overlays (SRV-SEARCH FR-SEARCH-7, -20), the legacy `search()` and
the forget calls (FR-SEARCH-5, -21).

Dense: an exact scan by cosine distance over the windows of each active pack's vector overlay (declared
deviation: no vector index), top `limit` windows, a span scored by its best window. Full text: the OR of the
query's own lexemes, quoted, never operators parsed from user text. Both lists are totally ordered. Only
packs in the owner's head entry are read, so a deleted or replaced pack is never ranked. Nothing here logs a
query, a filter value or a span text.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from reader.ingest import manifest, views
from reader.search import embed_client, params
from reader.search.errors import EmbedUnavailable, SearchError
from reader.search.fusion import rrf, snippet, tsquery_text

log = logging.getLogger("reader.search")


async def material_active(engine: AsyncEngine, owner_id: uuid.UUID, material_sha256: str) -> bool:
    """FR-SEARCH-20: the material has a pack in the owner's head entry (else its filter answers 404)."""
    async with engine.connect() as conn:
        return await views.active_pack(conn, owner_id, material_sha256) is not None


async def forget_legacy(conn: AsyncConnection, owner_id: uuid.UUID, ingest_id: uuid.UUID) -> int:
    """FR-SEARCH-21: the owner's chunk and embed rows of one ingest, deleted on the caller's connection
    (SRV-INGEST's GC and purge call it in their transaction); returns the number of rows deleted."""
    count = 0
    for table in ("search_chunks", "search_embeds"):  # constants only
        done = await conn.execute(
            text(f"DELETE FROM {table} WHERE owner_id = :o AND ingest_id = :i"),  # noqa: S608
            {"o": owner_id, "i": ingest_id},
        )
        count += int(done.rowcount)
    return count


async def forget_overlay(conn: AsyncConnection, owner_id: uuid.UUID, overlay_id: str) -> int:
    """FR-SEARCH-5: the owner's span-vector rows of one overlay, deleted on the caller's connection
    (SRV-INGEST's GC and purge, under their locks); the only writer of `reader.search_delete`, which it
    switches off again."""
    await conn.execute(text("SELECT set_config('reader.search_delete', 'on', true)"))
    done = await conn.execute(
        text("DELETE FROM search_span_vectors WHERE owner_id = :o AND overlay_id = :v"),
        {"o": owner_id, "v": overlay_id},
    )
    await conn.execute(text("SELECT set_config('reader.search_delete', 'off', true)"))
    return int(done.rowcount)


async def embed_facts(conn: AsyncConnection, owner_id: uuid.UUID, ingest_id: Any) -> tuple[int, Any]:
    """SRV-OBS FR-OBS-20's ingest Run (paired): the window rows of the ingest's pack's newest sealed vector
    overlay and that overlay's sealed_at; (0, None) without one."""
    pack = await views.pack_of_ingest(conn, ingest_id)
    if pack is None or pack.owner_id != owner_id:
        return 0, None
    row = (await conn.execute(text(
        "SELECT o.overlay_id, o.sealed_at FROM overlays o WHERE o.owner_id = :o AND o.pack_id = :p"
        " AND o.kind = 'vectors' AND o.state = 'sealed' ORDER BY o.sealed_at DESC, o.overlay_id LIMIT 1"),
        {"o": owner_id, "p": pack.pack_id})).first()
    if row is None:
        return 0, None
    count = (await conn.execute(text(
        "SELECT count(*) FROM search_span_vectors WHERE owner_id = :o AND overlay_id = :v"),
        {"o": owner_id, "v": row[0]})).scalar()
    return int(count or 0), row[1]


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


async def embed_query(query: str) -> list[float]:
    (vector,) = await embed_client.embed("query", [query], params.QUERY_TIMEOUT_S)
    return vector


# ---- candidates (FR-SEARCH-7) ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpanHit:
    locator: str
    pack_id: str
    material_sha256: str
    score: float
    dense_rank: int | None
    fts_rank: int | None
    covers: frozenset[str]  # FR-SEARCH-25 element keys `l:<lexeme>` the span shares with the query


@dataclass(frozen=True)
class Found:
    hits: list[SpanHit]
    degraded: bool
    lexemes: frozenset[str]
    brain_version: str


@dataclass(frozen=True)
class Scope:
    """The active vector overlays a query reads (overlay id → (pack id, material sha256)) and the tag overlays
    its facet filters read."""

    overlays: dict[str, tuple[str, str]]
    tags: list[str]

    @property
    def ids(self) -> list[str]:
        return sorted(self.overlays)


async def scope(conn: AsyncConnection, owner_id: uuid.UUID, *, material_sha256: str | None = None,
                exclude_packs: Sequence[str] = ()) -> tuple[Scope, str]:
    """The owner's head entry: its packs' vector overlays (optionally one material's, no excluded pack)."""
    view = await manifest.active_view(conn, owner_id)
    sha_of = {p.pack_id: sha for sha, p in (await views.active_packs(conn, owner_id)).items()}
    found: dict[str, tuple[str, str]] = {}
    tags: list[str] = []
    for element in view.entry["packs"]:
        pid, named = element["pack_id"], element["overlays"]
        sha = sha_of.get(pid)
        if sha is None or pid in exclude_packs or (material_sha256 is not None and sha != material_sha256):
            continue
        if named.get("vectors"):
            found[str(named["vectors"])] = (pid, sha)
        if named.get("tags"):
            tags.append(str(named["tags"]))
    return Scope(found, sorted(tags)), view.brain_version


def _where(filters: Mapping[str, str]) -> tuple[str, dict[str, Any]]:
    """The scope and the facet filters, applied in SQL before any ranking (each filter: a tag of the span in
    an active tags overlay). Only constants and numbered parameter names enter the text."""
    sql = " WHERE v.owner_id = :o AND v.overlay_id = ANY(:ov)"
    bound: dict[str, Any] = {}
    for n, (facet, value) in enumerate(sorted(filters.items())):
        sql += (
            " AND EXISTS (SELECT 1 FROM span_tags t WHERE t.owner_id = v.owner_id"  # noqa: S608
            " AND t.overlay_id = ANY(:tov) AND t.locator = v.locator"
            f" AND t.facet = :f{n} AND t.value = :x{n})"
        )
        bound[f"f{n}"], bound[f"x{n}"] = facet, value
    return sql, bound


def _best(rows: Sequence[Any], lower: bool) -> list[tuple[str, float]]:
    """(locator, value) per window in list order → one per span, its best window, in (best, locator) order."""
    best: dict[str, float] = {}
    for loc, value in rows:
        if loc not in best or (value < best[loc] if lower else value > best[loc]):
            best[loc] = float(value)
    return sorted(best.items(), key=lambda kv: (kv[1] if lower else -kv[1], kv[0]))


async def _lexemes(conn: AsyncConnection, configs: Sequence[str], query: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for config in configs:
        if config in params.FTS_CONFIGS:
            sql = "SELECT tsvector_to_array(to_tsvector(CAST(:cfg AS regconfig), :q))"
            got = (await conn.execute(text(sql), {"cfg": config, "q": query})).scalar_one()
            out[config] = sorted(got or [])
    return out


async def candidates(
    conn: AsyncConnection,
    owner_id: uuid.UUID,
    query: str,
    *,
    limit: int,
    use_rrf: bool = False,
    rrf_k: int = 60,
    w_dense: float = 5.0,
    material_sha256: str | None = None,
    exclude_packs: Sequence[str] = (),
    filters: Mapping[str, str] | None = None,
) -> Found:
    """FR-SEARCH-7 step 2: the owner's span candidates; with the embed service down, the full-text list alone,
    `degraded`, each span scored by its best window's ts_rank_cd."""
    where, extra = _where(dict(filters or {}))
    scoped, brain = await scope(conn, owner_id, material_sha256=material_sha256, exclude_packs=exclude_packs)
    base = {"o": owner_id, "ov": scoped.ids, "tov": scoped.tags, **extra}
    sql = "SELECT DISTINCT v.fts_config FROM search_span_vectors v" + where + " ORDER BY 1"  # noqa: S608
    configs = [str(r[0]) for r in await conn.execute(text(sql), base)]
    lexemes = await _lexemes(conn, configs, query)
    all_lexemes = frozenset(x for xs in lexemes.values() for x in xs)
    degraded = False
    dense: list[tuple[str, float]] = []
    try:
        vector: list[float] | None = await embed_query(query)
    except (EmbedUnavailable, SearchError, httpx.HTTPError) as failure:
        log.warning("search degraded: %s", type(failure).__name__)
        vector, degraded = None, True
    overlay_of: dict[str, str] = {}
    if vector is not None and scoped.ids:
        sql = (
            "SELECT v.locator, v.embedding <=> CAST(:q AS vector) AS d, v.overlay_id"  # noqa: S608
            " FROM search_span_vectors v" + where
            + " ORDER BY d, v.overlay_id, v.locator, v.window_no LIMIT :n"
        )
        rows = (await conn.execute(text(sql), {**base, "q": vector_literal(vector), "n": limit})).all()
        overlay_of.update((str(r[0]), str(r[2])) for r in rows)
        dense = _best([(str(r[0]), float(r[1])) for r in rows], lower=True)
    fts: list[tuple[str, float]] = []
    if (use_rrf or vector is None) and scoped.ids:
        windows: list[tuple[str, float]] = []
        for config, words in lexemes.items():
            tsq = tsquery_text(words)
            if not tsq:
                continue
            sql = (
                "SELECT v.locator, ts_rank_cd(v.fts, CAST(:tsq AS tsquery)) AS r, v.overlay_id"  # noqa: S608
                " FROM search_span_vectors v" + where
                + " AND v.fts_config = :cfg AND v.fts @@ CAST(:tsq AS tsquery)"
                " ORDER BY r DESC, v.locator, v.window_no LIMIT :n"
            )
            rows = (await conn.execute(text(sql), {**base, "cfg": config, "tsq": tsq, "n": limit})).all()
            overlay_of.update((str(r[0]), str(r[2])) for r in rows)
            windows.extend((str(r[0]), float(r[1])) for r in rows)
        windows.sort(key=lambda w: (-w[1], w[0]))
        fts = _best(windows[:limit], lower=False)
    ranked: list[tuple[str, float, int | None, int | None]]
    if vector is None:
        ranked = [(loc, value, None, i + 1) for i, (loc, value) in enumerate(fts)]
    elif use_rrf:
        fused = rrf([d[0] for d in dense], [x[0] for x in fts], k=rrf_k, w_dense=w_dense)
        ranked = [(f.id, f.score, f.dense_rank, f.fts_rank) for f in fused]
    else:
        ranked = [(loc, 1.0 - value, i + 1, None) for i, (loc, value) in enumerate(dense)]
    covers = await _covers(conn, base, where, [r[0] for r in ranked], sorted(all_lexemes))
    hits = []
    for loc, score, d, f in ranked:
        pid, sha = scoped.overlays[overlay_of[loc]]
        hits.append(SpanHit(loc, pid, sha, float(score), d, f, covers.get(loc, frozenset())))
    return Found(hits, degraded, all_lexemes, brain)


async def _covers(conn: AsyncConnection, base: dict[str, Any], where: str, locators: list[str],
                  lexemes: list[str]) -> dict[str, frozenset[str]]:
    if not locators or not lexemes:
        return {}
    sql = (
        "SELECT v.locator, array_agg(DISTINCT x) FROM search_span_vectors v"  # noqa: S608
        " CROSS JOIN LATERAL unnest(tsvector_to_array(v.fts)) AS x" + where
        + " AND v.locator = ANY(:locs) AND x = ANY(:lex) GROUP BY v.locator"
    )
    rows = await conn.execute(text(sql), {**base, "locs": locators, "lex": lexemes})
    return {str(r[0]): frozenset(f"l:{x}" for x in r[1]) for r in rows}


# ---- the legacy search() for SRV-TUTOR until V.kl-tutor-find (FR-SEARCH-7 compatibility) ----------------


@dataclass(frozen=True)
class Hit:
    material_sha256: str
    page: int
    span_id: str
    span_ids: list[str]
    snippet: str
    score: float
    dense_rank: int | None
    fts_rank: int | None

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class SearchResult:
    results: list[Hit]
    degraded: bool


async def search(
    engine: AsyncEngine,
    owner_id: uuid.UUID,
    query: str,
    k: int = params.K_DEFAULT,
    material_sha256: str | None = None,
) -> SearchResult:
    """FR-SEARCH-7 compatibility: the owner channel's `thematic` candidates as `Hit` items whose span id is
    the span's locator and whose span ids are [span_id] (a span covers exactly itself)."""
    from reader.search import policy as policy_mod

    loaded = policy_mod.policy()
    data = loaded.data
    route = loaded.route("owner", "thematic")
    async with engine.connect() as conn:
        found = await candidates(
            conn, owner_id, query, limit=data["candidates_max"], use_rrf=bool(route["rrf"]),
            rrf_k=data["rrf_k"], w_dense=float(data["w_dense"]), material_sha256=material_sha256)
        top = found.hits[:k]
        texts = await views.active_span_texts(conn, owner_id, [h.locator for h in top])
    hits = [
        Hit(material_sha256=h.material_sha256, page=texts[h.locator][0], span_id=h.locator,
            span_ids=[h.locator], snippet=snippet(texts[h.locator][1]), score=h.score,
            dense_rank=h.dense_rank, fts_rank=h.fts_rank)
        for h in top if h.locator in texts
    ]
    return SearchResult(hits, found.degraded)
