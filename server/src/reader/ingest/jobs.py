"""The `ingest.import` job kind (SRV-INGEST FR-INGEST-13), declared for SRV-EVENTS's KIND_MODULES.

The handler re-validates the stored artifact and writes the rows in the run transaction; it never commits.
Permanent failures carry their code as the exception class name, which is what SRV-EVENTS stores.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from asyncpg.exceptions import CannotConnectNowError, ConnectionDoesNotExistError
from procrastinate.exceptions import ConnectorException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

import reader.ingest as ingest
from reader.ingest import store
from reader.ingest.artifact import ArtifactError, artifact_id
from reader.ingest.errors import import_error
from reader.jobs.model import JobContext, JobKind, idempotency_key
from reader.obs.spans import child_span

KIND: Final = "ingest.import"
VersionFrozen = import_error("version_frozen")
ArtifactMissing = import_error("artifact_missing")
ArtifactInvalid = import_error("artifact_invalid")
NoOwner = import_error("no_owner")
ArtifactMismatch = import_error("artifact_mismatch")
UploadMissing = import_error("upload_missing")


class ImportArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    material_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    converter_version: str = Field(min_length=1, max_length=64)
    artifact_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    material_id: str = Field(min_length=1, max_length=200)


SEQMAP_KIND: Final = "ingest.seqmap"
REANCHOR_KIND: Final = "ingest.reanchor"
_TRANSIENT: Final = (
    ConnectionDoesNotExistError, CannotConnectNowError, ConnectionRefusedError, ConnectorException
)


class SeqmapArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_id: uuid.UUID
    material_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    from_ingest_id: uuid.UUID
    to_pack_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReanchorArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_id: uuid.UUID
    from_pack_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    to_pack_id: str = Field(pattern=r"^[0-9a-f]{64}$")


async def _seqmap(args: SeqmapArgs, conn: AsyncConnection, ctx: JobContext) -> None:
    """FR-INGEST-40 step 3; an accept calls try_activate in this transaction."""
    from reader.ingest import seqmap

    del ctx
    await seqmap.build(conn, args.owner_id, args.from_ingest_id, args.to_pack_id)


async def _reanchor(args: ReanchorArgs, conn: AsyncConnection, ctx: JobContext) -> None:
    """FR-INGEST-43: both packs and the material's head pack locked in one call, then the run and
    try_activate."""
    from reader.ingest import manifest, packs, reanchor

    del ctx
    sha = await packs.material_of(conn, args.owner_id, args.to_pack_id)
    _, top = await manifest.head(conn, args.owner_id)
    held = [e["pack_id"] for e in top["packs"]
            if await packs.material_of(conn, args.owner_id, e["pack_id"]) == sha]
    await packs.lock_packs(conn, {args.from_pack_id, args.to_pack_id, *held})
    await reanchor.run(conn, args.owner_id, args.from_pack_id, args.to_pack_id)  # logs its counts only
    await manifest.try_activate(conn, args.owner_id, args.to_pack_id)


async def _enqueue_embed(conn: AsyncConnection, owner: uuid.UUID, material_sha256: str) -> None:
    """FR-INGEST-13 (SRV-SEARCH FR-SEARCH-5 paired change): search.embed for the ingest this run wrote.

    The ingest id is read in the run session (its rows are not committed yet); the enqueue commits on its
    own connection and is idempotent by FR-SEARCH-5's key. Imported lazily: reader.search pulls httpx and
    the config. A missing or broken reader.yaml (ConfigError) fails the attempt, as it fails the api.
    """
    from reader.jobs import running
    from reader.search.jobs import KIND as EMBED_KIND
    from reader.search.jobs import embed_args
    from reader.search.store import fts_config_of

    ingest_id = (
        await conn.execute(
            text(
                "SELECT i.id FROM ingests i JOIN materials m ON m.id = i.material_id"
                " WHERE m.owner_id = :o AND m.sha256 = :s"
            ),
            {"o": owner, "s": material_sha256},
        )
    ).scalar_one()
    fts_config = await fts_config_of(conn, material_sha256, ingest_id)
    await running().enqueue(EMBED_KIND, embed_args(material_sha256, ingest_id, fts_config))


async def _deleted(conn: AsyncConnection, material_sha256: str) -> bool:
    """The instance owner's row of this material is a tombstone (SRV-INGEST FR-INGEST-31)."""
    owner = await store.owner_id(conn)
    if owner is None:
        return False
    found = await conn.execute(
        text("SELECT 1 FROM materials WHERE owner_id = :o AND sha256 = :s AND deleted_at IS NOT NULL"),
        {"o": owner, "s": material_sha256},
    )
    return found.first() is not None


def make_kinds(blobs_root: Path) -> tuple[JobKind, ...]:
    async def handle(args: ImportArgs, conn: AsyncConnection, ctx: JobContext) -> None:
        del ctx
        if await _deleted(conn, args.material_sha256):  # FR-INGEST-15 amendment: an import never clears it
            store.log.info("ingest material_deleted %s", args.material_id)
            return
        if args.material_id.startswith("up-"):  # FR-INGEST-23: language and title come from the upload's row
            found = await conn.execute(
                text("SELECT 1 FROM uploads WHERE sha256 = :s"), {"s": args.material_sha256}
            )
            if found.first() is None:
                raise UploadMissing()
        root = blobs_root / "ingest" / args.artifact_id
        with child_span("ingest.parse") as parsed:  # SRV-OBS FR-OBS-13: pages and bytes only
            try:
                manifest, members = await asyncio.to_thread(store.read_artifact, root)
            except ArtifactError as error:
                raise (ArtifactMissing if error.code == "missing" else ArtifactInvalid)() from None
            page_count = manifest.get("page_count")
            parsed.set({"reader.pages": page_count if isinstance(page_count, int) else None,
                        "reader.bytes": sum(len(v) for v in members.values())})
        frozen = await store.frozen_version(conn, args.material_sha256)
        if frozen is not None and frozen != args.converter_version:
            raise VersionFrozen()
        if (
            artifact_id(members["manifest.json"]) != args.artifact_id
            or manifest["material_sha256"] != args.material_sha256
            or manifest["converter_version"] != args.converter_version
        ):
            raise ArtifactMismatch()
        owner = await store.owner_id(conn)
        if owner is None:
            raise NoOwner()
        outline = json.loads(members["outline.json"])
        await store.import_rows(
            conn, owner, args.material_id, args.artifact_id, manifest, members["spans.jsonl"], outline
        )
        await _enqueue_embed(conn, owner, args.material_sha256)

    return (
        JobKind(
            name=KIND,
            queue="ingest",
            args=ImportArgs,
            key=("material_sha256", "converter_version"),
            handler=handle,
            max_attempts=3,
            # ConnectorException: procrastinate's wrapper of psycopg errors in the search.embed defer
            transient=(
                ConnectionDoesNotExistError,
                CannotConnectNowError,
                ConnectionRefusedError,
                ConnectorException,
            ),
            timeout_s=600.0,
            after_end=after_end,
        ),
        JobKind(name=SEQMAP_KIND, queue="ingest", args=SeqmapArgs,
                key=("owner_id", "from_ingest_id", "to_pack_id"), handler=_seqmap, transient=_TRANSIENT),
        JobKind(name=REANCHOR_KIND, queue="ingest", args=ReanchorArgs,
                key=("owner_id", "from_pack_id", "to_pack_id"), handler=_reanchor, transient=_TRANSIENT),
    )


async def after_end(engine: Any, payload: Mapping[str, Any], outcome: str) -> None:
    """FR-INGEST-26: a succeeded import enqueues `obs.runs` {ingest, ingest id, 1} after its commit (an import
    ends once: one per material)."""
    if outcome != "succeeded":
        return
    from reader.obs import runs as obs_runs

    async with engine.connect() as conn:
        owner = await store.owner_id(conn)
        if owner is None:
            return
        ingest_id = (await conn.execute(text(
            "SELECT i.id FROM ingests i JOIN materials m ON m.id = i.material_id AND m.owner_id = i.owner_id"
            " WHERE m.owner_id = :o AND m.sha256 = :s AND m.config_id = :c AND i.converter_version = :v"),
            {"o": owner, "s": str(payload["material_sha256"]), "c": str(payload["material_id"]),
             "v": str(payload["converter_version"])})).scalar()
    if ingest_id is not None:
        await obs_runs.enqueue_end("ingest", str(ingest_id), 1)
    from reader.ingest import packs

    pid = packs.pack_id(str(payload["material_sha256"]), str(payload["converter_version"]))
    async with engine.connect() as conn:
        await enqueue_followups(conn, owner, pid)


async def enqueue_followups(conn: AsyncConnection, owner: uuid.UUID, pid: str) -> list[str]:
    """FR-INGEST-13 rev (second part), -40, -43: after the import's commit, `ingest.seqmap` while the
    material has a legacy ingest and no accepted map from it, and `ingest.reanchor` when another pack of it
    is in the head."""
    from reader.ingest import manifest, packs
    from reader.jobs import running

    sha = await packs.material_of(conn, owner, pid)
    version = (await conn.execute(text(
        "SELECT parse_config ->> 'converter_version' FROM base_packs WHERE owner_id = :o AND pack_id = :p"),
        {"o": owner, "p": pid})).scalar()
    if sha is None or version in ingest.LEGACY_VERSIONS:
        return []
    done: list[str] = []
    legacy = (await conn.execute(text(
        "SELECT b.ingest_id FROM base_packs b WHERE b.owner_id = :o AND b.material_sha256 = :s"
        " AND b.parse_config ->> 'converter_version' = ANY(:v) AND NOT EXISTS (SELECT 1 FROM seq_maps m"
        " WHERE m.owner_id = b.owner_id AND m.from_ingest_id = b.ingest_id AND m.state = 'accepted')"
        " ORDER BY b.created_at LIMIT 1"),
        {"o": owner, "s": sha, "v": list(ingest.LEGACY_VERSIONS)})).scalar()
    if legacy is not None:
        await running().enqueue(SEQMAP_KIND, {"owner_id": str(owner), "material_sha256": sha,
                                              "from_ingest_id": str(legacy), "to_pack_id": pid})
        done.append(SEQMAP_KIND)
    _, top = await manifest.head(conn, owner)
    for element in top["packs"]:
        other = element["pack_id"]
        if other != pid and await packs.material_of(conn, owner, other) == sha:
            await running().enqueue(REANCHOR_KIND, {"owner_id": str(owner), "from_pack_id": other,
                                                    "to_pack_id": pid})
            done.append(REANCHOR_KIND)
    return done


JOB_KINDS: Final = make_kinds(ingest.BLOBS_DIR)


async def start_reconcile(jobs: Any) -> None:
    """FR-INGEST-36, -39: the profile reconcile per owner with a manifest, then the pending-activation sweep.

    Not yet `on_worker_start`: SRV-EVENTS FR-EVENTS-1's test pins the declared hooks (ingest exports none), so
    the wiring is the paired SRV-EVENTS change of the revision block's KIND_MODULES note.

    Rule work only (0 model calls); a failure is logged by code and never stops the worker.
    """
    from reader.ingest import manifest, packs

    try:
        materials = packs.load_materials()
        async with jobs.engine.connect() as conn:
            found = await conn.execute(text("SELECT DISTINCT owner_id FROM manifest_entries"))
            owners = [r[0] for r in found]
        for owner in owners:
            async with jobs.engine.begin() as conn:
                await packs.reconcile(conn, owner, materials)
        await manifest.sweep(jobs.engine)
    except Exception as error:  # logged by type; the worker serves jobs anyway
        store.log.warning("ingest start_reconcile_failed %s", type(error).__name__)


def key_for(material_sha256: str, converter_version: str) -> str:
    """The SRV-EVENTS idempotency key of the import of one material under one converter version."""
    payload: dict[str, Any] = {"material_sha256": material_sha256, "converter_version": converter_version}
    return idempotency_key(JOB_KINDS[0], payload)
