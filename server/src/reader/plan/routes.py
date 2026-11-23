"""Plan routes, declared as data for SRV-AUTH's ROUTERS (SRV-PLAN FR-PLAN-9, FR-PLAN-12).

Owner role only (403 `owner_only`). Preview writes nothing; confirm holds the course lock (SRV-PIPELINE
FR-PIPELINE-14) across the replay check, reading the started lessons, the hash check and the import; the first
lesson's compile starts after the commit, under the same lock, only while no lesson of the course has a run.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import time
import uuid
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.responses import JSONResponse, Response

from reader.api.http import Ctx, Route, error
from reader.config import ConfigError, load_config
from reader.config.model import Material
from reader.pipeline import courses, runs
from reader.pipeline.errors import PipelineError
from reader.plan import estimate, params, planner, store

DEFAULT_CONFIG: Final = "/etc/reader/reader.yaml"
RATE: Final = re.compile(r"[0-9]{1,2}(\.[0-9]{1,2})?")  # ASCII digits only (`\d` also matches other scripts)
log = logging.getLogger("reader.plan")


class _Refused(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _materials(ctx: Ctx) -> Sequence[Material]:
    injected = getattr(ctx.request.app.state, "ingest_materials", None)
    if injected is not None:
        return tuple(injected)
    try:
        return load_config(os.environ.get("READER_CONFIG", DEFAULT_CONFIG)).materials
    except ConfigError:
        raise _Refused(503, "config_unavailable") from None


def _int(body: dict[str, Any], key: str) -> int:
    value = body.get(key)
    if type(value) is not int:
        raise _Refused(400, key)
    return value


def _rate(body: dict[str, Any]) -> int:
    raw = body.get("rate", str(params.RATE_DEFAULT))
    if not isinstance(raw, str) or not RATE.fullmatch(raw):
        raise _Refused(400, "rate")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise _Refused(400, "rate") from None
    if not params.RATE_MIN <= value <= params.RATE_MAX:
        raise _Refused(400, "rate")
    return int(value * 100)


def _date(body: dict[str, Any]) -> dt.date:
    raw = body.get("today")
    try:
        if not isinstance(raw, str):
            raise ValueError
        return dt.date.fromisoformat(raw)
    except ValueError:
        raise _Refused(400, "today") from None


def _today_in_range(today: dt.date) -> None:
    server = dt.datetime.now(dt.UTC).date()
    if abs((today - server).days) > params.TODAY_SKEW_DAYS:
        raise _Refused(400, "today")


class _Request:
    """The validated shape of a preview or confirm body (FR-PLAN-9)."""

    def __init__(self, ctx: Ctx, body: Any) -> None:
        if not isinstance(body, dict):
            raise _Refused(400, "material_id")
        material_id = body.get("material_id")
        if not isinstance(material_id, str):
            raise _Refused(400, "material_id")
        self.weeks = _int(body, "total_weeks")
        if not planner.WEEKS_MIN <= self.weeks <= planner.WEEKS_MAX:
            raise _Refused(400, "total_weeks")
        self.minutes = _int(body, "minutes_per_day")
        m = self.minutes
        if not planner.MINUTES_MIN <= m <= planner.MINUTES_MAX or m % planner.MINUTES_STEP:
            raise _Refused(400, "minutes_per_day")
        self.rate_centi = _rate(body)
        self.today = _date(body)
        self.body = body
        material = next((x for x in _materials(ctx) if x.id == material_id), None)
        if material is None:
            raise _Refused(404, "not_found")
        self.material = material
        self.course = f"plan-{material.sha256[:12]}"


def _rate_text(rate_centi: int) -> str:
    return f"{rate_centi // 100}.{rate_centi % 100:02d}"


async def _compute(conn: AsyncConnection, owner: uuid.UUID, req: _Request) -> dict[str, Any]:
    pages = await store.page_chars(conn, owner, req.material.sha256)
    if pages is None:
        raise _Refused(404, "not_found")
    ingest_id, chars, spans = pages
    outline = await store.outline_entries(conn, owner, ingest_id)
    saved = await store.stored(conn, owner, req.course)
    start = saved.start_date if saved is not None else req.today
    inp = planner.PlanInput(
        page_chars=chars,
        page_spans=spans,
        outline=tuple(planner.Entry(level, page) for level, page, _ in outline),
        minutes_per_day=req.minutes,
        total_weeks=req.weeks,
        rate_centi=req.rate_centi,
        start_date=start,
        today=req.today,
        fixed=await store.fixed(conn, owner, req.course),
    )
    try:
        result = planner.plan(inp)
    except planner.PlanInputError as e:
        raise _Refused(422 if e.code == "no_text" else 400, e.code) from None
    titles = [title for _, _, title in outline]
    sha = planner.plan_sha256(req.material.sha256, str(ingest_id), result)
    free = [x.chars for x in result.free]
    first = result.lessons[0].chars if result.lessons else None
    return {
        "plan": result,
        "ingest_id": ingest_id,
        "plan_sha256": sha,
        "inputs": {
            "material_id": req.material.id,
            "total_weeks": req.weeks,
            "minutes_per_day": req.minutes,
            "rate": _rate_text(req.rate_centi),
            "start_date": start.isoformat(),
            "today": result.today.isoformat(),
        },
        "lessons": [
            {
                "id": x.id,
                "pages": [x.first, x.last],
                "minutes": x.minutes,
                "fixed": x.fixed,
                "title": planner.lesson_title(x, titles),
            }
            for x in result.lessons
        ],
        "summary": result.summary_json(),
        "estimate": estimate.estimate(free, first),
    }


def _public(computed: dict[str, Any], titles: bool = True) -> dict[str, Any]:
    lessons = (
        computed["lessons"]
        if titles
        else [{k: v for k, v in x.items() if k != "title"} for x in computed["lessons"]]
    )
    return {
        "plan_sha256": computed["plan_sha256"],
        "inputs": computed["inputs"],
        "lessons": lessons,
        "summary": computed["summary"],
        "estimate": computed["estimate"],
    }


def _owner_only(ctx: Ctx) -> None:
    if ctx.signed_in.role != "owner":
        raise _Refused(403, "owner_only")


async def preview(ctx: Ctx) -> Response:
    t0 = time.perf_counter()
    try:
        _owner_only(ctx)
        req = _Request(ctx, await ctx.json())
        _today_in_range(req.today)
        async with ctx.engine.connect() as conn:
            computed = await _compute(conn, ctx.signed_in.id, req)
    except _Refused as e:
        return error(e.status, e.code)
    s = computed["summary"]
    log.info(
        "plan.preview material=%s lessons=%d fits=%s sha=%s duration_ms=%d",
        req.material.sha256[:12],
        s["lesson_count"],
        s["fits"],
        computed["plan_sha256"][:12],
        (time.perf_counter() - t0) * 1000,
    )
    return JSONResponse(_public(computed))


SHA256: Final = re.compile(r"[0-9a-f]{64}")


async def confirm(ctx: Ctx) -> Response:
    t0 = time.perf_counter()
    owner = ctx.signed_in.id
    try:
        _owner_only(ctx)
        body = await ctx.json()
        req = _Request(ctx, body)
        sha = body.get("plan_sha256")
        if not isinstance(sha, str) or not SHA256.fullmatch(sha):
            raise _Refused(400, "plan_sha256")
        compile_first = body.get("compile_first")
        if type(compile_first) is not bool:
            raise _Refused(400, "compile_first")
        created = removed = 0
        async with ctx.engine.begin() as conn:
            await courses.lock_course(conn, owner, req.course)
            saved = await store.stored(conn, owner, req.course)
            replayed = False
            if saved is not None and saved.plan_sha256 == sha:  # the stored sha alone decides
                replayed = True
                course_id = saved.course_id
                lessons = await store.plan_lessons(conn, owner, req.course)
            else:
                _today_in_range(req.today)
                computed = await _compute(conn, owner, req)
                if computed["plan_sha256"] != sha:
                    return JSONResponse(
                        {"error": "plan_changed", "plan": _public(computed, titles=False)}, status_code=409
                    )
                result: planner.Plan = computed["plan"]
                if not result.summary["fits"]:
                    raise _Refused(422, "does_not_fit")
                titled = {x["id"]: x["title"] for x in computed["lessons"]}
                plan = courses.Plan(
                    req.course,
                    req.material.id,
                    req.material.sha256,
                    [courses.PlanLesson(x.id, titled[x.id], x.first, x.last) for x in result.lessons],
                )
                try:
                    imported = await courses.import_course_plan(conn, owner, plan, origin="study_plan")
                except PipelineError as e:
                    code = "plan_changed" if e.code == "started_lesson_dropped" else e.code
                    raise _Refused(409, code) from None
                course_id = imported.course_id
                created, removed = imported.created, imported.removed
                await store.save(
                    conn,
                    owner,
                    course_id,
                    req.course,
                    req.material.sha256,
                    computed["ingest_id"],
                    req.weeks,
                    req.minutes,
                    req.rate_centi,
                    result.input.start_date,
                    sha,
                )
                lessons = [
                    {"plan_lesson_id": p, "lesson_id": str(lid), "ordinal": o}
                    for p, lid, o in imported.lessons
                ]
    except _Refused as e:
        return error(e.status, e.code)
    compiled: dict[str, str] | None = None
    if compile_first and lessons:
        first = min(lessons, key=lambda x: x["ordinal"])
        try:
            run = await runs.create_run(
                ctx.engine,
                ctx.request.app.state.jobs,
                owner,
                uuid.UUID(first["lesson_id"]),
                params.RUN_CAP_USD,
                require_course_unstarted=True,
            )
            compiled = {"run_id": str(run)}
        except PipelineError as e:
            compiled = {"error": e.code}
    log.info(
        "plan.confirm material=%s lessons=%d replayed=%s created=%d removed=%d compile=%s sha=%s"
        " duration_ms=%d",
        req.material.sha256[:12],
        len(lessons),
        replayed,
        created,
        removed,
        compiled is not None and "run_id" in compiled,
        sha[:12],
        (time.perf_counter() - t0) * 1000,
    )
    return JSONResponse(
        {
            "course_id": str(course_id),
            "plan_course_id": req.course,
            "lessons": lessons,
            "created": created,
            "removed": removed,
            "replayed": replayed,
            "compile": compiled,
        }
    )


async def get_plan(ctx: Ctx) -> Response:
    try:
        _owner_only(ctx)
        ids = ctx.request.query_params.getlist("material_id")
        if len(ids) != 1:
            raise _Refused(400, "material_id")
        material = next((x for x in _materials(ctx) if x.id == ids[0]), None)
        if material is None:
            raise _Refused(404, "not_found")
        course = f"plan-{material.sha256[:12]}"
        owner = ctx.signed_in.id
        async with ctx.engine.connect() as conn:
            saved = await store.stored(conn, owner, course)
            if saved is None:
                raise _Refused(404, "not_found")
            started = len(await store.fixed(conn, owner, course))
    except _Refused as e:
        return error(e.status, e.code)
    return JSONResponse(
        {
            "course_id": str(saved.course_id),
            "plan_course_id": course,
            "inputs": {
                "material_id": material.id,
                "total_weeks": saved.total_weeks,
                "minutes_per_day": saved.minutes_per_day,
                "rate": _rate_text(saved.rate_centi),
                "start_date": saved.start_date.isoformat(),
            },
            "plan_sha256": saved.plan_sha256,
            "started": started,
        }
    )


async def lesson_estimate(ctx: Ctx) -> Response:
    """FR-PLAN-13: one lesson's compile estimate, read-only."""
    try:
        _owner_only(ctx)
        try:
            lesson = uuid.UUID(ctx.request.path_params["lesson_id"])
        except ValueError:
            raise _Refused(404, "not_found") from None
        async with ctx.engine.connect() as conn:
            chars = await store.lesson_chars(conn, ctx.signed_in.id, lesson)
        if chars is None:
            raise _Refused(404, "not_found")
    except _Refused as e:
        return error(e.status, e.code)
    return JSONResponse({"lesson_id": str(lesson), **estimate.lesson_estimate(chars)})


ROUTES: tuple[Route, ...] = (
    Route("POST", "/api/plans/preview", "session", preview),
    Route("POST", "/api/plans/confirm", "session", confirm),
    Route("GET", "/api/plans", "session", get_plan),
    Route("GET", "/api/lessons/{lesson_id}/estimate", "session", lesson_estimate),
)
