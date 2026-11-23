"""The `search.embed` job, its status and `ensure` (SRV-SEARCH FR-SEARCH-5, FR-SEARCH-9).

The handler runs inside SRV-EVENTS' run transaction: every database write is the transaction's last statements
(FR-EVENTS-4), and an earlier failure leaves nothing. It logs counts only, never a text.
"""

from __future__ import annotations

import logging
from typing import Any, Final
from uuid import UUID

import asyncpg  # untyped: covered by the asyncpg.* mypy override in pyproject.toml
import httpx
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from reader.jobs.model import JobContext, JobKind, idempotency_key
from reader.search import embed_client, params
from reader.search.chunking import Span, chunk_spans
from reader.search.errors import EmbedUnavailable, IngestNotReady, SearchError
from reader.search.store import fts_config_of, vector_literal

log = logging.getLogger("reader.search")
KIND: Final = "search.embed"
# structural: a cold start of the 5.6 GB image, with wide margin over the measured 1-2 s model load
HEALTH_WAIT_S = 300.0
# 1.7 times the upper end of the measured 22-36 min full-pilot estimate at 8 CPUs
TIMEOUT_S: Final = 3600.0


class EmbedArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    material_sha256: str
    ingest_id: UUID
    model: str
    params_sha256: str


def embed_args(material_sha256: str, ingest_id: UUID, fts_config: str) -> EmbedArgs:
    return EmbedArgs(
        material_sha256=material_sha256,
        ingest_id=ingest_id,
        model=params.MODEL_ID,
        params_sha256=params.params_sha256(fts_config),
    )


async def _resolve(conn: AsyncConnection, args: EmbedArgs) -> UUID:
    """FR-SEARCH-5 step 1: the material's frozen ingest. None yet is IngestNotReady at once (SRV-EVENTS
    retries it; the worker's only slot is not held); another ingest is `ingest_superseded`."""
    found = (
        await conn.execute(
            text(
                "SELECT i.owner_id FROM ingests i JOIN materials m ON m.id = i.material_id"
                " WHERE i.id = :i AND m.sha256 = :s"
            ),
            {"i": args.ingest_id, "s": args.material_sha256},
        )
    ).first()
    if found is not None:
        owner: UUID = found[0]
        return owner
    other = (
        await conn.execute(
            text(
                "SELECT 1 FROM ingests i JOIN materials m ON m.id = i.material_id"
                " WHERE m.sha256 = :s LIMIT 1"
            ),
            {"s": args.material_sha256},
        )
    ).first()
    if other is not None:
        raise SearchError("ingest_superseded")
    raise IngestNotReady("ingest_not_ready")


async def handle_embed(args: EmbedArgs, conn: AsyncConnection, ctx: JobContext) -> None:
    del ctx
    owner = await _resolve(conn, args)
    fts_config = await fts_config_of(conn, args.material_sha256, args.ingest_id)
    if args.model != params.MODEL_ID or args.params_sha256 != params.params_sha256(fts_config):
        raise SearchError("params_mismatch")
    done = await conn.execute(
        text("SELECT 1 FROM search_embeds WHERE ingest_id = :i AND model = :m AND params_sha256 = :p"),
        {"i": args.ingest_id, "m": args.model, "p": args.params_sha256},
    )
    if done.first() is not None:
        return
    await embed_client.wait_healthy(HEALTH_WAIT_S)
    rows = await conn.execute(
        text("SELECT seq, page, text FROM material_spans WHERE ingest_id = :i ORDER BY seq"),
        {"i": args.ingest_id},
    )
    chunks = chunk_spans(
        [Span(int(r[0]), int(r[1]), str(r[2])) for r in rows],
        size=params.CHUNK_SIZE,
        overlap=params.CHUNK_OVERLAP,
        min_len=params.CHUNK_MIN,
    )
    vectors: list[list[float]] = []
    for start in range(0, len(chunks), params.PASSAGE_BATCH):
        batch = [c.text for c in chunks[start : start + params.PASSAGE_BATCH]]
        vectors.extend(await embed_client.embed("passage", batch, params.PASSAGE_TIMEOUT_S))
    if chunks:
        await conn.execute(
            text(
                "INSERT INTO search_chunks (owner_id, ingest_id, material_sha256, model, params_sha256,"
                " fts_config, page, chunk_no, char_start, char_stop, span_seqs, primary_seq, text, embedding)"
                " VALUES (:o, :i, :s, :m, :p, :f, :page, :no, :a, :b, :seqs, :prim, :t, CAST(:v AS vector))"
            ),
            [
                {
                    "o": owner, "i": args.ingest_id, "s": args.material_sha256, "m": args.model,
                    "p": args.params_sha256, "f": fts_config, "page": c.page, "no": c.chunk_no,
                    "a": c.char_start, "b": c.char_stop, "seqs": list(c.span_seqs), "prim": c.primary_seq,
                    "t": c.text, "v": vector_literal(v),
                }
                for c, v in zip(chunks, vectors, strict=True)
            ],
        )
    await conn.execute(
        text(
            "INSERT INTO search_embeds (owner_id, ingest_id, model, params_sha256, chunk_count, embedded_at)"
            " VALUES (:o, :i, :m, :p, :n, now())"
        ),
        {"o": owner, "i": args.ingest_id, "m": args.model, "p": args.params_sha256, "n": len(chunks)},
    )
    log.info("embedded chunks=%d", len(chunks))


async def _handle(args: Any, conn: AsyncConnection, ctx: JobContext) -> None:
    await handle_embed(args, conn, ctx)


JOB_KINDS: Final[tuple[JobKind, ...]] = (
    JobKind(
        name=KIND,
        queue="search",
        args=EmbedArgs,
        key=("ingest_id", "model", "params_sha256"),
        handler=_handle,
        max_attempts=3,
        transient=(
            asyncpg.exceptions.ConnectionDoesNotExistError,
            asyncpg.exceptions.CannotConnectNowError,
            ConnectionRefusedError,
            httpx.TransportError,
            EmbedUnavailable,
            IngestNotReady,
        ),
        timeout_s=TIMEOUT_S,
    ),
)


# ---- status and ensure (FR-SEARCH-9) ------------------------------------------------------------------


async def _current_args(queue: Any, owner_id: UUID, material_sha256: str) -> EmbedArgs | None:
    async with queue.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT i.id FROM ingests i JOIN materials m ON m.id = i.material_id"
                    " WHERE m.owner_id = :o AND m.sha256 = :s"
                ),
                {"o": owner_id, "s": material_sha256},
            )
        ).first()
        if row is None:
            return None
        fts_config = await fts_config_of(conn, material_sha256, row[0])
    return embed_args(material_sha256, row[0], fts_config)


def _key(args: EmbedArgs) -> str:
    return idempotency_key(JOB_KINDS[0], args.model_dump(mode="json"))


async def search_status(queue: Any, owner_id: UUID, material_sha256: str) -> str:
    """VEC-SEARCH-4: none | queued | running | ready | dead for the material's current key."""
    args = await _current_args(queue, owner_id, material_sha256)
    if args is None:
        return "none"
    async with queue.engine.connect() as conn:
        row = await conn.execute(
            text("SELECT 1 FROM search_embeds WHERE ingest_id = :i AND model = :m AND params_sha256 = :p"),
            {"i": args.ingest_id, "m": args.model, "p": args.params_sha256},
        )
        has_row = row.first() is not None
    job = str(await queue.job_status(KIND, _key(args)))
    if job == "succeeded" and not has_row:
        log.warning("status_inconsistent")
    return params.status_from(job, has_row)


async def ensure(queue: Any, owner_id: UUID, material_sha256: str) -> str:
    """Enqueue the current key, or requeue it when dead; the resulting status, or `no_ingest`."""
    args = await _current_args(queue, owner_id, material_sha256)
    if args is None:
        return "no_ingest"
    if await search_status(queue, owner_id, material_sha256) == "dead":
        await queue.requeue_dead(KIND, _key(args))
    else:
        await queue.enqueue(KIND, args)
    return await search_status(queue, owner_id, material_sha256)


async def on_worker_start(queue: Any) -> None:
    """FR-SEARCH-9 at worker start: enqueue each imported ingest's current key whose job was never enqueued.

    Dead keys stay dead (only `ensure` requeues them), so a permanent failure is not retried on every restart.
    One ingest's failure is logged by its exception type and does not stop the others.
    """
    imported = "SELECT DISTINCT i.owner_id, m.sha256 FROM ingests i JOIN materials m ON m.id = i.material_id"
    async with queue.engine.connect() as conn:
        rows = (await conn.execute(text(imported))).all()
    for owner_id, material_sha256 in rows:
        try:
            args = await _current_args(queue, owner_id, material_sha256)
            if args is not None and str(await queue.job_status(KIND, _key(args))) == "none":
                await queue.enqueue(KIND, args)
        except Exception as failure:  # FR-SEARCH-9: log by code, keep going
            log.warning("search start hook: enqueue failed: %s", type(failure).__name__)
