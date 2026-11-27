"""The seq-to-locator map from a legacy ingest to a re-converted pack (SRV-INGEST FR-INGEST-40 step 3).

The map is accepted only when the two span lists pair 1:1 by seq with the same page, kind and quote hash
(INV-INGEST-15); otherwise one refused row names the first mismatch and its seq, never a text.
"""

from __future__ import annotations

import logging
import uuid
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from reader.ingest import manifest

log = logging.getLogger("reader.ingest")
KIND: Final = "ingest.seqmap"
_SPANS: Final = (
    "SELECT seq, page, kind, locator, quote_sha256 FROM material_spans WHERE ingest_id = :i ORDER BY seq"
)


def first_mismatch(old: list[tuple[int, str, str]], new: list[tuple[int, str, str]]) -> str | None:
    """`<reason>:<seq>` of the first pair that differs in page, kind or quote hash, else a count mismatch."""
    for seq, (a, b) in enumerate(zip(old, new, strict=False)):
        for reason, x, y in zip(("page", "kind", "text"), a, b, strict=True):
            if x != y:
                return f"{reason}:{seq}"
    return None if len(old) == len(new) else f"count:{min(len(old), len(new))}"


async def build(
    conn: AsyncConnection, owner_id: uuid.UUID, from_ingest_id: uuid.UUID, to_pack_id: str
) -> str:
    """`accepted`, `refused:<reason>:<seq>`, or `exists` when an accepted map from this ingest or a row for
    this pair is already there."""
    found = (await conn.execute(text(
        "SELECT 1 FROM seq_maps WHERE owner_id = :o AND from_ingest_id = :i"
        " AND (state = 'accepted' OR to_pack_id = :p)"),
        {"o": owner_id, "i": from_ingest_id, "p": to_pack_id})).first()
    if found is not None:
        return "exists"
    to_ingest = (await conn.execute(text(
        "SELECT ingest_id FROM base_packs WHERE owner_id = :o AND pack_id = :p AND state = 'sealed'"),
        {"o": owner_id, "p": to_pack_id})).scalar_one()
    old = (await conn.execute(text(_SPANS), {"i": from_ingest_id})).all()
    new = (await conn.execute(text(_SPANS), {"i": to_ingest})).all()
    mismatch = first_mismatch([(r[1], r[2], r[4]) for r in old], [(r[1], r[2], r[4]) for r in new])
    await conn.execute(text(
        "INSERT INTO seq_maps (owner_id, from_ingest_id, to_pack_id, state, reason, span_count)"
        " VALUES (:o, :i, :p, :s, :r, :n)"),
        {"o": owner_id, "i": from_ingest_id, "p": to_pack_id, "s": "refused" if mismatch else "accepted",
         "r": mismatch, "n": len(old)})
    if mismatch is not None:
        log.warning("ingest seqmap_refused %s", mismatch)  # the owner's signal: the task blocks (P2 (3))
        return f"refused:{mismatch}"
    rows = [{"o": owner_id, "i": from_ingest_id, "p": to_pack_id, "seq": r[0], "loc": n[3], "q": n[4]}
            for r, n in zip(old, new, strict=True)]
    if rows:
        await conn.execute(text(
            "INSERT INTO span_seq_map (owner_id, from_ingest_id, to_pack_id, seq, locator, quote_sha256)"
            " VALUES (:o, :i, :p, :seq, :loc, :q)"), rows)
    await manifest.try_activate(conn, owner_id, to_pack_id)
    return "accepted"
