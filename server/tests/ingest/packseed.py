"""Test helper for other modules' [db] fixtures: turn a fixture's bare ingest into a sealed base pack and, by
default, make it the material's active pack (SRV-INGEST FR-INGEST-35, -38; INV-INGEST-16).

Other modules' fixtures insert `ingests` and `material_spans` rows directly. Readers now find spans only
through the owner's head manifest entry, so a fixture seals its ingest here: locators and quote hashes are
derived from the span texts (FR-INGEST-33, -34), the pack row is written `building → sealed`, and the head
entry gains the pack in place of any other pack of the material.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text

R2 = "docling-2.151.1+r2"
R3 = "docling-2.151.1+r3"


async def seal(conn: Any, owner_id: uuid.UUID, ingest_id: uuid.UUID, material_sha256: str,
               version: str = R2, *, activate: bool = True) -> str:
    """Seal the ingest as the pack of `version` (its parse config); activate it unless told not to."""
    from reader.ingest import PARSE_CONFIGS, locator
    from reader.ingest.artifact import pack_id

    config = dict(PARSE_CONFIGS[version])
    pid = pack_id(material_sha256, config)
    await conn.execute(text(
        "INSERT INTO base_packs (owner_id, pack_id, material_sha256, ingest_id, parse_config, state)"
        " VALUES (:o, :p, :s, :i, CAST(:c AS jsonb), 'building')"),
        {"o": owner_id, "p": pid, "s": material_sha256, "i": ingest_id, "c": json.dumps(config)})
    rows = (await conn.execute(text(
        "SELECT seq, page, text FROM material_spans WHERE ingest_id = :i ORDER BY page, seq"),
        {"i": ingest_id})).all()
    pages: dict[int, list[Any]] = {}
    for row in rows:
        pages.setdefault(int(row[1]), []).append(row)
    for page, page_rows in pages.items():
        ids = locator.page_locators(material_sha256, page, [str(r[2]) for r in page_rows])
        for row, loc in zip(page_rows, ids, strict=True):
            await conn.execute(text(
                "UPDATE material_spans SET locator = :l, quote_sha256 = :q"
                " WHERE ingest_id = :i AND seq = :s"),
                {"l": loc, "q": locator.quote_sha256(str(row[2])), "i": ingest_id, "s": row[0]})
    await conn.execute(text(
        "UPDATE base_packs SET state = 'sealed', sealed_at = now() WHERE owner_id = :o AND pack_id = :p"),
        {"o": owner_id, "p": pid})
    if activate:
        await activate_pack(conn, owner_id, pid)
    return pid


async def add_pack(conn: Any, owner_id: uuid.UUID, material_sha256: str, version: str,
                   spans: list[tuple[int, str, str]], *, activate: bool = False) -> str:
    """A second ingest of an existing material under `version`, spans (page, kind, text) in reading order,
    sealed as its pack and, unless told otherwise, left inactive (the head keeps the current pack)."""
    ingest_id = (await conn.execute(text(
        "INSERT INTO ingests (owner_id, material_id, converter_version, artifact_id, span_count,"
        " outline_count, outline_source, pages_without_text)"
        " SELECT :o, m.id, :v, :a, :n, 0, 'headings', '{}' FROM materials m"
        " WHERE m.owner_id = :o AND m.sha256 = :s RETURNING id"),
        {"o": owner_id, "v": version, "a": "e" * 64, "n": len(spans), "s": material_sha256})).scalar_one()
    for seq, (page, kind, body) in enumerate(spans):
        await conn.execute(text(
            "INSERT INTO material_spans (ingest_id, owner_id, seq, page, kind, text, x0, y0, x1, y1)"
            " VALUES (:i, :o, :q, :p, :k, :t, 0, 0, 1, 1)"),
            {"i": ingest_id, "o": owner_id, "q": seq, "p": page, "k": kind, "t": body})
    return await seal(conn, owner_id, ingest_id, material_sha256, version, activate=activate)


async def activate_pack(conn: Any, owner_id: uuid.UUID, pid: str) -> None:
    """Append a head entry holding `pid` in place of any other pack of its material."""
    from reader.ingest import manifest, packs

    sha = await packs.material_of(conn, owner_id, pid)
    seq, top = await manifest.head(conn, owner_id)
    elements = {e["pack_id"]: dict(e["overlays"]) for e in top["packs"]
                if await packs.material_of(conn, owner_id, e["pack_id"]) != sha}
    elements[pid] = {}
    await manifest.append(conn, owner_id, seq, manifest.make_entry(elements), "test_fixture")
