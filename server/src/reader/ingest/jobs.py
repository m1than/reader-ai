"""The `ingest.import` job kind (SRV-INGEST FR-INGEST-13), declared for SRV-EVENTS's KIND_MODULES.

The handler re-validates the stored artifact and writes the rows in the run transaction; it never commits.
Permanent failures carry their code as the exception class name, which is what SRV-EVENTS stores.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Final

from asyncpg.exceptions import CannotConnectNowError, ConnectionDoesNotExistError
from pydantic import BaseModel, ConfigDict, Field
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


class ImportArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    material_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    converter_version: str = Field(min_length=1, max_length=64)
    artifact_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    material_id: str = Field(min_length=1, max_length=200)


def make_kinds(blobs_root: Path) -> tuple[JobKind, ...]:
    async def handle(args: ImportArgs, conn: AsyncConnection, ctx: JobContext) -> None:
        del ctx
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

    return (
        JobKind(
            name=KIND,
            queue="ingest",
            args=ImportArgs,
            key=("material_sha256", "converter_version"),
            handler=handle,
            max_attempts=3,
            transient=(ConnectionDoesNotExistError, CannotConnectNowError, ConnectionRefusedError),
            timeout_s=600.0,
        ),
    )


JOB_KINDS: Final = make_kinds(ingest.BLOBS_DIR)


def key_for(material_sha256: str, converter_version: str) -> str:
    """The SRV-EVENTS idempotency key of the import of one material under one converter version."""
    payload: dict[str, Any] = {"material_sha256": material_sha256, "converter_version": converter_version}
    return idempotency_key(JOB_KINDS[0], payload)
