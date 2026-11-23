"""Material routes (SRV-INGEST FR-INGEST-15, -18, -19, -23), declared as data for SRV-AUTH's ROUTERS.

Every handler requires the owner role: sharing is deferred and materials may be `personal-use` (ADR-0008).
The listed materials are reader.yaml's (`app.state.ingest_materials` in tests, else READER_CONFIG), then the
owner's uploads, newest first. Every read of an upload applies lease expiry first (FR-INGEST-19).
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.responses import JSONResponse, Response

import reader.ingest as ingest
from reader.api.http import Ctx, Handler, Route, error
from reader.config import ConfigError, load_config
from reader.config.model import Material
from reader.ingest import params, store, upload, uploads
from reader.ingest.artifact import span_id
from reader.ingest.jobs import KIND, key_for
from reader.jobs.queue import job_status_of

DEFAULT_CONFIG: Final = "/etc/reader/reader.yaml"
PAGE_DIGITS: Final = 5  # page_count <= 5,000 (artifact caps)
UPLOAD_ID: Final = re.compile(r"up-[0-9a-f]{12}")  # FR-INGEST-23
# Written at image build from FR-INGEST-3's closure function (OPS-DEPLOY FR-DEPLOY-13), so the api never
# imports reader.ingest.host to compare the watcher's code with its own.
SOURCE_SHA_FILE: Final = Path("/usr/local/share/reader/converter_source_sha256")


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


async def _material_fields(conn: AsyncConnection, owner: Any, sha: str) -> dict[str, Any]:
    row = await _imported(conn, owner, sha)
    version = row[1] if row else ingest.CONVERTER_VERSION
    state = await _job_status(conn, key_for(sha, version))
    return {
        "sha256": sha,
        "status": store.api_status(state, row is not None),
        "converter_version": row[1] if row else None,
        "page_count": row[2] if row else None,
        "span_count": row[3] if row else None,
        "outline_count": row[4] if row else None,
        "pages_without_text": list(row[5]) if row else None,
    }


def _title(filename: str) -> str:
    return filename[:-4] if filename.lower().endswith(".pdf") else filename


async def list_materials(ctx: Ctx) -> Response:
    if (refused := _owner_only(ctx)) is not None:
        return refused
    owner = ctx.signed_in.id
    listed: list[dict[str, Any]] = []
    declared = _materials(ctx)
    rows = await uploads.listed(ctx.engine, owner)
    async with ctx.engine.connect() as conn:
        for material in declared:
            fields = await _material_fields(conn, owner, material.sha256)
            listed.append(
                {"id": material.id, "language": material.language, "license": material.license, **fields}
                | {"source": "config"}
            )
        for row in rows:
            fields = await _material_fields(conn, owner, row.sha256)
            listed.append(
                {
                    "id": uploads.material_id(row.sha256),
                    "language": row.language,
                    "license": "personal-use",
                    **fields,
                    "source": "upload",
                    "title": _title(row.filename),
                    "upload": await uploads.view_of(conn, row),
                }
            )
        _, _, retained = await uploads.open_usage(conn)
    return JSONResponse({"materials": listed, "inbox_retained_bytes": retained})


async def _material_sha(ctx: Ctx, conn: AsyncConnection, material_id: str) -> str | None:
    if material_id.startswith("up-"):
        if not UPLOAD_ID.fullmatch(material_id):  # before any LIKE: no wildcard ever reaches the pattern
            return None
        found = (
            await conn.execute(
                text("SELECT sha256 FROM uploads WHERE owner_id = :o AND sha256 LIKE :p"),
                {"o": ctx.signed_in.id, "p": material_id[3:] + "%"},
            )
        ).all()
        return found[0][0] if len(found) == 1 else None
    material = next((m for m in _materials(ctx) if m.id == material_id), None)
    return None if material is None else material.sha256


async def _material_ingest(ctx: Ctx, conn: AsyncConnection) -> tuple[str, Any] | None:
    sha = await _material_sha(ctx, conn, ctx.request.path_params["material_id"])
    if sha is None:
        return None
    row = await _imported(conn, ctx.signed_in.id, sha)
    return None if row is None else (sha, row)


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
        sha, row = found
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
                        "span_id": span_id(sha, s[0]),
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


# ---- uploads (FR-INGEST-18, -19, -23) ----


async def post_upload(ctx: Ctx) -> Response:
    return await upload.handle(ctx, lambda: _materials(ctx))


def _upload_id(ctx: Ctx) -> uuid.UUID | None:
    try:
        return uuid.UUID(ctx.request.path_params["upload_id"])
    except ValueError:
        return None


async def get_upload(ctx: Ctx) -> Response:
    if (refused := _owner_only(ctx)) is not None:
        return refused
    upload_id = _upload_id(ctx)
    found = None if upload_id is None else await uploads.view(ctx.engine, ctx.signed_in.id, upload_id)
    return error(404, "not_found") if found is None else JSONResponse(found)


async def _delete(ctx: Ctx, allowed: tuple[str, ...], done: str) -> Response:
    if (refused := _owner_only(ctx)) is not None:
        return refused
    upload_id = _upload_id(ctx)
    if upload_id is None:
        return error(404, "not_found")
    try:
        inbox = upload.inbox_dir(ctx)
        await uploads.cancel_or_discard(ctx.engine, ctx.signed_in.id, upload_id, allowed, inbox)
    except uploads.Busy:
        return error(409, "upload_busy")
    except uploads.StateRefused:
        return error(409, "state")
    except LookupError:
        return error(404, "not_found")
    return JSONResponse({done: True})


async def cancel_upload(ctx: Ctx) -> Response:
    return await _delete(ctx, ("queued", "claimed", "converting"), "cancelled")


async def discard_upload(ctx: Ctx) -> Response:
    return await _delete(ctx, ("failed",), "discarded")


REQUEUE_REPLIES: Final = {
    "queued": (202, {"retry": "requeued"}),
    "already_queued": (202, {"retry": "already_queued"}),
    "running": (409, {"error": "busy"}),
    "unknown_key": (409, {"error": "state"}),
}


async def retry_upload(ctx: Ctx) -> Response:
    if (refused := _owner_only(ctx)) is not None:
        return refused
    upload_id = _upload_id(ctx)
    owner = ctx.signed_in.id
    current = None if upload_id is None else await uploads.view(ctx.engine, owner, upload_id)
    if current is None or upload_id is None:
        return error(404, "not_found")
    if current["retry"] == "reconvert":
        if not await uploads.reconvert(ctx.engine, owner, upload_id):
            return error(409, "state")
    elif current["retry"] == "requeue":
        async with ctx.engine.connect() as conn:
            row = await uploads.row_by_id(conn, upload_id)
        target = None if row is None else await uploads.dead_key(ctx.engine, row.sha256)
        jobs = getattr(ctx.request.app.state, "jobs", None)
        if target is None:
            return error(409, "state")
        if jobs is None:
            return error(503, "unavailable")
        result = await jobs.requeue_dead(*target)
        if result.kind in REQUEUE_REPLIES:
            status, body = REQUEUE_REPLIES[result.kind]
            return JSONResponse(body, status_code=status)
        if result.kind != "already_done":
            return error(409, "state")
    else:
        return error(409, "state")
    found = await uploads.view(ctx.engine, owner, upload_id)
    return error(404, "not_found") if found is None else JSONResponse(found)


def _image_sha(ctx: Ctx) -> str | None:
    injected = getattr(ctx.request.app.state, "converter_source_sha256", None)
    if injected is not None:
        return str(injected)
    try:
        return SOURCE_SHA_FILE.read_text(encoding="ascii").strip() or None
    except OSError:
        return None


async def get_converter(ctx: Ctx) -> Response:
    if (refused := _owner_only(ctx)) is not None:
        return refused
    await uploads.expire(ctx.engine)
    async with ctx.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT seen_at, state, strays, converter_version, watcher_sha, busy,"
                    " seen_at <= now() - make_interval(secs => :s) FROM ingest_hosts"
                ),
                {"s": params.ONLINE_S},
            )
        ).first()
    if row is None:
        never = {"seen_at": None, "converter_version": None, "strays": 0, "busy": "none", "stale": False}
        return JSONResponse({"state": "offline", **never})
    seen_at, state, strays, version, watcher_sha, busy, old = row
    image = _image_sha(ctx)
    return JSONResponse(
        {
            "state": "offline" if old else ("online" if state == "ready" else state),
            "seen_at": seen_at.isoformat(),
            "converter_version": version,
            "strays": strays,
            "busy": busy,
            "stale": image is not None and watcher_sha != image,
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
    Route("GET", "/api/materials/converter", "session", get_converter),
    Route("POST", "/api/materials/uploads", "session", _config_guard(post_upload)),
    Route("GET", "/api/materials/uploads/{upload_id}", "session", get_upload),
    Route("POST", "/api/materials/uploads/{upload_id}/cancel", "session", cancel_upload),
    Route("POST", "/api/materials/uploads/{upload_id}/retry", "session", retry_upload),
    Route("DELETE", "/api/materials/uploads/{upload_id}", "session", discard_upload),
    Route("GET", "/api/materials/{material_id}/outline", "session", _config_guard(get_outline)),
    Route("GET", "/api/materials/{material_id}/spans", "session", _config_guard(get_spans)),
)
