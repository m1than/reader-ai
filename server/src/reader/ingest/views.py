"""Read helpers over the active view for every other module (SRV-INGEST INV-INGEST-16; ADR-0039 B2 P5).

Other modules never name the ingest tables: they find a material's pack through the owner's head manifest
entry (`active_pack`) and read its spans and outline here. A pack and its `ingests` row are one to one
(FR-INGEST-35), so a pack is addressed by its ingest id, the key other modules already store (SRV-PLAN's and
SRV-FRESH's rows). Span ids follow FR-INGEST-33: the locator, except for a pack of a legacy converter
version, whose spans keep the `<material_sha256>:<seq>` form that published citations and the seq map already
hold (FR-INGEST-17, -40, -42).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

import reader.ingest as ingest
from reader.ingest import locator, manifest, spanref

_PACK = (
    "SELECT b.owner_id, b.pack_id, b.ingest_id, b.material_sha256, b.parse_config ->> 'converter_version'"
    " FROM base_packs b"
)
_INT4_MAX: Final = 2**31 - 1  # no seq column holds a larger value: such an id is never found


@dataclass(frozen=True)
class Pack:
    owner_id: uuid.UUID
    pack_id: str
    ingest_id: uuid.UUID
    material_sha256: str
    converter_version: str

    @property
    def legacy_ids(self) -> bool:
        return self.converter_version in ingest.LEGACY_VERSIONS


@dataclass(frozen=True)
class PackSpan:
    span_id: str
    seq: int
    page: int
    kind: str
    text: str
    quote_sha256: str | None


@dataclass(frozen=True)
class OutlineEntry:
    seq: int
    level: int
    title: str
    page: int | None


@dataclass(frozen=True)
class IngestFacts:
    owner_id: uuid.UUID
    material_sha256: str
    converter_version: str
    span_count: int
    imported_at: datetime
    page_count: int


def _pack(row: Any) -> Pack:
    return Pack(row[0], str(row[1]), row[2], str(row[3]), str(row[4]))


def _span_id(pack: Pack, seq: int, loc: str | None) -> str:
    return f"{pack.material_sha256}:{seq}" if pack.legacy_ids or loc is None else loc


async def active_packs(conn: AsyncConnection, owner_id: uuid.UUID) -> dict[str, Pack]:
    """The owner's head entry: material sha256 → its one active base pack (P2: one per material)."""
    view = await manifest.active_view(conn, owner_id)
    pids = [e["pack_id"] for e in view.entry["packs"]]
    if not pids:
        return {}
    rows = await conn.execute(text(_PACK + " WHERE b.owner_id = :o AND b.pack_id = ANY(:p)"),
                              {"o": owner_id, "p": pids})
    return {p.material_sha256: p for p in map(_pack, rows)}


async def active_pack(conn: AsyncConnection, owner_id: uuid.UUID, material_sha256: str) -> Pack | None:
    """The material's pack in the owner's head entry; None without one (not imported, deleted, removed)."""
    return (await active_packs(conn, owner_id)).get(material_sha256)


async def pack_of_ingest(conn: AsyncConnection, ingest_id: uuid.UUID | str) -> Pack | None:
    """The pack of an ingest id, active or not (a stored id may name a superseded or collected pack)."""
    row = (await conn.execute(text(_PACK + " WHERE b.ingest_id = :i"), {"i": ingest_id})).first()
    return None if row is None else _pack(row)


async def material_has_pack(conn: AsyncConnection, material_sha256: str) -> bool:
    found = await conn.execute(text("SELECT 1 FROM base_packs WHERE material_sha256 = :s LIMIT 1"),
                               {"s": material_sha256})
    return found.first() is not None


async def live_packs(conn: AsyncConnection, owner_id: uuid.UUID | None = None) -> list[Pack]:
    """Sealed packs that are neither collected nor purged (every owner when `owner_id` is None)."""
    sql = _PACK + " WHERE b.state = 'sealed' AND b.collected_at IS NULL AND b.purged_at IS NULL"
    if owner_id is not None:
        sql += " AND b.owner_id = :o"
    rows = await conn.execute(text(sql + " ORDER BY b.owner_id, b.material_sha256, b.created_at"),
                              {"o": owner_id})
    return [_pack(r) for r in rows]


async def pack_spans(
    conn: AsyncConnection, pack: Pack, first: int | None = None, last: int | None = None
) -> list[PackSpan]:
    """The pack's spans in reading order, optionally pages first..last."""
    sql = ("SELECT seq, page, kind, text, locator, quote_sha256 FROM material_spans"
           " WHERE ingest_id = :i AND owner_id = :o")
    if first is not None and last is not None:
        sql += " AND page BETWEEN :f AND :l"
    rows = await conn.execute(text(sql + " ORDER BY seq"),
                              {"i": pack.ingest_id, "o": pack.owner_id, "f": first, "l": last})
    return [PackSpan(_span_id(pack, int(r[0]), r[4]), int(r[0]), int(r[1]), str(r[2]), str(r[3]),
                     None if r[5] is None else str(r[5])) for r in rows]


async def span_ids(conn: AsyncConnection, pack: Pack, seqs: Sequence[int]) -> dict[int, str]:
    """seq → span id for the given seqs of the pack."""
    if pack.legacy_ids:
        return {s: f"{pack.material_sha256}:{s}" for s in seqs}
    rows = await conn.execute(text(
        "SELECT seq, locator FROM material_spans WHERE ingest_id = :i AND owner_id = :o AND seq = ANY(:s)"),
        {"i": pack.ingest_id, "o": pack.owner_id, "s": list(seqs)})
    return {int(r[0]): _span_id(pack, int(r[0]), r[1]) for r in rows}


async def page_stats(conn: AsyncConnection, pack: Pack) -> list[tuple[int, int, int]]:
    """(page, span characters, spans) per page of the pack."""
    rows = await conn.execute(text(
        "SELECT page, sum(char_length(text)), count(*) FROM material_spans"
        " WHERE ingest_id = :i AND owner_id = :o GROUP BY page ORDER BY page"),
        {"i": pack.ingest_id, "o": pack.owner_id})
    return [(int(r[0]), int(r[1]), int(r[2])) for r in rows]


async def span_chars(conn: AsyncConnection, pack: Pack, first: int, last: int) -> int:
    value = (await conn.execute(text(
        "SELECT coalesce(sum(char_length(text)), 0) FROM material_spans"
        " WHERE ingest_id = :i AND owner_id = :o AND page BETWEEN :f AND :l"),
        {"i": pack.ingest_id, "o": pack.owner_id, "f": first, "l": last})).scalar()
    return int(value or 0)


async def pack_outline(conn: AsyncConnection, pack: Pack) -> list[OutlineEntry]:
    rows = await conn.execute(text(
        "SELECT seq, level, title, page FROM material_outline WHERE ingest_id = :i AND owner_id = :o"
        " ORDER BY seq"), {"i": pack.ingest_id, "o": pack.owner_id})
    return [OutlineEntry(int(r[0]), int(r[1]), str(r[2]), None if r[3] is None else int(r[3])) for r in rows]


async def ingest_facts(conn: AsyncConnection, ingest_id: uuid.UUID | str) -> IngestFacts | None:
    """An ingest's recorded row (SRV-OBS's Run of an import), whatever its pack's state."""
    row = (await conn.execute(text(
        "SELECT i.owner_id, m.sha256, i.converter_version, i.span_count, i.imported_at, m.page_count"
        " FROM ingests i JOIN materials m ON m.id = i.material_id AND m.owner_id = i.owner_id"
        " WHERE i.id = :i"),
        {"i": ingest_id})).first()
    if row is None:
        return None
    return IngestFacts(row[0], str(row[1]), str(row[2]), int(row[3] or 0), row[4], int(row[5] or 0))


async def active_span_texts(
    conn: AsyncConnection, owner_id: uuid.UUID, ids: Sequence[str]
) -> dict[str, tuple[int, str]]:
    """span id → (page, text) from the owner's active packs only.

    A legacy id reads its seq in a legacy active pack, and otherwise goes through the resolver's accepted seq
    map (FR-INGEST-42); a locator reads the active pack's span with that locator."""
    heads = await active_packs(conn, owner_id)
    found: dict[str, tuple[int, str]] = {}
    mapped: list[str] = []
    for ref in dict.fromkeys(ids):
        if locator.is_legacy_id(ref):
            sha, seq = ref.split(":")
            pack = heads.get(sha)
            if pack is None or int(seq) > _INT4_MAX:
                continue
            if not pack.legacy_ids:
                mapped.append(ref)
                continue
            row = (await conn.execute(text(
                "SELECT page, text FROM material_spans WHERE ingest_id = :i AND owner_id = :o AND seq = :s"),
                {"i": pack.ingest_id, "o": owner_id, "s": int(seq)})).first()
        else:
            try:
                parsed = locator.parse(ref)
            except locator.LocatorError:
                continue
            pack = heads.get(parsed.material_sha256)
            if pack is None:
                continue
            row = (await conn.execute(text(
                "SELECT page, text FROM material_spans WHERE ingest_id = :i AND owner_id = :o"
                " AND locator = :l"),
                {"i": pack.ingest_id, "o": owner_id, "l": ref})).first()
        if row is not None:
            found[ref] = (int(row[0]), str(row[1]))
    resolved = await spanref.resolve(conn, owner_id, [(r, None) for r in mapped])
    for ref, got in zip(mapped, resolved, strict=True):
        if got.text is not None and got.page is not None and got.state in ("active", "reanchored"):
            found[ref] = (got.page, got.text)
    return found
