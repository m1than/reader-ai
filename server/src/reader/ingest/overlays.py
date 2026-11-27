"""Overlays: content-addressed sets of derived rows over one base pack (SRV-INGEST FR-INGEST-36, ADR-0039 B2).

`begin` creates the `building` row (or finds it), `seal` flips it; a collected id is never built again.
The profile overlay (L0) is built here; the vector overlay's rows live in SRV-SEARCH's table.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from reader.ingest.artifact import canonical

KINDS: Final = ("profile", "tags", "vectors", "prefixes", "summaries")
PROFILE_BUILDER: Final = "profile/1"
PROFILE_FIELDS: Final = ("license", "language", "role", "reliability", "as_of")
OWNER_FIELDS: Final = ("role", "reliability", "as_of")  # set by the owner (G1); the rest by rule


class OverlayError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def overlay_id(kind: str, pack_id: str, inputs: Mapping[str, Any], builder_version: str) -> str:
    """sha256 of the canonical JSON {kind, pack_id, inputs, builder_version}."""
    body = {"kind": kind, "pack_id": pack_id, "inputs": dict(inputs), "builder_version": builder_version}
    return hashlib.sha256(canonical(body)).hexdigest()


async def begin(
    conn: AsyncConnection, owner_id: uuid.UUID, oid: str, *, kind: str, pack_id: str,
    inputs: Mapping[str, Any], builder_version: str,
) -> str:
    """The overlay's state after this call: `building` (new or resumed) or `sealed` (reuse, build nothing)."""
    if kind not in KINDS or oid != overlay_id(kind, pack_id, inputs, builder_version):
        raise OverlayError("overlay_invalid")
    row = (await conn.execute(text(
        "SELECT state, collected_at IS NOT NULL FROM overlays"
        " WHERE owner_id = :o AND overlay_id = :i FOR UPDATE"),
        {"o": owner_id, "i": oid})).first()
    if row is not None:
        if row[1]:
            raise OverlayError("overlay_collected")
        return str(row[0])
    await conn.execute(text(
        "INSERT INTO overlays (owner_id, overlay_id, pack_id, kind, builder_version, inputs, state)"
        " VALUES (:o, :i, :p, :k, :b, CAST(:inp AS jsonb), 'building')"),
        {"o": owner_id, "i": oid, "p": pack_id, "k": kind, "b": builder_version,
         "inp": json.dumps(dict(inputs))})
    return "building"


async def seal(conn: AsyncConnection, owner_id: uuid.UUID, oid: str) -> None:
    done = await conn.execute(text(
        "UPDATE overlays SET state = 'sealed', sealed_at = now()"
        " WHERE owner_id = :o AND overlay_id = :i AND state = 'building' AND collected_at IS NULL"),
        {"o": owner_id, "i": oid})
    if done.rowcount != 1:
        raise OverlayError("not_building")


def profile_inputs(values: Mapping[str, Any]) -> dict[str, Any]:
    return {k: (None if values.get(k) is None else str(values[k])) for k in PROFILE_FIELDS}


async def build_profile(
    conn: AsyncConnection, owner_id: uuid.UUID, pack_id: str, values: Mapping[str, Any]
) -> str:
    """The sealed profile overlay of these values: reused when it exists, else built and sealed."""
    inputs = profile_inputs(values)
    oid = overlay_id("profile", pack_id, inputs, PROFILE_BUILDER)
    if await begin(conn, owner_id, oid, kind="profile", pack_id=pack_id, inputs=inputs,
                   builder_version=PROFILE_BUILDER) == "building":
        by_owner = any(inputs[k] is not None for k in OWNER_FIELDS)
        await conn.execute(text(
            "INSERT INTO pack_profiles (owner_id, overlay_id, role, reliability, as_of, license, language,"
            " set_by) VALUES (:o, :i, :role, :rel, CAST(:asof AS date), :lic, :lang, :by)"
            " ON CONFLICT DO NOTHING"),
            {"o": owner_id, "i": oid, "role": inputs["role"], "rel": inputs["reliability"],
             "asof": inputs["as_of"], "lic": inputs["license"], "lang": inputs["language"],
             "by": "owner" if by_owner else "rule"})
        await seal(conn, owner_id, oid)
    return oid


async def profile_values(conn: AsyncConnection, owner_id: uuid.UUID, oid: str) -> dict[str, Any] | None:
    row = (await conn.execute(text(
        "SELECT license, language, role, reliability, as_of FROM pack_profiles"
        " WHERE owner_id = :o AND overlay_id = :i"), {"o": owner_id, "i": oid})).first()
    if row is None:
        return None
    values = dict(zip(PROFILE_FIELDS, row, strict=True))
    values["as_of"] = None if values["as_of"] is None else values["as_of"].isoformat()
    return values
