"""Garbage collection and owner purge of packs and overlays (SRV-INGEST FR-INGEST-44, -45; INV-INGEST-14;
ADR-0039 P6).

Background only: the jobs of `reader.ingest.jobs` are the only callers. Every delete runs under the pack's
lock (FR-INGEST-46) with `reader.pack_delete` on; `ingests` and `base_packs` rows stay as the tombstone and
overlay rows keep their `collected_at`. Other modules are reached through the declared `Seams` record only.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from reader.ingest import manifest, packs
from reader.ingest.errors import import_error

log = logging.getLogger("reader.ingest")
SeamUnbound = import_error("seam_unbound")  # a text-holding seam not bound yet: the delete rolls back
KEEP_ENTRIES: Final = 20  # bounds.gc_keep_entries
AGE_FLOOR_DAYS: Final = 30  # bounds.gc_age_floor_days (judge.find output retention)
GC_PACK_KIND: Final = "ingest.gc_pack"
GC_OVERLAY_KIND: Final = "ingest.gc_overlay"

OwnerCall = Callable[[AsyncConnection, uuid.UUID, Any], Awaitable[Any]]


async def _judges_forget_pack(conn: AsyncConnection, owner_id: uuid.UUID, pack_id: Any) -> Any:
    from reader.agent import judges

    return await judges.forget_pack(conn, owner_id, str(pack_id))


async def _judges_forget_overlay(conn: AsyncConnection, owner_id: uuid.UUID, overlay_id: Any) -> Any:
    from reader.agent import judges

    return await judges.forget_overlay(conn, owner_id, str(overlay_id))


async def _unbound(conn: AsyncConnection, owner_id: uuid.UUID, value: Any) -> None:
    """A text-holding seam left unbound. No production seam uses it any more (SRV-SEARCH's forget_overlay is
    bound); tests put it in a seam to check that a delete needing it raises `seam_unbound` and rolls back
    (FR-INGEST-44, -45)."""
    del conn, owner_id, value
    raise SeamUnbound


async def _requests_expire(conn: AsyncConnection, owner_id: uuid.UUID, pack_id: Any) -> None:
    """SRV-EVENTS' judge_requests purge (paired, not yet bound): a pending request's card text stays until its
    deadline plus the collector's grace (FR-INGEST-45's recorded deviation), so this no-op does not refuse."""
    del conn, owner_id, pack_id


async def _search_forget_legacy(conn: AsyncConnection, owner_id: uuid.UUID, ingest_id: Any) -> Any:
    from reader.search import store

    return await store.forget_legacy(conn, owner_id, ingest_id)


async def _search_forget_overlay(conn: AsyncConnection, owner_id: uuid.UUID, overlay_id: Any) -> Any:
    from reader.search import store

    return await store.forget_overlay(conn, owner_id, str(overlay_id))


async def _cited_until_bound(conn: AsyncConnection, owner_id: uuid.UUID, pack_id: Any) -> bool:
    """Fails closed until SRV-LESSON's revision_packs and SRV-KB's findings bind it: every pack is cited."""
    del conn, owner_id, pack_id
    return True


async def _obs_purge_judge_inputs(conn: AsyncConnection, pack_id: str) -> Any:
    from reader.obs import judge_inputs

    return await judge_inputs.purge_pack(conn, pack_id)


@dataclass(frozen=True)
class Seams:
    """The other modules' functions GC and purge call in their transaction (FR-INGEST-44, -45)."""

    forget_pack: OwnerCall = _judges_forget_pack  # SRV-AGENT judges.forget_pack
    forget_overlay: OwnerCall = _judges_forget_overlay  # SRV-AGENT judges.forget_overlay
    search_forget_overlay: OwnerCall = _search_forget_overlay  # SRV-SEARCH forget_overlay (FR-SEARCH-5)
    search_forget_legacy: OwnerCall = _search_forget_legacy  # SRV-SEARCH FR-SEARCH-21
    cited: Callable[[AsyncConnection, uuid.UUID, Any], Awaitable[bool]] = _cited_until_bound
    purge_judge_inputs: Callable[[AsyncConnection, str], Awaitable[Any]] = _obs_purge_judge_inputs  # SRV-OBS
    purge_judge_requests: OwnerCall = _requests_expire  # SRV-EVENTS (paired)


@dataclass(frozen=True)
class Policy:
    keep_entries: int = KEEP_ENTRIES
    age_floor_days: int = AGE_FLOOR_DAYS
    now: datetime | None = None  # None: the database clock


SEAMS: list[Seams] = [Seams()]
POLICY: list[Policy] = [Policy()]

_LAST_ENTRIES: Final = (
    "SELECT entry FROM manifest_entries WHERE owner_id = :o ORDER BY seq DESC LIMIT :n"
)
_PACK_ELIGIBLE: Final = (  # constants only
    "SELECT b.ingest_id FROM base_packs b WHERE b.owner_id = :o AND b.pack_id = :p"  # noqa: S608
    " AND b.state = 'sealed'"
    " AND b.collected_at IS NULL AND b.purged_at IS NULL"
    " AND b.sealed_at < coalesce(CAST(:now AS timestamptz), now()) - make_interval(days => :d)"
    f" AND NOT EXISTS (SELECT 1 FROM ({_LAST_ENTRIES}) m, jsonb_array_elements(m.entry -> 'packs') e"
    "  WHERE e ->> 'pack_id' = b.pack_id)"
    " AND NOT EXISTS (SELECT 1 FROM seq_maps s WHERE s.owner_id = b.owner_id AND s.to_pack_id = b.pack_id"
    "  AND s.state = 'accepted')"
    " AND NOT EXISTS (SELECT 1 FROM pending_activations a WHERE a.owner_id = b.owner_id"
    "  AND a.pack_id = b.pack_id)"
)
_OVERLAY_ELIGIBLE: Final = (  # constants only
    "SELECT o.pack_id FROM overlays o"  # noqa: S608
    " JOIN base_packs b ON b.owner_id = o.owner_id AND b.pack_id = o.pack_id"
    " WHERE o.owner_id = :o AND o.overlay_id = :i AND o.collected_at IS NULL"
    " AND o.kind NOT IN ('profile', 'vectors')"  # kept while their pack is: a flip back reuses them
    " AND b.collected_at IS NULL AND b.purged_at IS NULL"
    " AND ((o.state = 'sealed'"
    "   AND o.sealed_at < coalesce(CAST(:now AS timestamptz), now()) - make_interval(days => :d))"
    "  OR (o.state = 'building'"
    "   AND o.created_at < coalesce(CAST(:now AS timestamptz), now()) - make_interval(days => :d)"
    "   AND NOT EXISTS (SELECT 1 FROM (SELECT entry FROM manifest_entries WHERE owner_id = :o"
    "    ORDER BY seq DESC LIMIT 1) h, jsonb_array_elements(h.entry -> 'packs') e"
    "    WHERE e ->> 'pack_id' = o.pack_id)))"
    f" AND NOT EXISTS (SELECT 1 FROM ({_LAST_ENTRIES}) m, jsonb_array_elements(m.entry -> 'packs') e,"
    "  jsonb_each_text(e -> 'overlays') x WHERE x.value = o.overlay_id)"
    " AND NOT EXISTS (SELECT 1 FROM pending_activations a, jsonb_each_text(a.overlays) x"
    "  WHERE a.owner_id = o.owner_id AND x.value = o.overlay_id)"
)


def _params(owner_id: uuid.UUID, policy: Policy) -> dict[str, Any]:
    if policy.keep_entries < 1:  # the head entry is always kept
        raise ValueError("keep_entries")
    return {"o": owner_id, "n": policy.keep_entries, "d": policy.age_floor_days, "now": policy.now}


async def _pack_eligible(
    conn: AsyncConnection, owner_id: uuid.UUID, pid: str, policy: Policy, seams: Seams
) -> Any:
    """The pack's ingest id when every FR-INGEST-44 condition holds, else None."""
    ingest_id = (await conn.execute(text(_PACK_ELIGIBLE), {**_params(owner_id, policy), "p": pid})).scalar()
    if ingest_id is None or await seams.cited(conn, owner_id, pid):
        return None
    return ingest_id


async def _overlay_eligible(
    conn: AsyncConnection, owner_id: uuid.UUID, oid: str, policy: Policy
) -> str | None:
    pid = (await conn.execute(text(_OVERLAY_ELIGIBLE), {**_params(owner_id, policy), "i": oid})).scalar()
    return None if pid is None else str(pid)


async def candidates(
    conn: AsyncConnection, policy: Policy, seams: Seams
) -> tuple[list[tuple[uuid.UUID, str]], list[tuple[uuid.UUID, str]]]:
    """The scanner's read, without locks: collectable packs, and the lone overlays (of packs that stay)."""
    rows = (await conn.execute(text(
        "SELECT owner_id, pack_id FROM base_packs WHERE collected_at IS NULL AND purged_at IS NULL"
        " ORDER BY owner_id, pack_id"))).all()
    found = [(r[0], str(r[1])) for r in rows
             if await _pack_eligible(conn, r[0], str(r[1]), policy, seams) is not None]
    gone = set(found)
    rows = (await conn.execute(text(
        "SELECT owner_id, overlay_id, pack_id FROM overlays WHERE collected_at IS NULL"
        " ORDER BY owner_id, overlay_id"))).all()
    lone = [(r[0], str(r[1])) for r in rows
            if (r[0], str(r[2])) not in gone and await _overlay_eligible(conn, r[0], str(r[1]), policy)]
    return found, lone


async def scan(conn: AsyncConnection, slot: str, policy: Policy, seams: Seams,
               blobs_root: Path | None = None) -> int:
    """The `ingest.gc` job: one job per candidate pack and per lone overlay (keyed by slot), then the artifact
    directories a crash left behind. Returns the number of jobs enqueued."""
    from reader.ingest.jobs import enqueue_known

    found, lone = await candidates(conn, policy, seams)
    count = 0
    for owner_id, pid in found:
        count += await enqueue_known(GC_PACK_KIND, {"owner_id": str(owner_id), "pack_id": pid, "slot": slot})
    for owner_id, oid in lone:
        count += await enqueue_known(GC_OVERLAY_KIND,
                                     {"owner_id": str(owner_id), "overlay_id": oid, "slot": slot})
    if blobs_root is not None:
        rows = await conn.execute(text(
            "SELECT DISTINCT i.artifact_id FROM ingests i JOIN base_packs b ON b.ingest_id = i.id"
            " WHERE b.collected_at IS NOT NULL OR b.purged_at IS NOT NULL"))
        for (artifact,) in rows.all():
            if (blobs_root / "ingest" / str(artifact)).exists():
                await _remove_if_unnamed(conn, blobs_root, str(artifact))
    return count


async def _delete_mode(conn: AsyncConnection) -> None:
    await conn.execute(text("SELECT set_config('reader.pack_delete', 'on', true)"))


async def _drop_overlay(
    conn: AsyncConnection, owner_id: uuid.UUID, oid: str, kind: str, seams: Seams
) -> None:
    for statement in ("DELETE FROM pack_profiles WHERE owner_id = :o AND overlay_id = :i",
                      "DELETE FROM span_tags WHERE owner_id = :o AND overlay_id = :i"):
        await conn.execute(text(statement), {"o": owner_id, "i": oid})
    if kind == "vectors":
        await seams.search_forget_overlay(conn, owner_id, oid)
    await seams.forget_overlay(conn, owner_id, oid)
    await conn.execute(text(
        "UPDATE overlays SET collected_at = now() WHERE owner_id = :o AND overlay_id = :i"),
        {"o": owner_id, "i": oid})


async def _drop_pack_data(
    conn: AsyncConnection, owner_id: uuid.UUID, pid: str, ingest_id: Any, seams: Seams
) -> None:
    """Spans, outline, overlays and their rows, and the other modules' rows of the pack (lock held)."""
    await _delete_mode(conn)
    rows = (await conn.execute(text(
        "SELECT overlay_id, kind FROM overlays WHERE owner_id = :o AND pack_id = :p AND collected_at IS NULL"
        " ORDER BY overlay_id"), {"o": owner_id, "p": pid})).all()
    for oid, kind in rows:
        await _drop_overlay(conn, owner_id, str(oid), str(kind), seams)
    await seams.forget_pack(conn, owner_id, pid)
    await seams.search_forget_legacy(conn, owner_id, ingest_id)
    for statement in ("DELETE FROM material_spans WHERE ingest_id = :i",  # the ingest is this pack's (UNIQUE)
                      "DELETE FROM material_outline WHERE ingest_id = :i"):
        await conn.execute(text(statement), {"i": ingest_id})


async def collect_pack(conn: AsyncConnection, owner_id: uuid.UUID, pid: str, policy: Policy,
                       seams: Seams) -> bool:
    """FR-INGEST-44's deleter for one pack: lock, re-check, delete; False when it is kept."""
    await packs.lock_packs(conn, [pid])
    ingest_id = await _pack_eligible(conn, owner_id, pid, policy, seams)
    if ingest_id is None:
        return False
    await _drop_pack_data(conn, owner_id, pid, ingest_id, seams)
    await conn.execute(text(
        "UPDATE base_packs SET collected_at = now() WHERE owner_id = :o AND pack_id = :p"),
        {"o": owner_id, "p": pid})
    log.info("ingest gc collected=pack")
    return True


async def collect_overlay(conn: AsyncConnection, owner_id: uuid.UUID, oid: str, policy: Policy,
                          seams: Seams) -> bool:
    """FR-INGEST-44's deleter for one lone overlay, under its pack's lock."""
    pid = (await conn.execute(text("SELECT pack_id FROM overlays WHERE owner_id = :o AND overlay_id = :i"),
                              {"o": owner_id, "i": oid})).scalar()
    if pid is None:
        return False
    await packs.lock_packs(conn, [str(pid)])
    if await _overlay_eligible(conn, owner_id, oid, policy) is None:
        return False
    kind = (await conn.execute(text("SELECT kind FROM overlays WHERE owner_id = :o AND overlay_id = :i"),
                               {"o": owner_id, "i": oid})).scalar_one()
    await _delete_mode(conn)
    await _drop_overlay(conn, owner_id, oid, str(kind), seams)
    log.info("ingest gc collected=overlay")
    return True


async def purge(conn: AsyncConnection, owner_id: uuid.UUID, pid: str, seams: Seams) -> bool:
    """FR-INGEST-45's job: out of the head, then collected without GC's conditions; False when purged."""
    await packs.lock_packs(conn, [pid])
    row = (await conn.execute(text(
        "SELECT ingest_id, purged_at IS NOT NULL FROM base_packs WHERE owner_id = :o AND pack_id = :p"),
        {"o": owner_id, "p": pid})).first()
    if row is None or row[1]:
        return False
    await manifest.remove_pack(conn, owner_id, pid)
    await conn.execute(text("DELETE FROM pending_activations WHERE owner_id = :o AND pack_id = :p"),
                       {"o": owner_id, "p": pid})
    await _drop_pack_data(conn, owner_id, pid, row[0], seams)
    await seams.purge_judge_inputs(conn, pid)
    await seams.purge_judge_requests(conn, owner_id, pid)
    await conn.execute(text("UPDATE base_packs SET purged_at = now() WHERE owner_id = :o AND pack_id = :p"),
                       {"o": owner_id, "p": pid})
    log.info("ingest purged")
    return True


async def _remove_if_unnamed(conn: AsyncConnection, blobs_root: Path, artifact: str) -> bool:
    live = (await conn.execute(text(
        "SELECT 1 FROM ingests i LEFT JOIN base_packs b ON b.ingest_id = i.id WHERE i.artifact_id = :a"
        " AND (b.pack_id IS NULL OR (b.collected_at IS NULL AND b.purged_at IS NULL)) LIMIT 1"),
        {"a": artifact})).first()
    directory = blobs_root / "ingest" / artifact
    if live is not None or not directory.is_dir():
        return False
    await asyncio.to_thread(shutil.rmtree, directory)
    log.info("ingest gc artifact_removed")
    return True


async def remove_artifact(engine: AsyncEngine, blobs_root: Path, artifact: str) -> bool:
    """After the collecting transaction committed: the directory goes when no live ingest names it."""
    async with engine.connect() as conn:
        return await _remove_if_unnamed(conn, blobs_root, artifact)


async def artifact_of(engine: AsyncEngine, owner_id: uuid.UUID, pid: str) -> str | None:
    async with engine.connect() as conn:
        value = (await conn.execute(text(
            "SELECT i.artifact_id FROM base_packs b JOIN ingests i ON i.id = b.ingest_id"
            " WHERE b.owner_id = :o AND b.pack_id = :p"), {"o": owner_id, "p": pid})).scalar()
    return None if value is None else str(value)
