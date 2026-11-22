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

_CURRENT: Final = """
WITH cur AS (
    SELECT DISTINCT ON (ingest_id) ingest_id, params_sha256
    FROM search_embeds WHERE owner_id = :o AND model = :m
    ORDER BY ingest_id, embedded_at DESC
)
"""
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


def _language(config_id: str | None) -> str | None:
    path = Path(os.environ.get("READER_CONFIG", DEFAULT_CONFIG))
    config = load_config(path)
    return next((m.language for m in config.materials if m.id == config_id), None)


async def fts_config_of(conn: AsyncConnection, material_sha256: str, ingest_id: UUID | None = None) -> str:
    """FR-SEARCH-6: the material's reader.yaml language through materials.config_id; no entry is `simple`."""
    if ingest_id is None:
        sql = "SELECT config_id FROM materials WHERE sha256 = :s ORDER BY id LIMIT 1"
        row = (await conn.execute(text(sql), {"s": material_sha256})).first()
    else:
        sql = "SELECT m.config_id FROM ingests i JOIN materials m ON m.id = i.material_id WHERE i.id = :i"
        row = (await conn.execute(text(sql), {"i": ingest_id})).first()
    return params.fts_config_for(_language(row[0]) if row else None)


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
         "n": params.CANDIDATES},
    )
    return [int(r[0]) for r in rows]


async def fts_list(
    conn: AsyncConnection, owner_id: UUID, query: str, material_sha256: str | None
) -> list[int]:
    """The OR full-text list: per configuration present, the query's lexemes quoted and OR-ed."""
    base = {"o": owner_id, "m": params.MODEL_ID, "mat": material_sha256}
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


async def page_of(conn: AsyncConnection, chunk_id: int) -> int:
    row = await conn.execute(text("SELECT page FROM search_chunks WHERE id = :i"), {"i": chunk_id})
    page: int = row.scalar_one()
    return page


async def search(
    engine: AsyncEngine,
    owner_id: UUID,
    query: str,
    k: int = params.K_DEFAULT,
    material_sha256: str | None = None,
) -> SearchResult:
    """FR-SEARCH-7: hybrid top k; with the embed service down, the full-text list alone, `degraded`."""
    degraded = False
    try:
        vector: list[float] | None = await embed_query(query)
    except (EmbedUnavailable, SearchError, httpx.HTTPError) as failure:
        log.warning("search degraded: %s", type(failure).__name__)
        vector, degraded = None, True
    async with engine.connect() as conn:
        dense = await dense_list(conn, owner_id, vector, material_sha256) if vector is not None else []
        fts = await fts_list(conn, owner_id, query, material_sha256)
        fused = rrf(dense, fts)[:k]
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
