"""Freshness routes (SRV-FRESH FR-FRESH-1, -9, -11; INV-FRESH-4), declared as data for SRV-AUTH's ROUTERS.

Session, owner role and the material's owner only: anyone else, an unknown id, a malformed id and a deleted
material (FR-FRESH-20) get the same 404 `not_found`. The models and the material's language come from
reader.yaml (`app.state.freshness_config` in tests); without it the routes answer 503 `config_unavailable`.
Logs carry ids and codes, never a field value.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from starlette.responses import JSONResponse, Response

from reader.api.http import Ctx, Route, error
from reader.config import ReaderConfig, load_config
from reader.freshness import check, params, store
from reader.freshness.bibliography import BibliographyInvalid, validate
from reader.freshness.errors import FreshError
from reader.ingest import manage
from reader.jobs.model import idempotency_key

log = logging.getLogger("reader.freshness")
DEFAULT_CONFIG: Final = "/etc/reader/reader.yaml"
SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


def _not_found() -> JSONResponse:
    return error(404, "not_found")


def _config(ctx: Ctx) -> ReaderConfig:
    injected = getattr(ctx.request.app.state, "freshness_config", None)
    if injected is not None:
        config: ReaderConfig = injected
        return config
    try:
        return load_config(Path(os.environ.get("READER_CONFIG", DEFAULT_CONFIG)))
    except Exception:
        raise FreshError("config_unavailable") from None


async def _material(ctx: Ctx) -> store.MaterialRow | None:
    if ctx.signed_in.role != "owner":
        return None
    async with ctx.engine.connect() as conn:
        return await store.live_material(conn, ctx.signed_in.id, ctx.request.path_params["material_id"])


def _fresh_error(failure: FreshError) -> JSONResponse:
    status = {"not_imported": 404, "config_unavailable": 503, "freshness_running": 409}.get(failure.code, 400)
    return error(status, failure.code)


async def _job_status(ctx: Ctx, kind: str, report_id: uuid.UUID) -> str:
    from reader.jobs.queue import job_status_of

    key = idempotency_key(_kind(kind), {"report_id": str(report_id)})
    async with ctx.engine.connect() as conn:
        return await job_status_of(conn, kind, key)


def _kind(name: str) -> Any:
    from reader.freshness.jobs import JOB_KINDS

    return next(k for k in JOB_KINDS if k.name == name)


async def finalize_stale(ctx: Ctx, owner_id: uuid.UUID) -> None:
    """FR-FRESH-12: a running report whose job key is dead, or with no live job for `stale_without_job_s`,
    is stored `failed` with `job_failed` (the handlers finish every report they run)."""
    async with ctx.engine.connect() as conn:
        running = await store.running_of(conn, owner_id)
    now = datetime.now(UTC)
    for report in running:
        scan_job = await _job_status(ctx, params.KIND_SCAN, report.id)
        web_job = await _job_status(ctx, params.KIND_WEB, report.id)
        statuses = {scan_job, web_job}
        old = store.age_s(report, now) > params.STALE_WITHOUT_JOB_S
        lost = old and not statuses & {"queued", "running"}
        ended = ("dead" in statuses or lost) and await store.finish(
            ctx.engine, report.id, "failed", error="job_failed"
        )
        if ended:
            log.info("freshness_finalised report=%s scan=%s web=%s", report.id.hex[:8], scan_job, web_job)


async def get_freshness(ctx: Ctx) -> Response:
    material = await _material(ctx)
    if material is None:
        return _not_found()
    owner = ctx.signed_in.id
    try:
        config = _config(ctx)
        await finalize_stale(ctx, owner)
        async with ctx.engine.connect() as conn:
            prep = await check.prepare(conn, config, owner, material)
            latest = await store.latest(conn, owner, material.id)
            bib = await store.bibliography(conn, owner, material.id)
            scan_bib = await store.latest_scan_bibliography(conn, owner, material.id)
    except FreshError as failure:
        return _fresh_error(failure)
    reason = check.stale_reason(latest, material.ingest_id, bib["sha256"] if bib else None, datetime.now(UTC))
    body = {
        "report": None if latest is None else check.report_view(latest, prep.topics),
        "stale": reason is not None,
        "stale_reason": reason,
        "bibliography": bib,
        "scan_bibliography": scan_bib,
        "estimate": prep.estimate.as_json(),
        "daily_cap_usd": str(params.DAILY_CAP_USD),
    }
    return JSONResponse(body)


async def get_report(ctx: Ctx) -> Response:
    material = await _material(ctx)
    try:
        report_id = uuid.UUID(ctx.request.path_params["report_id"])
    except ValueError:
        return _not_found()
    if material is None:
        return _not_found()
    await finalize_stale(ctx, ctx.signed_in.id)
    async with ctx.engine.connect() as conn:
        report = await store.load_owned(conn, ctx.signed_in.id, material.id, report_id)
        topics = None
        if report is not None and report["verdict"] is not None:
            from reader.freshness.sample import topics as topics_of

            topics = topics_of(await store.outline(conn, ctx.signed_in.id, report["ingest_id"]))
    if report is None:
        return _not_found()
    return JSONResponse(check.report_view(report, topics))


async def put_bibliography(ctx: Ctx) -> Response:
    body = await ctx.json()  # read before any database connection is taken
    material = await _material(ctx)
    if material is None:
        return _not_found()
    try:
        fields = validate(body, check.today_utc())
    except BibliographyInvalid as invalid:
        log.info("freshness_bibliography material=%s outcome=invalid field=%s", material.id, invalid.field)
        return JSONResponse({"error": invalid.code, "field": invalid.field}, status_code=400)
    async with ctx.engine.begin() as conn:
        await store.save_bibliography(conn, ctx.signed_in.id, material.id, fields, "owner")
        saved = await store.bibliography(conn, ctx.signed_in.id, material.id)
    log.info("freshness_bibliography material=%s outcome=saved", material.id)
    return JSONResponse(saved)


async def post_freshness(ctx: Ctx) -> Response:
    body = await ctx.json()
    material = await _material(ctx)
    if material is None:
        return _not_found()
    owner = ctx.signed_in.id
    try:
        config = _config(ctx)
        await finalize_stale(ctx, owner)
        async with ctx.engine.connect() as conn:
            prep = await check.prepare(conn, config, owner, material)  # not_imported before running
            if await store.running_of(conn, owner):
                raise FreshError("freshness_running")
            owner_bib = await store.bibliography(conn, owner, material.id)
            scan_bib = await store.latest_scan_bibliography(conn, owner, material.id)
    except FreshError as failure:
        return _fresh_error(failure)
    estimate = prep.estimate.as_json()
    if not isinstance(body, dict) or body.get("high_usd") != estimate["high_usd"]:
        return JSONResponse({"error": "estimate_changed", "estimate": estimate}, status_code=409)
    used: dict[str, Any] | None = None
    if owner_bib is not None and owner_bib["source"] == "owner":
        used = owner_bib
    confirm = body.get("bibliography_sha256")
    if confirm is not None:
        if scan_bib is None:
            return error(409, "nothing_to_confirm")
        if not isinstance(confirm, str) or not SHA256.match(confirm) or confirm != scan_bib["sha256"]:
            return error(409, "bibliography_unconfirmed")
        if used is None:  # FR-FRESH-1: the confirmed scan bibliography is the one searched with
            used = {**check.fields_of(scan_bib), "source": "scan", "sha256": confirm}
    spent = await check.spent_today(ctx.engine, owner)
    if spent + prep.estimate.high_usd > params.DAILY_CAP_USD:
        log.info("freshness_post material=%s outcome=daily_budget_exhausted", material.id)
        return JSONResponse(
            {
                "error": "daily_budget_exhausted",
                "daily_cap_usd": str(params.DAILY_CAP_USD),
                "spent_usd": str(spent.quantize(params.DAILY_CAP_USD)),
            },
            status_code=429,  # as SRV-TUTOR's daily cap
        )
    try:
        report_id = await store.insert(
            ctx.engine, owner, material.id, prep.ingest_id, prep.scan_inputs_sha256, used,
            prep.sample.stored(material.sha256),
        )
    except FreshError as failure:
        return _fresh_error(failure)
    outcome = await _enqueue(ctx, report_id)
    log.info("freshness_post material=%s report=%s outcome=%s", material.id, report_id.hex[:8], outcome)
    return JSONResponse({"report_id": str(report_id)}, status_code=202)


async def _enqueue(ctx: Ctx, report_id: uuid.UUID) -> str:
    jobs = getattr(ctx.request.app.state, "jobs", None)
    try:
        if jobs is None:
            raise LookupError("no Jobs on the app")
        await jobs.enqueue(params.KIND_SCAN, {"report_id": str(report_id)})
    except Exception as failure:  # the report must not stay running
        log.warning("freshness_enqueue_failed report=%s error=%s", report_id.hex[:8], type(failure).__name__)
        await store.finish(ctx.engine, report_id, "failed", error="job_failed")
        return "job_failed"
    return "queued"


# FR-FRESH-20: SRV-INGEST's material delete is refused while a report of it runs (409 material_busy).
if store.running_for not in manage.BUSY_CHECKS:
    manage.BUSY_CHECKS.append(store.running_for)

ROUTES: Final[tuple[Route, ...]] = (
    Route("GET", "/api/materials/{material_id}/freshness", "session", get_freshness),
    Route("POST", "/api/materials/{material_id}/freshness", "session", post_freshness),
    Route("GET", "/api/materials/{material_id}/freshness/{report_id}", "session", get_report),
    Route("PUT", "/api/materials/{material_id}/bibliography", "session", put_bibliography),
)
