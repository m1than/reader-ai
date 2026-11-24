"""The management route handlers (SRV-LESSON FR-LESSON-21..31); `reader.lessons.routes` declares the routes.

Every route is `auth: session`; FR-AUTH-12's Origin and content-type checks run before the handler. An id the
caller does not own (or cannot open) answers 404 `not_found` like a missing one (INV-LESSON-20). Each handler
runs its `reader.lessons.manage` function in one transaction; a `ManageError` rolls it back and answers its
code.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.responses import JSONResponse, Response

from reader import blobs
from reader.api.http import Ctx, error
from reader.lessons import manage
from reader.lessons.access import lesson_uuid

Action = Callable[[AsyncConnection], Awaitable[dict[str, Any]]]


def _blobs_root(ctx: Ctx) -> Path:
    root: Path = getattr(ctx.request.app.state, "blobs_root", blobs.DEFAULT_ROOT)
    return root


async def _run(action: Action, ctx: Ctx, status: int = 200) -> Response:
    try:
        async with ctx.engine.begin() as conn:
            result = await action(conn)
    except manage.ManageError as e:
        return JSONResponse({"error": e.code, **e.extra}, status_code=e.status)
    return JSONResponse(result, status_code=status)


async def _body(ctx: Ctx) -> dict[str, Any] | None:
    body = await ctx.json()
    return body if isinstance(body, dict) else None


def _path_id(ctx: Ctx, name: str) -> uuid.UUID | None:
    return lesson_uuid(ctx.request.path_params[name])


def course_route(
    fn: Callable[[AsyncConnection, uuid.UUID, uuid.UUID, dict[str, Any]], Awaitable[dict[str, Any]]],
) -> Callable[[Ctx], Awaitable[Response]]:
    """A course mutation with a JSON object body: `fn(conn, owner, course_id, body)`."""

    async def handler(ctx: Ctx) -> Response:
        course = _path_id(ctx, "course_id")
        if course is None:
            return error(404, "not_found")
        body = await _body(ctx)
        if body is None:
            return error(400, "invalid_body")
        return await _run(lambda conn: fn(conn, ctx.signed_in.id, course, body), ctx)

    return handler


def lesson_route(
    fn: Callable[[AsyncConnection, Ctx, uuid.UUID, dict[str, Any]], Awaitable[dict[str, Any]]],
    *,
    needs_body: bool = True,
    status: int = 200,
) -> Callable[[Ctx], Awaitable[Response]]:
    """A lesson route: `fn(conn, ctx, lesson_id, body)`; GET and bodiless DELETE routes get an empty body."""

    async def handler(ctx: Ctx) -> Response:
        lesson = _path_id(ctx, "lesson_id")
        if lesson is None:
            return error(404, "not_found")
        body: dict[str, Any] | None = {}
        if needs_body:
            body = await _body(ctx)
            if body is None:
                return error(400, "invalid_body")
        return await _run(lambda conn: fn(conn, ctx, lesson, body or {}), ctx, status)

    return handler


async def list_courses(ctx: Ctx) -> Response:
    state = ctx.request.query_params.get("state", "active")
    if state not in manage.STATES:
        return error(400, "invalid_body")

    async def action(conn: AsyncConnection) -> dict[str, Any]:
        return {"courses": await manage.list_courses(conn, ctx.signed_in.id, state)}

    return await _run(action, ctx)


async def order_courses(ctx: Ctx) -> Response:
    body = await _body(ctx)
    if body is None:
        return error(400, "invalid_body")
    return await _run(lambda conn: manage.order_courses(conn, ctx.signed_in.id, body), ctx)


async def course_impact(ctx: Ctx) -> Response:
    course = _path_id(ctx, "course_id")
    if course is None:
        return error(404, "not_found")

    async def action(conn: AsyncConnection) -> dict[str, Any]:
        return dict(await manage.course_impact(conn, ctx.signed_in.id, course))

    return await _run(action, ctx)


async def course_lessons(ctx: Ctx) -> Response:
    course = _path_id(ctx, "course_id")
    if course is None:
        return error(404, "not_found")
    if ctx.request.query_params.get("state") != "deleted":
        return error(400, "invalid_body")
    return await _run(lambda conn: manage.deleted_lessons(conn, ctx.signed_in.id, course), ctx)


async def list_bookmarks(ctx: Ctx) -> Response:
    return await _run(lambda conn: manage.list_bookmarks(conn, _blobs_root(ctx), ctx.signed_in.id), ctx)


Body = dict[str, Any]
Conn = AsyncConnection


def _flag(action: str) -> Callable[[Conn, Ctx, uuid.UUID, Body], Awaitable[Body]]:
    async def fn(conn: Conn, ctx: Ctx, lesson: uuid.UUID, _: Body) -> Body:
        return await manage.set_lesson_flag(conn, ctx.signed_in.id, lesson, action)

    return fn


async def _rename(conn: Conn, ctx: Ctx, lesson: uuid.UUID, body: Body) -> Body:
    return await manage.rename_lesson(conn, ctx.signed_in.id, lesson, body)


async def _progress(conn: Conn, ctx: Ctx, lesson: uuid.UUID, body: Body) -> Body:
    return await manage.set_progress(conn, ctx.signed_in.id, lesson, body)


async def _revert(conn: Conn, ctx: Ctx, lesson: uuid.UUID, body: Body) -> Body:
    return await manage.revert_lesson(conn, _blobs_root(ctx), ctx.signed_in.id, lesson, body)


async def _notes(conn: Conn, ctx: Ctx, lesson: uuid.UUID, _: Body) -> Body:
    return await manage.list_notes(conn, ctx.signed_in.id, lesson)


async def _add_note(conn: Conn, ctx: Ctx, lesson: uuid.UUID, body: Body) -> Body:
    return await manage.add_note(conn, _blobs_root(ctx), ctx.signed_in.id, lesson, body)


def _note_id(ctx: Ctx) -> uuid.UUID:
    note = lesson_uuid(ctx.request.path_params["note_id"])
    if note is None:
        raise manage.ManageError("not_found")
    return note


async def _edit_note(conn: Conn, ctx: Ctx, lesson: uuid.UUID, body: Body) -> Body:
    return await manage.edit_note(conn, ctx.signed_in.id, lesson, _note_id(ctx), body)


async def _delete_note(conn: Conn, ctx: Ctx, lesson: uuid.UUID, _: Body) -> Body:
    return await manage.delete_note(conn, ctx.signed_in.id, lesson, _note_id(ctx))


async def _add_bookmark(conn: Conn, ctx: Ctx, lesson: uuid.UUID, _: Body) -> Body:
    cell = ctx.request.path_params["cell_id"]
    return await manage.add_bookmark(conn, _blobs_root(ctx), ctx.signed_in.id, lesson, cell)


async def _remove_bookmark(conn: Conn, ctx: Ctx, lesson: uuid.UUID, _: Body) -> Body:
    return await manage.remove_bookmark(conn, ctx.signed_in.id, lesson, ctx.request.path_params["cell_id"])


async def _archive(conn: Conn, owner: uuid.UUID, course: uuid.UUID, _: Body) -> Body:
    return await manage.archive_course(conn, owner, course)


# The handlers by route; `reader.lessons.routes` declares the routes (the module SRV-AUTH's ROUTERS names).
edit_course = course_route(manage.edit_course)
archive_course = course_route(_archive)
unarchive_course = course_route(manage.unarchive_course)
delete_course = course_route(manage.delete_course)
restore_course = course_route(manage.restore_course)
order_lessons = course_route(manage.order_lessons)
rename_lesson = lesson_route(_rename)
skip_lesson = lesson_route(_flag("skip"))
unskip_lesson = lesson_route(_flag("unskip"))
delete_lesson = lesson_route(_flag("delete"))
restore_lesson = lesson_route(_flag("restore"))
set_progress = lesson_route(_progress)
revert_lesson = lesson_route(_revert)
list_notes = lesson_route(_notes, needs_body=False)
add_note = lesson_route(_add_note, status=201)
edit_note = lesson_route(_edit_note)
delete_note = lesson_route(_delete_note, needs_body=False)
add_bookmark = lesson_route(_add_bookmark)
remove_bookmark = lesson_route(_remove_bookmark, needs_body=False)
