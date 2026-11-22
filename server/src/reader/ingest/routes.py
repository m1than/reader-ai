"""Material routes (SRV-INGEST FR-INGEST-15), declared as data for SRV-AUTH's ROUTERS.

Every handler requires the owner role: sharing is deferred and materials may be `personal-use` (ADR-0008).
The listed materials are reader.yaml's (`app.state.ingest_materials` in tests, else READER_CONFIG).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.responses import JSONResponse, Response

import reader.ingest as ingest
from reader.api.http import Ctx, Handler, Route, error
from reader.config import ConfigError, load_config
from reader.config.model import Material
from reader.ingest import store
from reader.ingest.artifact import span_id
from reader.ingest.jobs import KIND, key_for
from reader.jobs.queue import job_status_of

DEFAULT_CONFIG: Final = "/etc/reader/reader.yaml"
PAGE_DIGITS: Final = 5  # page_count <= 5,000 (artifact caps)


class _NoConfig(Exception):
    pass


def _materials(ctx: Ctx) -> Sequence[Material]:
    injected = getattr(ctx.request.app.state, "ingest_materials", None)
    if injected is not None:
        return tuple(injected)
    try:
        return load_config(os.environ.get("READER_CONFIG", DEFAULT_CONFIG)).materials
    except ConfigError:
        raise _NoConfig from None


async def _job_status(conn: AsyncConnection, key: str) -> str:
    """SRV-EVENTS job_status on this connection: the api holds no procrastinate app."""
    return await job_status_of(conn, KIND, key)


async def _imported(conn: AsyncConnection, owner: Any, sha: str) -> Any:
    return (
        await conn.execute(
            text(
                "SELECT i.id, i.converter_version, m.page_count, i.span_count, i.outline_count,"
                " i.pages_without_text FROM ingests i JOIN materials m ON m.id = i.material_id"
                " WHERE m.owner_id = :o AND m.sha256 = :s"
            ),
            {"o": owner, "s": sha},
        )
    ).first()


def _owner_only(ctx: Ctx) -> Response | None:
    return None if ctx.signed_in.role == "owner" else error(403, "owner_only")


async def list_materials(ctx: Ctx) -> Response:
    if (refused := _owner_only(ctx)) is not None:
        return refused
    listed: list[dict[str, Any]] = []
    async with ctx.engine.connect() as conn:
        for material in _materials(ctx):
            row = await _imported(conn, ctx.signed_in.id, material.sha256)
            version = row[1] if row else ingest.CONVERTER_VERSION
            state = await _job_status(conn, key_for(material.sha256, version))
            listed.append(
                {
                    "id": material.id,
                    "sha256": material.sha256,
                    "language": material.language,
                    "license": material.license,
                    "status": store.api_status(state, row is not None),
                    "converter_version": row[1] if row else None,
                    "page_count": row[2] if row else None,
                    "span_count": row[3] if row else None,
                    "outline_count": row[4] if row else None,
                    "pages_without_text": list(row[5]) if row else None,
                }
            )
    return JSONResponse({"materials": listed})


async def _material_ingest(ctx: Ctx, conn: AsyncConnection) -> tuple[Material, Any] | None:
    material = next((m for m in _materials(ctx) if m.id == ctx.request.path_params["material_id"]), None)
    if material is None:
        return None
    row = await _imported(conn, ctx.signed_in.id, material.sha256)
    return None if row is None else (material, row)


async def get_outline(ctx: Ctx) -> Response:
    if (refused := _owner_only(ctx)) is not None:
        return refused
    async with ctx.engine.connect() as conn:
        found = await _material_ingest(ctx, conn)
        if found is None:
            return error(404, "not_found")
        rows = await conn.execute(
            text("SELECT seq, level, title, page FROM material_outline WHERE ingest_id = :i ORDER BY seq"),
            {"i": found[1][0]},
        )
        entries = [{"seq": r[0], "level": r[1], "title": r[2], "page": r[3]} for r in rows]
        return JSONResponse({"outline": entries})


async def get_spans(ctx: Ctx) -> Response:
    if (refused := _owner_only(ctx)) is not None:
        return refused
    async with ctx.engine.connect() as conn:
        found = await _material_ingest(ctx, conn)
        if found is None:
            return error(404, "not_found")
        material, row = found
        raw = ctx.request.query_params.get("page", "")
        valid = raw.isascii() and raw.isdecimal() and len(raw) <= PAGE_DIGITS
        if not valid or not 1 <= int(raw) <= int(row[2]):
            return error(400, "page")
        spans = await conn.execute(
            text(
                "SELECT seq, page, kind, level, decoded, text, x0, y0, x1, y1 FROM material_spans"
                " WHERE ingest_id = :i AND page = :p ORDER BY seq"
            ),
            {"i": row[0], "p": int(raw)},
        )
        return JSONResponse(
            {
                "spans": [
                    {
                        "span_id": span_id(material.sha256, s[0]),
                        "seq": s[0],
                        "page": s[1],
                        "kind": s[2],
                        "level": s[3],
                        "decoded": s[4],
                        "text": s[5],
                        "bbox": [round(float(v), 2) for v in s[6:10]],  # `real` columns: back to 0.01
                    }
                    for s in spans
                ]
            }
        )


def _config_guard(handler: Handler) -> Handler:
    async def guarded(ctx: Ctx) -> Response:
        try:
            return await handler(ctx)
        except _NoConfig:
            return error(503, "config_unavailable")

    return guarded


ROUTES: tuple[Route, ...] = (
    Route("GET", "/api/materials", "session", _config_guard(list_materials)),
    Route("GET", "/api/materials/{material_id}/outline", "session", _config_guard(get_outline)),
    Route("GET", "/api/materials/{material_id}/spans", "session", _config_guard(get_spans)),
)
