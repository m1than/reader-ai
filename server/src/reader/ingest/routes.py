"""Material routes (SRV-INGEST FR-INGEST-15, -18, -19, -23, -30..32), declared as data for SRV-AUTH's ROUTERS.

Every handler requires the owner role: sharing is deferred and materials may be `personal-use` (ADR-0008).
The listed materials are reader.yaml's (`app.state.ingest_materials` in tests, else READER_CONFIG), then the
owner's uploads, newest first; a deleted material is listed only under `?state=deleted`. Every read of an
upload applies lease expiry first (FR-INGEST-19). Rename, delete and restore write their audit row in the
transaction of the change (SRV-EVENTS FR-EVENTS-30), never for a no-op.
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
from reader.ingest import manage, manifest, params, store, upload, uploads
from reader.ingest.artifact import span_id
from reader.ingest.jobs import KIND, key_for
from reader.jobs import audit
from reader.jobs.queue import job_status_of

DEFAULT_CONFIG: Final = "/etc/reader/reader.yaml"
PAGE_DIGITS: Final = 5  # page_count <= 5,000 (artifact caps)
UPLOAD_ID: Final = re.compile(r"up-[0-9a-f]{12}")  # FR-INGEST-23
LIST_STATES: Final = ("active", "deleted")  # GET /api/materials?state= (FR-INGEST-15 amendment)
# Written at image build from FR-INGEST-3's closure function (OPS-DEPLOY FR-DEPLOY-14), so the api never
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
                " i.pages_without_text, m.deleted_at FROM ingests i JOIN materials m ON m.id = i.material_id"
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


async def _manage_fields(
    conn: AsyncConnection, owner: Any, material_id: str, sha: str, filename: str | None
) -> dict[str, Any]:
    """FR-INGEST-30/31: display title, version, tombstone and the courses that use the material."""
    row = await manage.row_of(conn, owner, sha)
    title = row.title if row is not None and row.title is not None else None
    deleted_at = None if row is None or row.deleted_at is None else row.deleted_at.isoformat()
    return {
        "title": title or manage.default_title(material_id, filename),
        "version": None if row is None else row.version,
        "deleted_at": deleted_at,
        "courses": await manage.courses_using(conn, owner, sha),
    }


async def _config_entry(conn: AsyncConnection, owner: Any, material: Material) -> dict[str, Any]:
    fields = await _material_fields(conn, owner, material.sha256)
    return {
        "id": material.id,
        "language": material.language,
        "license": material.license,
        **fields,
        "source": "config",
        **await _manage_fields(conn, owner, material.id, material.sha256, None),
    }


async def _upload_entry(conn: AsyncConnection, owner: Any, row: uploads.Row) -> dict[str, Any]:
    material_id = uploads.material_id(row.sha256)
    return {
        "id": material_id,
        "language": row.language,
        "license": "personal-use",
        **await _material_fields(conn, owner, row.sha256),
        "source": "upload",
        **await _manage_fields(conn, owner, material_id, row.sha256, row.filename),
        "upload": await uploads.view_of(conn, row),
    }


async def _entry(ctx: Ctx, conn: AsyncConnection, material_id: str, sha: str) -> dict[str, Any]:
    """One material as GET /api/materials lists it (the rename and restore responses)."""
    if material_id.startswith("up-"):
        row = await uploads.row_by_sha(conn, sha)
        if row is not None:
            return await _upload_entry(conn, ctx.signed_in.id, row)
    material = next(m for m in _materials(ctx) if m.id == material_id)
    return await _config_entry(conn, ctx.signed_in.id, material)


async def list_materials(ctx: Ctx) -> Response:
    if (refused := _owner_only(ctx)) is not None:
        return refused
    state = ctx.request.query_params.get("state", "active")
    if state not in LIST_STATES:
        return error(400, "state")
    owner = ctx.signed_in.id
    listed: list[dict[str, Any]] = []
    declared = _materials(ctx)
    rows = await uploads.listed(ctx.engine, owner)
    async with ctx.engine.connect() as conn:
        for material in declared:
            listed.append(await _config_entry(conn, owner, material))
        for row in rows:
            listed.append(await _upload_entry(conn, owner, row))
        _, _, retained = await uploads.open_usage(conn)
    shown = [m for m in listed if (m["deleted_at"] is not None) == (state == "deleted")]
    return JSONResponse({"materials": shown, "inbox_retained_bytes": retained})


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
    return None if row is None or row[6] is not None else (sha, row)  # a deleted material: 404


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


# ---- rename, delete and restore (FR-INGEST-30..32) ----


def _managed_route(handler: Handler) -> Handler:
    """Owner only (FR-INGEST-32), then the handler; a refusal raised inside its transaction rolls it back."""

    async def guarded(ctx: Ctx) -> Response:
        if (refused := _owner_only(ctx)) is not None:
            return refused
        try:
            return await handler(ctx)
        except manage.Refused as refusal:
            return JSONResponse(refusal.body, status_code=refusal.status)
        except _NoConfig, ConfigError:
            return error(503, "config_unavailable")
        except audit.AuditRefused as refusal:  # a programming error (SRV-EVENTS FR-EVENTS-30); rolled back
            audit.report_refused(refusal)
            return error(500, "audit_refused")

    return guarded


async def _managed(ctx: Ctx, conn: AsyncConnection, *, lock: bool) -> tuple[str, str, manage.Material]:
    """The path's material: imported (a `materials` row of the caller) or 404."""
    material_id = ctx.request.path_params["material_id"]
    sha = await _material_sha(ctx, conn, material_id)
    row = None if sha is None else await manage.row_of(conn, ctx.signed_in.id, sha, lock=lock)
    if sha is None or row is None:
        raise manage.refused(404, "not_found")
    return material_id, sha, row


def _version(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


async def rename_material(ctx: Ctx) -> Response:
    body = await ctx.json()
    if not isinstance(body, dict) or set(body) != {"version", "title"} or not _version(body["version"]):
        return error(400, "bad_request")
    owner = ctx.signed_in.id
    async with ctx.engine.begin() as conn:
        material_id, sha, row = await _managed(ctx, conn, lock=True)
        if row.deleted_at is not None:
            raise manage.refused(404, "not_found")
        title = manage.clean_title(body["title"])
        if title == row.title:  # the no-op check comes before the version check (INV-LESSON-21)
            return JSONResponse(await _entry(ctx, conn, material_id, sha))
        if body["version"] != row.version:
            current = await _entry(ctx, conn, material_id, sha)
            raise manage.Refused(409, {"error": "stale", "material": current})
        await conn.execute(
            text("UPDATE materials SET title = :t, version = version + 1 WHERE id = :i"),
            {"t": title, "i": row.id},
        )
        await audit.record(conn, owner, "material.rename", material_id, {"version": row.version + 1})
        return JSONResponse(await _entry(ctx, conn, material_id, sha))


async def get_impact(ctx: Ctx) -> Response:
    async with ctx.engine.connect() as conn:
        _, sha, row = await _managed(ctx, conn, lock=False)
        if row.deleted_at is not None:
            raise manage.refused(404, "not_found")
        return JSONResponse(await manage.impact(conn, ctx.signed_in.id, sha))


async def _deleted_body(
    conn: AsyncConnection, owner: Any, material_id: str, sha: str, deleted_at: Any
) -> dict[str, Any]:
    """The delete's response, the same for a repeated DELETE: the courses its cascade deleted."""
    courses = await manage.courses_cascaded(conn, owner, material_id)
    return {
        "courses": courses,
        "lessons": await manage.lesson_count(conn, owner, courses),
        "uploads_open": await manage.uploads_open(conn, owner, sha),
        "deleted_at": deleted_at.isoformat(),
    }


async def delete_material(ctx: Ctx) -> Response:
    body = await ctx.json()
    if (
        not isinstance(body, dict)
        or not set(body) <= {"confirm", "cascade"}
        or not isinstance(body.get("cascade", False), bool)
    ):
        return error(400, "bad_request")
    owner = ctx.signed_in.id
    material_id = ctx.request.path_params["material_id"]
    async with ctx.engine.begin() as conn:
        sha = await _material_sha(ctx, conn, material_id)
        if sha is None:
            raise manage.refused(404, "not_found")
        await manage.lock_affected(conn, owner, sha, material_id)  # before any check (INV-LESSON-23)
        _, _, row = await _managed(ctx, conn, lock=True)
        if row.deleted_at is not None:
            return JSONResponse(await _deleted_body(conn, owner, material_id, sha, row.deleted_at))
        display = await manage.display_title(conn, owner, material_id, row)
        if not manage.confirm_matches(body.get("confirm"), display):
            raise manage.refused(400, "confirm_mismatch")
        if await manage.busy(conn, owner, row):
            raise manage.refused(409, "material_busy")
        courses = await manage.courses_using(conn, owner, sha)
        if courses and body.get("cascade") is not True:
            raise manage.Refused(409, {"error": "material_in_use", "courses": courses})
        if courses:
            await manage.delete_courses(conn, owner, courses, material_id)
        sql = "UPDATE materials SET deleted_at = now() WHERE id = :i RETURNING deleted_at"
        deleted_at = (await conn.execute(text(sql), {"i": row.id})).scalar_one()
        await manifest.remove_material(conn, owner, sha)  # FR-INGEST-39: one entry when the head names it
        await audit.record(conn, owner, "material.delete", material_id, {"courses": len(courses)})
        return JSONResponse(await _deleted_body(conn, owner, material_id, sha, deleted_at))


async def restore_material(ctx: Ctx) -> Response:
    body = await ctx.json()
    if body not in (None, {}):
        return error(400, "bad_request")
    owner = ctx.signed_in.id
    material_id = ctx.request.path_params["material_id"]
    async with ctx.engine.begin() as conn:
        sha = await _material_sha(ctx, conn, material_id)
        if sha is None:
            raise manage.refused(404, "not_found")
        await manage.lock_affected(conn, owner, sha, material_id)
        _, _, row = await _managed(ctx, conn, lock=True)
        restored: list[str] = []
        if row.deleted_at is not None:
            await conn.execute(text("UPDATE materials SET deleted_at = NULL WHERE id = :i"), {"i": row.id})
            restored = await manage.restore_courses(conn, owner, material_id)  # after the material is live
            await manifest.restore_material(conn, owner, sha)  # FR-INGEST-39: after FR-INGEST-31's locks
            await audit.record(conn, owner, "material.restore", material_id, {"courses": len(restored)})
        return JSONResponse(
            {"material": await _entry(ctx, conn, material_id, sha), "restored_courses": restored}
        )


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
    Route("POST", "/api/materials/{material_id}", "session", _managed_route(rename_material)),
    Route("GET", "/api/materials/{material_id}/impact", "session", _managed_route(get_impact)),
    Route("DELETE", "/api/materials/{material_id}", "session", _managed_route(delete_material)),
    Route("POST", "/api/materials/{material_id}/restore", "session", _managed_route(restore_material)),
)
