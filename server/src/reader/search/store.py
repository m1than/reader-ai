"""Search over the owner's current chunks (SRV-SEARCH FR-SEARCH-6, FR-SEARCH-7).

Dense: an exact scan by cosine distance (declared deviation: no vector index at slice scale). Full text: the
OR of the query's own lexemes, quoted, never operators parsed from user text. Fusion: reciprocal rank, k = 60.
Both lists are totally ordered ((distance, id) and (ts_rank_cd desc, id)). "Current" chunks are those of the
newest `search_embeds` row of the lock's model for each ingest. Nothing here logs a query or a chunk text.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from reader.config import load_config
from reader.search import embed_client, params
from reader.search.errors import EmbedUnavailable, SearchError
from reader.search.fusion import rrf, snippet, tsquery_text

log = logging.getLogger("reader.search")
DEFAULT_CONFIG: Final = "/etc/reader/reader.yaml"

# The current key per ingest is computed in Python (it needs reader.yaml's language) and passed as two arrays.
_CURRENT: Final = (
    "WITH cur AS (SELECT * FROM unnest(CAST(:ci AS uuid[]), CAST(:cp AS text[]))"
    " AS cur(ingest_id, params_sha256))"
)
_FILTER: Final = (
    " FROM search_chunks c JOIN cur ON cur.ingest_id = c.ingest_id AND cur.params_sha256 = c.params_sha256"
    " WHERE c.owner_id = :o AND c.model = :m AND (CAST(:mat AS text) IS NULL OR c.material_sha256 = :mat)"
)


@dataclass(frozen=True)
class Hit:
    chunk_id: int
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


def _languages() -> dict[str, str]:
    """reader.yaml's materials: config id → language."""
    config = load_config(Path(os.environ.get("READER_CONFIG", DEFAULT_CONFIG)))
    return {m.id: m.language for m in config.materials}


UPLOAD_PREFIX: Final = "up-"  # SRV-CONFIG FR-CONFIG-17 reserves it for uploads (SRV-INGEST FR-INGEST-23)


async def _upload_language(conn: AsyncConnection, material_sha256: str) -> str | None:
    """FR-SEARCH-10: an uploaded material's language comes from its upload; `other` or no row is `simple`."""
    sql = "SELECT language FROM uploads WHERE sha256 = :s"
    row = (await conn.execute(text(sql), {"s": material_sha256})).first()
    return None if row is None else str(row[0])


async def _fts_config(conn: AsyncConnection, config_id: str | None, sha: str, languages: Any) -> str:
    """`languages` is a callable that loads reader.yaml's languages once; it is never called for an upload."""
    if config_id and config_id.startswith(UPLOAD_PREFIX):
        return params.fts_config_for(await _upload_language(conn, sha))
    return params.fts_config_for(languages().get(config_id or ""))


async def fts_config_of(conn: AsyncConnection, material_sha256: str, ingest_id: UUID | None = None) -> str:
    """FR-SEARCH-6: the material's reader.yaml language through materials.config_id; no entry is `simple`.
    FR-SEARCH-10: an `up-` material reads its upload's language and never reader.yaml."""
    if ingest_id is None:
        sql = "SELECT config_id FROM materials WHERE sha256 = :s ORDER BY id LIMIT 1"
        row = (await conn.execute(text(sql), {"s": material_sha256})).first()
    else:
        sql = "SELECT m.config_id FROM ingests i JOIN materials m ON m.id = i.material_id WHERE i.id = :i"
        row = (await conn.execute(text(sql), {"i": ingest_id})).first()
    if row is None:
        return params.fts_config_for(None)
    return await _fts_config(conn, row[0], material_sha256, _languages)


async def current_keys(conn: AsyncConnection, owner_id: UUID) -> dict[str, Any]:
    """FR-SEARCH-7: per embedded ingest of the owner, the params_sha256 its material would get now."""
    rows = await conn.execute(
        text(
            "SELECT DISTINCT e.ingest_id, m.config_id, m.sha256 FROM search_embeds e"
            " JOIN ingests i ON i.id = e.ingest_id JOIN materials m ON m.id = i.material_id"
            " WHERE e.owner_id = :o AND e.model = :m"
        ),
        {"o": owner_id, "m": params.MODEL_ID},
    )
    loaded: list[dict[str, str]] = []

    def languages() -> dict[str, str]:  # reader.yaml is read once, and only for a declared material
        if not loaded:
            loaded.append(_languages())
        return loaded[0]

    pairs = [(r[0], params.params_sha256(await _fts_config(conn, r[1], r[2], languages))) for r in rows.all()]
    return {"ci": [p[0] for p in pairs], "cp": [p[1] for p in pairs]}


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


async def embed_query(query: str) -> list[float]:
    (vector,) = await embed_client.embed("query", [query], params.QUERY_TIMEOUT_S)
    return vector


async def dense_list(
    conn: AsyncConnection, owner_id: UUID, vector: list[float], material_sha256: str | None
) -> list[int]:
    sql = (
        _CURRENT + "SELECT c.id" + _FILTER
        + " ORDER BY c.embedding <=> CAST(:q AS vector), c.id LIMIT :n"
    )
    rows = await conn.execute(
        text(sql),
        {"o": owner_id, "m": params.MODEL_ID, "mat": material_sha256, "q": vector_literal(vector),
         "n": params.CANDIDATES, **await current_keys(conn, owner_id)},
    )
    return [int(r[0]) for r in rows]


async def fts_list(
    conn: AsyncConnection, owner_id: UUID, query: str, material_sha256: str | None
) -> list[int]:
    """The OR full-text list: per configuration present, the query's lexemes quoted and OR-ed."""
    base = {"o": owner_id, "m": params.MODEL_ID, "mat": material_sha256, **await current_keys(conn, owner_id)}
    present = await conn.execute(text(_CURRENT + "SELECT DISTINCT c.fts_config" + _FILTER), base)
    configs = [r[0] for r in present]
    scored: list[tuple[float, int]] = []
    for config in sorted(configs):
        if config not in params.FTS_CONFIGS:
            continue
        lexemes = (
            await conn.execute(
                text("SELECT tsvector_to_array(to_tsvector(CAST(:cfg AS regconfig), :q))"),
                {"cfg": config, "q": query},
            )
        ).scalar_one()
        tsq = tsquery_text(sorted(lexemes or []))
        if not tsq:
            continue
        sql = (
            _CURRENT + "SELECT c.id, ts_rank_cd(c.fts, CAST(:tsq AS tsquery))" + _FILTER
            + " AND c.fts_config = :cfg AND c.fts @@ CAST(:tsq AS tsquery)"
            " ORDER BY 2 DESC, c.id LIMIT :n"
        )
        rows = await conn.execute(text(sql), {**base, "cfg": config, "tsq": tsq, "n": params.CANDIDATES})
        scored.extend((float(r[1]), int(r[0])) for r in rows)
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [cid for _, cid in scored[: params.CANDIDATES]]


async def search(
    engine: AsyncEngine,
    owner_id: UUID,
    query: str,
    k: int = params.K_DEFAULT,
    material_sha256: str | None = None,
) -> SearchResult:
    """FR-SEARCH-7: weighted-RRF top k (or the dense order alone when params.HYBRID is off); with the embed
    service down, the full-text list alone, `degraded`."""
    degraded = False
    try:
        vector: list[float] | None = await embed_query(query)
    except (EmbedUnavailable, SearchError, httpx.HTTPError) as failure:
        log.warning("search degraded: %s", type(failure).__name__)
        vector, degraded = None, True
    async with engine.connect() as conn:
        dense = await dense_list(conn, owner_id, vector, material_sha256) if vector is not None else []
        use_fts = params.HYBRID or vector is None
        fts = await fts_list(conn, owner_id, query, material_sha256) if use_fts else []
        fused = rrf(dense, fts, w_dense=params.W_DENSE)[:k]
        if not fused:
            return SearchResult([], degraded)
        rows = await conn.execute(
            text(
                "SELECT id, material_sha256, page, span_seqs, primary_seq, text FROM search_chunks"
                " WHERE id = ANY(:ids)"
            ),
            {"ids": [f.id for f in fused]},
        )
        by_id = {int(r[0]): r for r in rows}
    hits = []
    for f in fused:
        row = by_id[f.id]
        sha = str(row[1])
        hits.append(
            Hit(
                chunk_id=f.id,
                material_sha256=sha,
                page=int(row[2]),
                span_id=f"{sha}:{row[4]}",
                span_ids=[f"{sha}:{seq}" for seq in row[3]],
                snippet=snippet(str(row[5])),
                score=f.score,
                dense_rank=f.dense_rank,
                fts_rank=f.fts_rank,
            )
        )
    return SearchResult(hits, degraded)
