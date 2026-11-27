"""Re-anchoring between two packs of one material (SRV-INGEST FR-INGEST-43).

An old span maps to the new span on the same page with the same quote hash and the same equal-hash
ordinal; when the number of such spans on that page differs between the packs, or there is none, it is
orphaned (never guessed).
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Sequence
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

log = logging.getLogger("reader.ingest")
KIND: Final = "ingest.reanchor"


async def _spans(conn: AsyncConnection, owner_id: uuid.UUID, pid: str) -> Sequence[Any]:
    return (await conn.execute(text(
        "SELECT s.page, s.locator, s.quote_sha256 FROM material_spans s"
        " JOIN base_packs b ON b.ingest_id = s.ingest_id WHERE b.owner_id = :o AND b.pack_id = :p"
        " ORDER BY s.seq"), {"o": owner_id, "p": pid})).all()


def _groups(spans: Sequence[Any]) -> dict[tuple[int, str], list[str]]:
    """(page, quote hash) → locators in reading order: a span's index is its equal-hash ordinal."""
    out: dict[tuple[int, str], list[str]] = defaultdict(list)
    for page, loc, quote in spans:
        out[(int(page), str(quote))].append(str(loc))
    return out


async def run(conn: AsyncConnection, owner_id: uuid.UUID, from_pack: str, to_pack: str) -> tuple[int, int]:
    """The pair's anchor_maps rows and anchor_runs row; an existing run is reused. (mapped, orphaned)."""
    done = (await conn.execute(text(
        "SELECT mapped, orphaned FROM anchor_runs"
        " WHERE owner_id = :o AND from_pack_id = :f AND to_pack_id = :t"),
        {"o": owner_id, "f": from_pack, "t": to_pack})).first()
    if done is not None:
        return int(done[0]), int(done[1])
    old = _groups(await _spans(conn, owner_id, from_pack))
    new = _groups(await _spans(conn, owner_id, to_pack))
    rows: list[dict[str, Any]] = []
    for key, locators in old.items():
        targets = new.get(key, [])
        same = len(targets) == len(locators)
        for ordinal, loc in enumerate(locators):
            rows.append({"o": owner_id, "f": from_pack, "t": to_pack, "fl": loc, "q": key[1],
                         "tl": targets[ordinal] if same else None, "s": "mapped" if same else "orphaned"})
    if rows:
        await conn.execute(text(
            "INSERT INTO anchor_maps (owner_id, from_pack_id, to_pack_id, from_locator, quote_sha256,"
            " to_locator, state) VALUES (:o, :f, :t, :fl, :q, :tl, :s)"), rows)
    mapped = sum(r["s"] == "mapped" for r in rows)
    await conn.execute(text(
        "INSERT INTO anchor_runs (owner_id, from_pack_id, to_pack_id, mapped, orphaned, finished_at)"
        " VALUES (:o, :f, :t, :m, :x, now())"),
        {"o": owner_id, "f": from_pack, "t": to_pack, "m": mapped, "x": len(rows) - mapped})
    log.info("ingest reanchor mapped=%d orphaned=%d", mapped, len(rows) - mapped)
    return mapped, len(rows) - mapped
