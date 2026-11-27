"""The `search.embed` job on vector overlays, its status and `ensure` (SRV-SEARCH FR-SEARCH-5, -6, -9).

The handler runs inside SRV-EVENTS' run transaction: the embed calls come first and every database write is
the transaction's last statements (FR-EVENTS-4), so an earlier failure, or a refusal from `try_activate`,
leaves nothing. It logs counts only, never a text. Pack facts come from SRV-INGEST's views, manifest and
profile read; the vector overlay's own `overlays` row (state, collected) is read here because this module
builds it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any, Final

import asyncpg  # untyped: covered by the asyncpg.* mypy override in pyproject.toml
import httpx
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from reader.ingest import manifest, overlays, packs, views
from reader.jobs.model import JobContext, JobKind, idempotency_key
from reader.obs.spans import annotate_job, child_span
from reader.search import embed_client, params
from reader.search.chunking import windows
from reader.search.errors import EmbedUnavailable, IngestNotReady, SearchError
from reader.search.store import vector_literal

log = logging.getLogger("reader.search")
KIND: Final = "search.embed"
BUILDER: Final = "search.vectors/1"
# structural: a cold start of the 5.6 GB image, with wide margin over the measured 1-2 s model load
HEALTH_WAIT_S = 300.0
# 1.7 times the upper end of the measured 22-36 min full-pilot estimate at 8 CPUs
TIMEOUT_S: Final = 3600.0
# try_activate's results that roll the build back (FR-SEARCH-5): the pack can never take this overlay
REFUSED: Final = frozenset({"pack_purged", "pack_collected", "not_sealed"})


class EmbedArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_id: uuid.UUID
    pack_id: str
    overlay_id: str


def vector_inputs(fts_config: str, prefix_overlay_id: str | None = None) -> dict[str, Any]:
    """FR-SEARCH-5's overlay inputs; the id itself comes only from SRV-INGEST's `overlays.overlay_id`."""
    return {"model": params.MODEL_ID, "params_sha256": params.params_sha256(fts_config),
            "prefix_overlay_id": prefix_overlay_id}


async def fts_config_of(conn: AsyncConnection, owner_id: uuid.UUID, pack_id: str) -> str:
    """FR-SEARCH-6: the build path's configuration, from `profile_for_pack`'s language."""
    profile = await packs.profile_for_pack(conn, owner_id, pack_id)
    return params.fts_config_for(None if profile is None else profile.get("language"))


async def overlay_for(conn: AsyncConnection, owner_id: uuid.UUID, pack_id: str) -> str:
    """The pack's current vector overlay id."""
    inputs = vector_inputs(await fts_config_of(conn, owner_id, pack_id))
    return overlays.overlay_id("vectors", pack_id, inputs, BUILDER)


async def args_for(conn: AsyncConnection, owner_id: uuid.UUID, pack_id: str) -> EmbedArgs:
    overlay_id = await overlay_for(conn, owner_id, pack_id)
    return EmbedArgs(owner_id=owner_id, pack_id=pack_id, overlay_id=overlay_id)


def job_key(args: EmbedArgs) -> str:
    return idempotency_key(JOB_KINDS[0], args.model_dump(mode="json"))


async def _deleted(conn: AsyncConnection, owner_id: uuid.UUID, material_sha256: str) -> bool:
    row = await conn.execute(
        text("SELECT 1 FROM materials WHERE owner_id = :o AND sha256 = :s AND deleted_at IS NOT NULL"),
        {"o": owner_id, "s": material_sha256},
    )
    return row.first() is not None


async def _newest_live(conn: AsyncConnection, owner_id: uuid.UUID, material_sha256: str) -> views.Pack | None:
    """The material's newest sealed pack that is neither purged nor collected, by converter-version order."""
    live = [p for p in await views.live_packs(conn, owner_id) if p.material_sha256 == material_sha256]
    return max(live, key=lambda p: (packs.version_rank(p.converter_version), p.pack_id), default=None)


async def buildable_pack(
    conn: AsyncConnection, owner_id: uuid.UUID, material_sha256: str
) -> views.Pack | None:
    """FR-SEARCH-5's predicate per material: the head entry's pack, else the newest sealed one of a material
    that is not deleted."""
    pack = await views.active_pack(conn, owner_id, material_sha256)
    if pack is not None:
        return pack
    if await _deleted(conn, owner_id, material_sha256):
        return None
    return await _newest_live(conn, owner_id, material_sha256)


async def buildable(conn: AsyncConnection, owner_id: uuid.UUID, pack_id: str) -> views.Pack | None:
    """FR-SEARCH-5's `buildable` for one pack: the pack itself when it satisfies the predicate."""
    sha = await packs.material_of(conn, owner_id, pack_id)
    if sha is None:
        return None
    pack = await buildable_pack(conn, owner_id, sha)
    return pack if pack is not None and pack.pack_id == pack_id else None


async def overlay_state(conn: AsyncConnection, owner_id: uuid.UUID, overlay_id: str) -> str:
    """none | building | sealed | collected for the owner's overlay row."""
    row = (await conn.execute(
        text("SELECT state, collected_at IS NOT NULL FROM overlays WHERE owner_id = :o AND overlay_id = :i"),
        {"o": owner_id, "i": overlay_id},
    )).first()
    if row is None:
        return "none"
    return "collected" if row[1] else str(row[0])


async def _build(conn: AsyncConnection, args: EmbedArgs, pack: views.Pack, fts_config: str) -> None:
    await embed_client.wait_healthy(HEALTH_WAIT_S)
    spans = [s for s in await views.pack_spans(conn, pack) if s.locator is not None]
    with child_span("ingest.chunk") as chunked:  # SRV-OBS FR-OBS-13: counts only
        cut = [(s.locator, w) for s in spans
               for w in windows(s.text, params.CHUNK_SIZE, params.CHUNK_OVERLAP, params.CHUNK_MIN)]
        chunked.set({"reader.chunks": len(cut)})
    vectors: list[list[float]] = []
    for start in range(0, len(cut), params.PASSAGE_BATCH):
        batch = [w.passage for _, w in cut[start : start + params.PASSAGE_BATCH]]
        with child_span("embed.batch", {"reader.embed.batch": len(batch)}) as embedded:
            got = await embed_client.embed("passage", batch, params.PASSAGE_TIMEOUT_S)
            embedded.set({"reader.embed.vectors": len(got)})
        vectors.extend(got)
    try:
        await overlays.begin(conn, args.owner_id, args.overlay_id, kind="vectors", pack_id=args.pack_id,
                             inputs=vector_inputs(fts_config), builder_version=BUILDER)
    except overlays.OverlayError as error:
        raise SearchError("overlay_superseded") from error
    if cut:
        await conn.execute(
            text(
                "INSERT INTO search_span_vectors (owner_id, overlay_id, pack_id, locator, window_no,"
                " char_start, char_stop, model, fts_config, fts, embedding) VALUES (:o, :v, :p, :l, :n, :a,"
                " :b, :m, :f, to_tsvector(CAST(:f AS regconfig), :t), CAST(:e AS vector))"
            ),
            [
                {"o": args.owner_id, "v": args.overlay_id, "p": args.pack_id, "l": loc, "n": w.window_no,
                 "a": w.char_start, "b": w.char_stop, "m": params.MODEL_ID, "f": fts_config, "t": w.passage,
                 "e": vector_literal(v)}
                for (loc, w), v in zip(cut, vectors, strict=True)
            ],
        )
    await overlays.seal(conn, args.owner_id, args.overlay_id)
    log.info("embedded windows=%d", len(cut))


async def handle_embed(args: EmbedArgs, conn: AsyncConnection, ctx: JobContext) -> None:
    del ctx
    owner, pid = args.owner_id, args.pack_id
    pack = await buildable(conn, owner, pid)
    if pack is None or await overlay_for(conn, owner, pid) != args.overlay_id:
        raise SearchError("overlay_superseded")
    # FR-OBS-5 (e): the ingest Run links this job's trace by its ingest id (the args carry none)
    annotate_job({"reader.ingest_id": str(pack.ingest_id), "reader.material_sha256": pack.material_sha256})
    state = await overlay_state(conn, owner, args.overlay_id)
    if state == "collected":
        raise SearchError("overlay_superseded")
    if state != "sealed":
        await _build(conn, args, pack, await fts_config_of(conn, owner, pid))
    await manifest.request_activation(conn, owner, pid, {"vectors": args.overlay_id})
    outcome = await manifest.try_activate(conn, owner, pid)
    if outcome in REFUSED:
        raise SearchError("overlay_superseded")


async def _handle(args: Any, conn: AsyncConnection, ctx: JobContext) -> None:
    await handle_embed(args, conn, ctx)


async def after_end(engine: Any, payload: Mapping[str, Any], outcome: str) -> None:
    """SRV-INGEST FR-INGEST-26's late end (SRV-OBS FR-OBS-20 d): a succeeded embed enqueues `obs.runs`
    {ingest, the pack's ingest id, 1 + the pack's sealed vector overlays}."""
    if outcome != "succeeded":
        return
    from reader.obs import runs as obs_runs

    owner = uuid.UUID(str(payload["owner_id"]))
    async with engine.connect() as conn:
        pack = next((p for p in await views.live_packs(conn, owner) if p.pack_id == payload["pack_id"]), None)
        sealed = None if pack is None else (await conn.execute(
            text("SELECT count(*) FROM overlays WHERE owner_id = :o AND pack_id = :p AND kind = 'vectors'"
                 " AND state = 'sealed' AND collected_at IS NULL"),
            {"o": owner, "p": pack.pack_id},
        )).scalar()
    if pack is not None and sealed:
        await obs_runs.enqueue_end("ingest", str(pack.ingest_id), min(1 + int(sealed), 100))


JOB_KINDS: Final[tuple[JobKind, ...]] = (
    JobKind(
        name=KIND,
        queue="search",
        args=EmbedArgs,
        key=("owner_id", "overlay_id"),
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
        after_end=after_end,
    ),
)


# ---- status and ensure (FR-SEARCH-9) ------------------------------------------------------------------


async def current_args(conn: AsyncConnection, owner_id: uuid.UUID, material_sha256: str) -> EmbedArgs | None:
    """The key of the material's buildable pack's current vector overlay; None without a buildable pack."""
    pack = await buildable_pack(conn, owner_id, material_sha256)
    return None if pack is None else await args_for(conn, owner_id, pack.pack_id)


async def embed_key(conn: AsyncConnection, owner_id: uuid.UUID, material_sha256: str) -> str | None:
    """The current key as SRV-INGEST's status, Retry and busy check read it (paired)."""
    args = await current_args(conn, owner_id, material_sha256)
    return None if args is None else job_key(args)


async def _overlay_of(conn: AsyncConnection, args: EmbedArgs) -> str:
    """VEC-SEARCH-4's overlay state: `active` when sealed and named by the head entry."""
    state = await overlay_state(conn, args.owner_id, args.overlay_id)
    if state == "sealed":
        view = await manifest.active_view(conn, args.owner_id)
        named = {e["overlays"].get("vectors") for e in view.entry["packs"] if e["pack_id"] == args.pack_id}
        return "active" if args.overlay_id in named else "sealed"
    return state


async def _status(queue: Any, args: EmbedArgs) -> str:
    async with queue.engine.connect() as conn:
        overlay = await _overlay_of(conn, args)
    job = str(await queue.job_status(KIND, job_key(args)))
    if job == "succeeded" and overlay in ("none", "building"):
        log.warning("status_inconsistent")
    return params.status_from(job, overlay)


async def search_status(queue: Any, owner_id: uuid.UUID, material_sha256: str) -> str:
    """VEC-SEARCH-4: none | queued | running | ready | dead | collected for the material's current key."""
    async with queue.engine.connect() as conn:
        args = await current_args(conn, owner_id, material_sha256)
    return "none" if args is None else await _status(queue, args)


async def _ensure_args(queue: Any, args: EmbedArgs, state: str) -> None:
    """Enqueue the key, requeue it when dead; a sealed or collected overlay is left alone."""
    if state in ("sealed", "collected"):
        return
    if str(await queue.job_status(KIND, job_key(args))) == "dead":
        await queue.requeue_dead(KIND, job_key(args))
    else:
        await queue.enqueue(KIND, args)


async def ensure(queue: Any, owner_id: uuid.UUID, material_sha256: str) -> str:
    """The CLI's ensure: the resulting status, or `no_ingest` without a buildable pack."""
    async with queue.engine.connect() as conn:
        args = await current_args(conn, owner_id, material_sha256)
        state = "none" if args is None else await overlay_state(conn, owner_id, args.overlay_id)
    if args is None:
        return "no_ingest"
    await _ensure_args(queue, args, state)
    return await _status(queue, args)


async def ensure_material(conn: AsyncConnection, owner_id: uuid.UUID, material_sha256: str) -> None:
    """SRV-INGEST's `SEARCH_ENSURE` seam (restore, profile reconcile): the material's current key enqueued, or
    requeued when dead, through the worker's queue; in a process without a worker (the api) it is skipped and
    the worker-start hook enqueues it."""
    from reader.jobs import JobError, running

    try:
        queue = running()
    except JobError:
        log.info("search enqueue_skipped no_worker")
        return
    if queue.catalog.get(KIND) is None:
        return
    args = await current_args(conn, owner_id, material_sha256)
    if args is not None:
        await _ensure_args(queue, args, await overlay_state(conn, owner_id, args.overlay_id))


async def on_worker_start(queue: Any) -> None:
    """FR-SEARCH-9 at worker start: for every buildable pack (legacy packs included) whose current vector
    overlay is neither sealed nor collected, enqueue its key when the job status is `none`. Dead keys stay
    dead (only `ensure` requeues them). The worker's start-hook handler guards the whole hook."""
    keys: list[EmbedArgs] = []
    async with queue.engine.connect() as conn:
        pairs = dict.fromkeys((p.owner_id, p.material_sha256) for p in await views.live_packs(conn))
        for owner_id, material_sha256 in pairs:
            args = await current_args(conn, owner_id, material_sha256)
            if args is not None and await overlay_state(conn, owner_id, args.overlay_id) not in (
                "sealed", "collected"
            ):
                keys.append(args)
    for args in keys:
        if str(await queue.job_status(KIND, job_key(args))) == "none":
            await queue.enqueue(KIND, args)
