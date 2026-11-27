"""The read-time resolver of citation references (SRV-INGEST FR-INGEST-42). It never writes.

Every reference gets exactly one state, decided in order: invalid, unmapped, active, reanchored, inactive,
removed, orphaned. Text is returned only from a span whose quote hash equals the reference's, never logged.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from reader.ingest import locator, manifest, packs

_HEX64: Final = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Resolved:
    locator: str | None
    quote_sha256: str | None
    page: int | None
    text: str | None
    state: str


async def _legacy(conn: AsyncConnection, owner_id: uuid.UUID, ref: str) -> tuple[str, str] | None:
    sha, seq = ref.split(":")
    row = (await conn.execute(text(
        "SELECT m.locator, m.quote_sha256 FROM span_seq_map m"
        " JOIN seq_maps s ON s.owner_id = m.owner_id AND s.from_ingest_id = m.from_ingest_id"
        " AND s.to_pack_id = m.to_pack_id AND s.state = 'accepted'"
        " JOIN ingests i ON i.id = m.from_ingest_id JOIN materials mt ON mt.id = i.material_id"
        " WHERE m.owner_id = :o AND mt.sha256 = :s AND m.seq = :q"),
        {"o": owner_id, "s": sha, "q": int(seq)})).first()
    return None if row is None else (str(row[0]), str(row[1]))


async def legacy_target(conn: AsyncConnection, owner_id: uuid.UUID, ref: str) -> tuple[str, str] | None:
    """The accepted map's (locator, quote_sha256) for a legacy id, else None (FR-INGEST-41 shares it)."""
    return await _legacy(conn, owner_id, ref) if locator.is_legacy_id(ref) else None


async def _span_text(
    conn: AsyncConnection, owner_id: uuid.UUID, pid: str, loc: str, quote: str
) -> str | None:
    value = (await conn.execute(text(
        "SELECT s.text FROM material_spans s JOIN base_packs b ON b.ingest_id = s.ingest_id"
        " WHERE b.owner_id = :o AND b.pack_id = :p AND s.locator = :l AND s.quote_sha256 = :q"),
        {"o": owner_id, "p": pid, "l": loc, "q": quote})).first()
    return None if value is None else str(value[0])


def _order(loc: str) -> tuple[Any, ...]:
    parsed = locator.parse(loc)
    return (parsed.page or 0, parsed.start, parsed.end, parsed.ordinal or 0, parsed.path or "",
            parsed.line_start)


async def _hops(
    conn: AsyncConnection, owner_id: uuid.UUID, pid: str, loc: str, quote: str
) -> list[tuple[str, str]]:
    rows = await conn.execute(text(
        "SELECT to_pack_id, to_locator FROM anchor_maps WHERE owner_id = :o AND from_pack_id = :p"
        " AND from_locator = :l AND quote_sha256 = :q AND state = 'mapped'"),
        {"o": owner_id, "p": pid, "l": loc, "q": quote})
    return [(str(r[0]), str(r[1])) for r in rows]


async def _reanchored(
    conn: AsyncConnection, owner_id: uuid.UUID, active: str, loc: str, quote: str
) -> str | None:
    """Walk every anchor chain from every starting pack; the lowest active locator reached, if any."""
    starts = [str(r[0]) for r in await conn.execute(text(
        "SELECT DISTINCT from_pack_id FROM anchor_maps WHERE owner_id = :o AND from_locator = :l"
        " AND quote_sha256 = :q ORDER BY from_pack_id"), {"o": owner_id, "l": loc, "q": quote})]
    reached: set[str] = set()
    stack: list[tuple[str, str, frozenset[str]]] = [(p, loc, frozenset({p})) for p in starts]
    while stack:
        pid, at, seen = stack.pop()
        for to_pack, to_loc in await _hops(conn, owner_id, pid, at, quote):
            if to_pack in seen or await _span_text(conn, owner_id, to_pack, to_loc, quote) is None:
                continue  # a cycle, or a target whose hash differs (or whose rows are gone): the chain ends
            if to_pack == active:
                reached.add(to_loc)
            else:
                stack.append((to_pack, to_loc, seen | {to_pack}))
    return min(reached, key=_order) if reached else None


async def _one(conn: AsyncConnection, owner_id: uuid.UUID, head: dict[str, str], ref: str,
               quote: str | None) -> Resolved:
    if locator.is_legacy_id(ref):
        target = await _legacy(conn, owner_id, ref)
        if target is None:
            return Resolved(None, None, None, None, "unmapped")
        ref, quote = target
    elif quote is None or not _HEX64.match(quote):
        return Resolved(None, None, None, None, "invalid")
    try:
        parsed = locator.parse(ref)
    except locator.LocatorError:
        return Resolved(None, None, None, None, "invalid")
    sha, page = parsed.material_sha256, parsed.page

    def done(state: str, body: str | None, loc: str = ref) -> Resolved:
        return Resolved(loc, quote, page, body, state)

    active = head.get(sha)
    if active is not None:
        body = await _span_text(conn, owner_id, active, ref, quote)
        if body is not None:
            return done("active", body)
        loc = await _reanchored(conn, owner_id, active, ref, quote)
        if loc is not None:
            return done("reanchored", await _span_text(conn, owner_id, active, loc, quote), loc)
    held = (await conn.execute(text(
        "SELECT s.text FROM material_spans s JOIN base_packs b ON b.ingest_id = s.ingest_id"
        " WHERE b.owner_id = :o AND b.material_sha256 = :m AND b.state = 'sealed' AND b.collected_at IS NULL"
        " AND b.purged_at IS NULL AND s.locator = :l AND s.quote_sha256 = :q ORDER BY b.pack_id LIMIT 1"),
        {"o": owner_id, "m": sha, "l": ref, "q": quote})).first()
    if held is not None:
        return done("inactive", str(held[0]))
    gone = (await conn.execute(text(
        "SELECT 1 FROM base_packs WHERE owner_id = :o AND material_sha256 = :m"
        " AND (collected_at IS NOT NULL OR purged_at IS NOT NULL)"), {"o": owner_id, "m": sha})).first()
    return done("removed" if gone is not None else "orphaned", None)


async def resolve(conn: AsyncConnection, owner_id: uuid.UUID,
                  refs: Sequence[tuple[str, str | None]]) -> list[Resolved]:
    """Each (span_ref, quote_sha256 or None for a legacy id) → one Resolved, in order."""
    view = await manifest.active_view(conn, owner_id)
    head: dict[str, str] = {}
    for element in view.entry["packs"]:
        sha = await packs.material_of(conn, owner_id, element["pack_id"])
        if sha is not None:
            head[sha] = element["pack_id"]
    return [await _one(conn, owner_id, head, ref, quote) for ref, quote in refs]
