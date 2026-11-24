"""The `ingest.import` job kind (SRV-INGEST FR-INGEST-13), declared for SRV-EVENTS's KIND_MODULES.

The handler re-validates the stored artifact and writes the rows in the run transaction; it never commits.
Permanent failures carry their code as the exception class name, which is what SRV-EVENTS stores.
"""

from __future__ import annotations

import asyncio
import json
import uuid
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
        try:
            manifest, members = await asyncio.to_thread(store.read_artifact, root)
        except ArtifactError as error:
            raise (ArtifactMissing if error.code == "missing" else ArtifactInvalid)() from None
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
        ),
    )


JOB_KINDS: Final = make_kinds(ingest.BLOBS_DIR)


def key_for(material_sha256: str, converter_version: str) -> str:
    """The SRV-EVENTS idempotency key of the import of one material under one converter version."""
    payload: dict[str, Any] = {"material_sha256": material_sha256, "converter_version": converter_version}
    return idempotency_key(JOB_KINDS[0], payload)
