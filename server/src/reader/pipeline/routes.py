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
from reader.pipeline.errors import PipelineError

RUN_CAP_USD: Final = Decimal("8.00")  # bounds.run_cap_usd
STATUS: Final = {
    "no_material": 409,
    "compile_running": 409,
    "lesson_unknown": 404,
    "not_found": 404,
    "plan_invalid": 400,
    "no_source_spans": 409,
    "source_too_large": 409,
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
    return JSONResponse(await runs.status(ctx.engine, _jobs(ctx), ctx.signed_in.id, run))


async def resume(ctx: Ctx) -> Response:
    run = _uuid(ctx.request.path_params["run_id"])
    if not _owner(ctx) or run is None:
        return error(404, "not_found")
    jobs = _jobs(ctx)
    try:
        state = await runs.derive(ctx.engine, jobs, ctx.signed_in.id, run)
    except PipelineError as e:
        return _failure(e)
    pending = state["_pending"]
    if state["state"] != "stage_failed" or pending is None or pending["args"] is None:
        return error(409, "not_failed")
    kind = runs.kind_for(pending["stage"])
    await jobs.requeue_dead(kind, idempotency_key(jobs.catalog[kind], pending["args"]))
    result = await runs.status(ctx.engine, jobs, ctx.signed_in.id, run)
    return JSONResponse(result, status_code=202)


ROUTES: tuple[Route, ...] = (
    Route("POST", "/api/pipeline/courses", "session", import_course),
    Route("POST", "/api/lessons/{lesson_id}/compile", "session", compile_lesson),
    Route("GET", "/api/lessons/{lesson_id}/compile", "session", latest_run),
    Route("GET", "/api/compiles/{run_id}", "session", get_run),
    Route("POST", "/api/compiles/{run_id}/resume", "session", resume),
)
