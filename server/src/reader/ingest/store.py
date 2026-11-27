"""Ingest rows: frozen versions, the import, and the api's status map (SRV-INGEST FR-INGEST-13..15, -17)."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from reader.ingest import PARSE_CONFIGS, locator, overlays, packs
from reader.ingest import manifest as manifests
from reader.ingest.artifact import read_members, validate_members

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


def read_artifact(root: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Read the stored artifact once and validate it (FR-INGEST-11's validator): manifest and member bytes.

    Memory stays at the caps: the bytes (at most 64 MiB of spans) are kept and parsed in batches later.
    """
    members = read_members(root)
    return validate_members(members), members


def span_batches(spans: bytes) -> Iterator[list[dict[str, Any]]]:
    """The validated spans.jsonl, parsed _BATCH lines at a time, never all spans as objects at once."""
    batch: list[dict[str, Any]] = []
    for line in spans.splitlines():
        batch.append(json.loads(line))
        if len(batch) == _BATCH:
            yield batch
            batch = []
    if batch:
        yield batch


async def _insert_many(conn: AsyncConnection, sql: str, rows: Sequence[dict[str, Any]]) -> None:
    for start in range(0, len(rows), _BATCH):
        await conn.execute(text(sql), list(rows[start : start + _BATCH]))


async def import_rows(
    conn: AsyncConnection,
    owner: uuid.UUID,
    material_id: str,
    artifact_id: str,
    manifest: dict[str, Any],
    spans: bytes,
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
                " ON CONFLICT (material_id, converter_version) DO NOTHING RETURNING id"
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
    sha = str(manifest["material_sha256"])
    version = str(manifest["converter_version"])
    pid = packs.pack_id(sha, version)
    await conn.execute(
        text(
            "INSERT INTO base_packs (owner_id, pack_id, material_sha256, ingest_id, parse_config, state)"
            " VALUES (:o, :p, :s, :i, CAST(:c AS jsonb), 'building')"
        ),
        {"o": owner, "p": pid, "s": sha, "i": ingest, "c": json.dumps(dict(PARSE_CONFIGS[version]))},
    )
    # FR-INGEST-33/-34: a first pass keeps each page's span lengths (ints, not texts) for the locators.
    lengths: dict[int, list[int]] = {}
    for batch in span_batches(spans):
        for s in batch:
            lengths.setdefault(int(s["page"]), []).append(len(s["text"]))
    ranges = {page: iter(locator.ranges_of_lengths(found)) for page, found in lengths.items()}
    span_sql = (
        "INSERT INTO material_spans (ingest_id, owner_id, seq, page, kind, level, decoded, text,"
        " x0, y0, x1, y1, locator, quote_sha256) VALUES (:i, :o, :seq, :page, :kind, :level, :decoded, :text,"
        " :x0, :y0, :x1, :y1, :loc, :q)"
    )
    for batch in span_batches(spans):
        rows = [
            {
                "i": ingest,
                "o": owner,
                **{k: s[k] for k in ("seq", "page", "kind", "level", "decoded", "text")},
                **dict(zip(("x0", "y0", "x1", "y1"), s["bbox"], strict=True)),
                "loc": locator.pdf_locator(sha, int(s["page"]), *next(ranges[int(s["page"])])),
                "q": locator.quote_sha256(s["text"]),
            }
            for s in batch
        ]
        await conn.execute(text(span_sql), rows)
    await _insert_many(
        conn,
        "INSERT INTO material_outline (ingest_id, owner_id, seq, level, title, page)"
        " VALUES (:i, :o, :seq, :level, :title, :page)",
        [
            {"i": ingest, "o": owner, **{k: e[k] for k in ("seq", "level", "title", "page")}}
            for e in outline
        ],
    )
    # FR-INGEST-35, -36, -39: seal, build the profile overlay, request the activation (no try_activate).
    await conn.execute(
        text("UPDATE base_packs SET state = 'sealed', sealed_at = now()"
             " WHERE owner_id = :o AND pack_id = :p"),
        {"o": owner, "p": pid},
    )
    rule = await packs.rule_values(conn, sha, packs.load_materials())
    profile = await overlays.build_profile(conn, owner, pid, rule or {})
    await manifests.request_activation(conn, owner, pid, {"profile": profile})
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
