"""Lesson read routes (SRV-LESSON FR-LESSON-7..9, FR-LESSON-12), declared as data for SRV-AUTH's ROUTERS,
plus the management routes of `reader.lessons.manage_routes` (FR-LESSON-21..31)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import anyio.to_thread
from sqlalchemy import text
from starlette.responses import JSONResponse, Response

from reader import blobs
from reader.api.http import Ctx, Route, error
from reader.lessons import manage
from reader.lessons import manage_routes as m
from reader.lessons.access import lesson_uuid

REVISIONS_LISTED: Final = 100
log = logging.getLogger("reader.lessons")

# latest_sha256 is the current revision: the sha256 of the most recent stage record (FR-LESSON-7). Deleted
# lessons and lessons of deleted courses are not found (INV-LESSON-22); rows carry the amendment's fields.
_LESSON_COLUMNS: Final = manage.LESSON_ROW
_lesson = manage.lesson_json


def _not_found() -> JSONResponse:
    return error(404, "not_found")


def _iso(value: datetime) -> str:
    return value.isoformat()


async def list_lessons(ctx: Ctx) -> Response:
    """FR-LESSON-7; archived courses' lessons only with `?archived=1` (FR-LESSON-24)."""
    archived = "" if ctx.request.query_params.get("archived") == "1" else " AND c.archived_at IS NULL"
    async with ctx.engine.connect() as conn:
        rows = await conn.execute(
            text(_LESSON_COLUMNS + archived + " ORDER BY c.created_at DESC, c.id, l.ordinal"),
            {"u": ctx.signed_in.id},
        )
        return JSONResponse({"lessons": [_lesson(row) for row in rows]})


async def _own_lesson(ctx: Ctx, conn: Any, lesson: uuid.UUID) -> Any:
    rows = await conn.execute(text(_LESSON_COLUMNS + " AND l.id = :l"), {"u": ctx.signed_in.id, "l": lesson})
    return rows.first()


async def get_lesson(ctx: Ctx) -> Response:
    lesson = lesson_uuid(ctx.request.path_params["lesson_id"])
    if lesson is None:
        return _not_found()
    async with ctx.engine.connect() as conn:
        row = await _own_lesson(ctx, conn, lesson)
        if row is None:
            return _not_found()
        revisions = await conn.execute(
            text(
                "SELECT r.sha256, r.byte_size, r.cell_count, r.created_at,"
                " ARRAY(SELECT s.stage FROM lesson_revision_stages s"
                " WHERE s.lesson_id = r.lesson_id AND s.sha256 = r.sha256 ORDER BY s.id)"
                " FROM lesson_revisions r WHERE r.lesson_id = :l ORDER BY r.seq DESC LIMIT :n"
            ),
            {"l": lesson, "n": REVISIONS_LISTED},
        )
        return JSONResponse(
            {
                "lesson": _lesson(row),
                "revisions": [
                    {
                        "sha256": r[0],
                        "byte_size": r[1],
                        "cell_count": r[2],
                        "created_at": _iso(r[3]),
                        "stages": list(r[4]),
                    }
                    for r in revisions
                ],
            }
        )


def _matches(if_none_match: str, etag: str) -> bool:
    """RFC 9110 §13.1.2 weak comparison: `*`, or any listed entity tag equal to ours with or without `W/`."""
    for raw in if_none_match.split(","):
        tag = raw.strip()
        if tag == "*":
            return True
        if tag.startswith("W/"):
            tag = tag[2:]
        if tag == etag:
            return True
    return False


async def get_revision(ctx: Ctx) -> Response:
    lesson = lesson_uuid(ctx.request.path_params["lesson_id"])
    sha256 = ctx.request.path_params["sha256"]
    if lesson is None or not blobs.is_sha256(sha256):
        return _not_found()
    async with ctx.engine.connect() as conn:
        if await _own_lesson(ctx, conn, lesson) is None:
            return _not_found()
        known = await conn.execute(
            text("SELECT 1 FROM lesson_revisions WHERE lesson_id = :l AND sha256 = :s"),
            {"l": lesson, "s": sha256},
        )
        if known.scalar() is None:
            return error(404, "unknown_revision")
    etag = f'"{sha256}"'
    headers = {
        "ETag": etag,
        "Cache-Control": "private, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
    }
    if _matches(ctx.request.headers.get("if-none-match", ""), etag):
        return Response(status_code=304, headers=headers)
    root: Path = getattr(ctx.request.app.state, "blobs_root", blobs.DEFAULT_ROOT)
    try:
        data = await anyio.to_thread.run_sync(blobs.get, root, sha256)
    except (blobs.BlobMissing, blobs.BlobCorrupt) as failure:
        log.warning("blob_unavailable lesson=%s sha256=%s %s", lesson, sha256, type(failure).__name__)
        return error(500, "blob_unavailable")
    return Response(data, media_type="application/x-ipynb+json", headers=headers)


ROUTES: tuple[Route, ...] = (
    Route("GET", "/api/lessons", "session", list_lessons),
    Route("GET", "/api/lessons/{lesson_id}", "session", get_lesson),
    Route("GET", "/api/lessons/{lesson_id}/revisions/{sha256}", "session", get_revision),
    # FR-LESSON-21..31 (amendment V/manage); `/api/courses/order` matches before `/api/courses/{course_id}`.
    Route("GET", "/api/courses", "session", m.list_courses),
    Route("POST", "/api/courses/order", "session", m.order_courses),
    Route("POST", "/api/courses/{course_id}", "session", m.edit_course),
    Route("POST", "/api/courses/{course_id}/archive", "session", m.archive_course),
    Route("POST", "/api/courses/{course_id}/unarchive", "session", m.unarchive_course),
    Route("GET", "/api/courses/{course_id}/impact", "session", m.course_impact),
    Route("DELETE", "/api/courses/{course_id}", "session", m.delete_course),
    Route("POST", "/api/courses/{course_id}/restore", "session", m.restore_course),
    Route("GET", "/api/courses/{course_id}/lessons", "session", m.course_lessons),
    Route("POST", "/api/courses/{course_id}/lessons/order", "session", m.order_lessons),
    Route("POST", "/api/lessons/{lesson_id}", "session", m.rename_lesson),
    Route("POST", "/api/lessons/{lesson_id}/skip", "session", m.skip_lesson),
    Route("POST", "/api/lessons/{lesson_id}/unskip", "session", m.unskip_lesson),
    Route("DELETE", "/api/lessons/{lesson_id}", "session", m.delete_lesson),
    Route("POST", "/api/lessons/{lesson_id}/restore", "session", m.restore_lesson),
    Route("POST", "/api/lessons/{lesson_id}/progress", "session", m.set_progress),
    Route("POST", "/api/lessons/{lesson_id}/revert", "session", m.revert_lesson),
    Route("GET", "/api/lessons/{lesson_id}/notes", "session", m.list_notes),
    Route("POST", "/api/lessons/{lesson_id}/notes", "session", m.add_note),
    Route("POST", "/api/lessons/{lesson_id}/notes/{note_id}", "session", m.edit_note),
    Route("DELETE", "/api/lessons/{lesson_id}/notes/{note_id}", "session", m.delete_note),
    Route("POST", "/api/lessons/{lesson_id}/bookmarks/{cell_id}", "session", m.add_bookmark),
    Route("DELETE", "/api/lessons/{lesson_id}/bookmarks/{cell_id}", "session", m.remove_bookmark),
    Route("GET", "/api/bookmarks", "session", m.list_bookmarks),
)
