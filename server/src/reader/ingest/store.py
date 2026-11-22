"""Ingest rows: frozen versions, the import, and the api's status map (SRV-INGEST FR-INGEST-13..15, -17)."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from reader.ingest.artifact import open_members, validate

log = logging.getLogger("reader.ingest")
_BATCH: Final = 1000  # rows per INSERT: bounds one statement's parameters, not a tuning value


async def owner_id(conn: AsyncConnection) -> uuid.UUID | None:
    rows = await conn.execute(text("SELECT id FROM users WHERE role = 'owner' ORDER BY created_at LIMIT 2"))
    found = [r[0] for r in rows]
    return found[0] if len(found) == 1 else None


async def frozen_version(conn: AsyncConnection, material_sha256: str) -> str | None:
    """FR-INGEST-17: the converter version of the material's one `ingests` row, if any."""
    row = await conn.execute(
        text(
            "SELECT i.converter_version FROM ingests i JOIN materials m ON m.id = i.material_id"
            " WHERE m.sha256 = :s"
        ),
        {"s": material_sha256},
    )
    value = row.scalar()
    return None if value is None else str(value)


def read_artifact(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate the stored artifact (FR-INGEST-11's validator) and return manifest, spans and outline."""
    with open_members(root) as handles:
        manifest_bytes = handles["manifest.json"].read()
        outline_bytes = handles["outline.json"].read()
        manifest = validate(manifest_bytes, handles["spans.jsonl"], outline_bytes)
        handles["spans.jsonl"].seek(0)
        spans = [json.loads(line) for line in handles["spans.jsonl"]]
    return manifest, spans, json.loads(outline_bytes)


async def _insert_many(conn: AsyncConnection, sql: str, rows: Sequence[dict[str, Any]]) -> None:
    for start in range(0, len(rows), _BATCH):
        await conn.execute(text(sql), list(rows[start : start + _BATCH]))


async def import_rows(
    conn: AsyncConnection,
    owner: uuid.UUID,
    material_id: str,
    artifact_id: str,
    manifest: dict[str, Any],
    spans: Sequence[dict[str, Any]],
    outline: Sequence[dict[str, Any]],
) -> bool:
    """Write the material, its ingest and its rows in the caller's transaction; False if already imported."""
    material = (
        await conn.execute(
            text(
                "INSERT INTO materials (owner_id, sha256, config_id, page_count)"
                " VALUES (:o, :s, :c, :p)"
                " ON CONFLICT (owner_id, sha256) DO UPDATE SET config_id = excluded.config_id,"
                " page_count = excluded.page_count RETURNING id"
            ),
            {"o": owner, "s": manifest["material_sha256"], "c": material_id, "p": manifest["page_count"]},
        )
    ).scalar_one()
    ingest = (
        await conn.execute(
            text(
                "INSERT INTO ingests (owner_id, material_id, converter_version, artifact_id, span_count,"
                " outline_count, outline_source, pages_without_text)"
                " VALUES (:o, :m, :v, :a, :sc, :oc, :src, :pwt)"
                " ON CONFLICT (material_id) DO NOTHING RETURNING id"
            ),
            {
                "o": owner,
                "m": material,
                "v": manifest["converter_version"],
                "a": artifact_id,
                "sc": manifest["span_count"],
                "oc": manifest["outline_count"],
                "src": manifest["outline_source"],
                "pwt": list(manifest["pages_without_text"]),
            },
        )
    ).scalar()
    if ingest is None:
        return False
    await _insert_many(
        conn,
        "INSERT INTO material_spans (ingest_id, owner_id, seq, page, kind, level, decoded, text,"
        " x0, y0, x1, y1) VALUES (:i, :o, :seq, :page, :kind, :level, :decoded, :text, :x0, :y0, :x1, :y1)",
        [
            {
                "i": ingest,
                "o": owner,
                "seq": s["seq"],
                "page": s["page"],
                "kind": s["kind"],
                "level": s["level"],
                "decoded": s["decoded"],
                "text": s["text"],
                "x0": s["bbox"][0],
                "y0": s["bbox"][1],
                "x1": s["bbox"][2],
                "y1": s["bbox"][3],
            }
            for s in spans
        ],
    )
    await _insert_many(
        conn,
        "INSERT INTO material_outline (ingest_id, owner_id, seq, level, title, page)"
        " VALUES (:i, :o, :seq, :level, :title, :page)",
        [
            {"i": ingest, "o": owner, **{k: e[k] for k in ("seq", "level", "title", "page")}}
            for e in outline
        ],
    )
    return True


def api_status(job_status: str, ingested: bool) -> str:
    """VEC-INGEST-5: an `ingests` row means imported; otherwise the job status decides."""
    if ingested:
        return "imported"
    if job_status == "succeeded":
        log.warning("ingest status inconsistent: succeeded without an ingests row")
        return "dead"
    return {"none": "not_ingested", "queued": "queued", "running": "running", "dead": "dead"}.get(
        job_status, "not_ingested"
    )
