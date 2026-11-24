"""Compile routes, declared as data for SRV-AUTH's ROUTERS (SRV-PIPELINE FR-PIPELINE-1, FR-PIPELINE-11).

Owner role only; anyone else gets 404. The routes enqueue through `app.state.jobs` (SRV-AUTH paired change).
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from sqlalchemy import text
from starlette.responses import JSONResponse, Response

from reader.api.http import Ctx, Route, error
from reader.jobs import idempotency_key
from reader.pipeline import courses, runs
from reader.pipeline.errors import CourseDeleted, PipelineError

RUN_CAP_USD: Final = Decimal("8.00")  # bounds.run_cap_usd
STATUS: Final = {
    "no_material": 409,
    "compile_running": 409,
    "lesson_unknown": 404,
    "not_found": 404,
    "plan_invalid": 400,
    "no_source_spans": 409,
    "source_too_large": 409,
    "origin_conflict": 409,  # FR-PIPELINE-14
    "not_running": 409,  # FR-PIPELINE-20
    "lesson_inactive": 409,  # FR-PIPELINE-22
    "course_deleted": 409,
}

log = logging.getLogger("reader.pipeline")


def _owner(ctx: Ctx) -> bool:
    return ctx.signed_in.role == "owner"


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _failure(e: PipelineError) -> JSONResponse:
    if isinstance(e, CourseDeleted):  # FR-PIPELINE-22: the answer names the course to restore
        return JSONResponse({"error": e.code, "course_id": str(e.course_id)}, status_code=409)
    return error(STATUS.get(e.code, 400), e.code)


def _jobs(ctx: Ctx) -> Any:
    return ctx.request.app.state.jobs


def _cap(body: Any) -> Decimal | None:
    raw = body.get("cap_usd", RUN_CAP_USD) if isinstance(body, dict) else None
    if isinstance(raw, bool) or not isinstance(raw, (int, float, Decimal)):
        return None
    try:
        cap = Decimal(str(raw))
    except InvalidOperation:
        return None
    if not cap.is_finite() or not 0 < cap <= RUN_CAP_USD or cap != cap.quantize(Decimal("0.01")):
        return None
    return cap


async def import_course(ctx: Ctx) -> Response:
    if not _owner(ctx):
        return error(404, "not_found")
    body = await ctx.json()
    try:
        async with ctx.engine.begin() as conn:
            result = await courses.import_plan(conn, ctx.signed_in.id, body)
    except PipelineError as e:
        return _failure(e)
    return JSONResponse(result)


async def compile_lesson(ctx: Ctx) -> Response:
    lesson = _uuid(ctx.request.path_params["lesson_id"])
    if not _owner(ctx) or lesson is None:
        return error(404, "not_found")
    raw = await ctx.request.body()
    cap = _cap({} if not raw.strip() else await ctx.json())  # no body at all: the default cap
    if cap is None:
        return error(400, "cap_invalid")
    try:
        run = await runs.create_run(ctx.engine, _jobs(ctx), ctx.signed_in.id, lesson, cap)
    except PipelineError as e:
        return _failure(e)
    return JSONResponse({"run_id": str(run)}, status_code=202)


async def get_run(ctx: Ctx) -> Response:
    run = _uuid(ctx.request.path_params["run_id"])
    if not _owner(ctx) or run is None:
        return error(404, "not_found")
    try:
        return JSONResponse(await runs.status(ctx.engine, _jobs(ctx), ctx.signed_in.id, run))
    except PipelineError as e:
        return _failure(e)


async def latest_run(ctx: Ctx) -> Response:
    lesson = _uuid(ctx.request.path_params["lesson_id"])
    if not _owner(ctx) or lesson is None:
        return error(404, "not_found")
    async with ctx.engine.connect() as conn:
        run = (
            await conn.execute(
                text(
                    "SELECT id FROM compile_runs WHERE lesson_id = :l AND owner_id = :o"
                    " ORDER BY created_at DESC, id LIMIT 1"
                ),
                {"l": lesson, "o": ctx.signed_in.id},
            )
        ).scalar()
    if run is None:
        return error(404, "not_found")
    try:
        return JSONResponse(await runs.status(ctx.engine, _jobs(ctx), ctx.signed_in.id, run))
    except PipelineError as e:  # a deleted lesson's runs are not found (SRV-LESSON INV-LESSON-22)
        return _failure(e)


async def resume(ctx: Ctx) -> Response:
    run = _uuid(ctx.request.path_params["run_id"])
    if not _owner(ctx) or run is None:
        return error(404, "not_found")
    jobs = _jobs(ctx)
    try:
        async with ctx.engine.connect() as conn:  # FR-PIPELINE-22: an inactive lesson is refused first
            row = await runs.load_run(conn, run, ctx.signed_in.id)
            if row is None:
                raise PipelineError("not_found")
            await runs.lesson_gate(conn, ctx.signed_in.id, row.lesson_id, active=True)
        state = await runs.derive(ctx.engine, jobs, ctx.signed_in.id, run)
    except PipelineError as e:
        return _failure(e)
    pending = state["_pending"]
    if state["state"] != "stage_failed" or pending is None or pending["args"] is None:
        return error(409, "not_failed")
    kind = runs.kind_for(pending["stage"])
    await runs.record_stage_error(ctx.engine, run, None)  # FR-PIPELINE-16: committed before the requeue
    await jobs.requeue_dead(kind, idempotency_key(jobs.catalog[kind], pending["args"]))
    result = await runs.status(ctx.engine, jobs, ctx.signed_in.id, run)
    return JSONResponse(result, status_code=202)


async def open_runs(ctx: Ctx) -> Response:
    """FR-PIPELINE-17: the owner's open runs (unfinished or budget-stopped), for the lesson list."""
    if not _owner(ctx):
        return error(404, "not_found")
    return JSONResponse({"runs": await runs.open_runs(ctx.engine, _jobs(ctx), ctx.signed_in.id)})


async def cancel(ctx: Ctx) -> Response:
    """FR-PIPELINE-20: cancel an open run (idempotent); a finished one is 409 `not_running`."""
    run = _uuid(ctx.request.path_params["run_id"])
    if not _owner(ctx) or run is None:
        return error(404, "not_found")
    try:
        return JSONResponse(await runs.cancel(ctx.engine, _jobs(ctx), ctx.signed_in.id, run))
    except PipelineError as e:
        return _failure(e)


async def run_history(ctx: Ctx) -> Response:
    """FR-PIPELINE-21: the lesson's newest runs, read-only."""
    lesson = _uuid(ctx.request.path_params["lesson_id"])
    if not _owner(ctx) or lesson is None:
        return error(404, "not_found")
    try:
        return JSONResponse({"runs": await runs.history(ctx.engine, _jobs(ctx), ctx.signed_in.id, lesson)})
    except PipelineError as e:
        return _failure(e)


ROUTES: tuple[Route, ...] = (
    Route("POST", "/api/pipeline/courses", "session", import_course),
    Route("POST", "/api/lessons/{lesson_id}/compile", "session", compile_lesson),
    Route("GET", "/api/lessons/{lesson_id}/compile", "session", latest_run),
    Route("GET", "/api/compiles", "session", open_runs),
    Route("GET", "/api/compiles/{run_id}", "session", get_run),
    Route("POST", "/api/compiles/{run_id}/resume", "session", resume),
    Route("POST", "/api/compiles/{run_id}/cancel", "session", cancel),  # FR-PIPELINE-20
    Route("GET", "/api/lessons/{lesson_id}/compiles", "session", run_history),  # FR-PIPELINE-21
)
