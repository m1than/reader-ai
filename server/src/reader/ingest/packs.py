"""Base packs: ids, per-pack advisory locks, the profile read and the worker-start reconcile (SRV-INGEST
FR-INGEST-35, -36, -46; ADR-0039 B2, B7b)."""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

import reader.ingest as ingest
from reader.ingest import overlays
from reader.ingest.artifact import canonical

log = logging.getLogger("reader.ingest")
LOCK_NAMESPACE: Final = 0x524B504B  # "RKPK", as SRV-LESSON's "RCPO"
UPLOAD_LICENSE: Final = "personal-use"  # an upload's license (the materials listing states the same)

# SRV-SEARCH's `ensure(conn, owner_id, material_sha256)` (paired); a no-op until its revision binds it.
SearchEnsure = Callable[[AsyncConnection, uuid.UUID, str], Awaitable[None]]


async def _unbound_ensure(conn: AsyncConnection, owner_id: uuid.UUID, material_sha256: str) -> None:
    del conn, owner_id, material_sha256


SEARCH_ENSURE: list[SearchEnsure] = [_unbound_ensure]


class PackError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def pack_id(material_sha256: str, converter_version: str) -> str:
    """sha256 of the canonical {material_sha256, parse_config}; no owner (FR-INGEST-35)."""
    config = dict(ingest.PARSE_CONFIGS[converter_version])
    return hashlib.sha256(canonical({"material_sha256": material_sha256, "parse_config": config})).hexdigest()


def version_rank(converter_version: str) -> int:
    """Converter-version order: the position in PARSE_CONFIGS."""
    return list(ingest.PARSE_CONFIGS).index(converter_version)


def lock_key(pid: str) -> int:
    return int(pid[0:8], 16) - 2**31


async def lock_packs(conn: AsyncConnection, pack_ids: Iterable[str]) -> None:
    """FR-INGEST-46: pack locks in ascending id order; new keys only above every key already held."""
    keys = sorted({lock_key(p) for p in pack_ids})
    if not keys:
        return
    held = {
        (int(r[0]) + 2**31) % 2**32 - 2**31
        for r in await conn.execute(text(
            "SELECT objid::bigint FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
            " AND classid = :ns AND objsubid = 2 AND granted"), {"ns": LOCK_NAMESPACE})
    }
    new = [k for k in keys if k not in held]
    if new and held and min(new) < max(held):
        raise PackError("lock_order")
    for key in keys:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:ns, :k)"), {"ns": LOCK_NAMESPACE, "k": key})


async def material_of(conn: AsyncConnection, owner_id: uuid.UUID, pid: str) -> str | None:
    row = await conn.execute(
        text("SELECT material_sha256 FROM base_packs WHERE owner_id = :o AND pack_id = :p"),
        {"o": owner_id, "p": pid},
    )
    value = row.scalar()
    return None if value is None else str(value)


async def profile_for_pack(conn: AsyncConnection, owner_id: uuid.UUID, pid: str) -> dict[str, Any] | None:
    """The head's profile when the pack is active, else its pending one, else the one its import built."""
    from reader.ingest import manifest

    view = await manifest.active_view(conn, owner_id)
    oid = next((e["overlays"].get("profile") for e in view.entry["packs"] if e["pack_id"] == pid), None)
    if oid is None:
        pending = (await conn.execute(text(
            "SELECT overlays ->> 'profile' FROM pending_activations WHERE owner_id = :o AND pack_id = :p"),
            {"o": owner_id, "p": pid})).scalar()
        oid = pending
    if oid is None:
        oid = (await conn.execute(text(
            "SELECT overlay_id FROM overlays WHERE owner_id = :o AND pack_id = :p AND kind = 'profile'"
            " AND state = 'sealed' ORDER BY created_at LIMIT 1"), {"o": owner_id, "p": pid})).scalar()
    return None if oid is None else await overlays.profile_values(conn, owner_id, str(oid))


def load_materials() -> Sequence[Any] | None:
    """reader.yaml's materials (READER_CONFIG, as SRV-SEARCH reads it); None when it cannot be read."""
    from reader.config import load_config
    from reader.config.errors import ConfigError

    try:
        return load_config(Path(os.environ.get("READER_CONFIG", "reader.yaml"))).materials
    except (ConfigError, OSError):
        return None


async def rule_values(
    conn: AsyncConnection, material_sha256: str, materials: Sequence[Any] | None
) -> dict[str, Any] | None:
    """FR-INGEST-36's rule values: the upload row, else reader.yaml's entry; None when neither is known."""
    upload = (await conn.execute(text("SELECT language FROM uploads WHERE sha256 = :s"),
                                 {"s": material_sha256})).scalar()
    if upload is not None:
        return {"license": UPLOAD_LICENSE, "language": str(upload)}
    for m in materials or ():
        if m.sha256 == material_sha256:
            return {"license": str(m.license), "language": str(m.language)}
    return None


async def reconcile(
    conn: AsyncConnection, owner_id: uuid.UUID, materials: Sequence[Any] | None,
    ensure: SearchEnsure | None = None,
) -> None:
    """FR-INGEST-36: each head pack's profile overlay follows the current rule values (no tags enabled)."""
    from reader.ingest import manifest

    view = await manifest.active_view(conn, owner_id)
    for element in view.entry["packs"]:
        pid = element["pack_id"]
        sha = await material_of(conn, owner_id, pid)
        rule = None if sha is None else await rule_values(conn, sha, materials)
        head = element["overlays"].get("profile")
        current = None if head is None else await overlays.profile_values(conn, owner_id, head)
        if rule is None or sha is None:
            continue
        values = {**{k: (current or {}).get(k) for k in overlays.OWNER_FIELDS}, **rule}
        inputs = overlays.profile_inputs(values)
        oid = overlays.overlay_id("profile", pid, inputs, overlays.PROFILE_BUILDER)
        if oid == head:
            continue
        try:
            oid = await overlays.build_profile(conn, owner_id, pid, values)
        except overlays.OverlayError as error:
            log.info("ingest %s", error.code)
            continue
        await manifest.request_activation(conn, owner_id, pid, {"profile": oid})
        await manifest.try_activate(conn, owner_id, pid)
        await (ensure or SEARCH_ENSURE[0])(conn, owner_id, sha)
