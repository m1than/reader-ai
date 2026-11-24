"""SRV-OBS routes (FR-OBS-9, FR-OBS-10): the owner gate Caddy's forward_auth calls for every request to the
dashboard, and the owner's summary. Registered through SRV-AUTH's ROUTERS (FR-AUTH-33)."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any, Final

from starlette.responses import JSONResponse, Response

from reader.api.http import Ctx, Route, error
from reader.auth import store
from reader.auth.policy import SESSION_COOKIE
from reader.obs.gate_rules import DASHBOARD_ORIGIN, decide, precheck

__all__ = ["DASHBOARD_ORIGIN", "ROUTES", "decide"]

log = logging.getLogger("reader.obs")
DAYS: Final = (1, 90)
DAYS_TEXT: Final = re.compile(r"[0-9]{1,2}")


async def gate(ctx: Ctx) -> Response:
    """204 for the owner, 401 without a session, 403 otherwise; no body, no cookie, never extends a
    session."""
    h = ctx.request.headers
    shape = (h.get("origin"), h.get("x-forwarded-method"), h.get("sec-fetch-site"), h.get("sec-fetch-mode"),
             h.get("sec-fetch-dest"))
    site = h.get("x-reader-site")  # FR-OBS-9: set by Caddy, never trusted from a client (Caddy overrides it)
    refused = precheck(*shape, site)
    if refused is not None:
        return Response(status_code=refused)
    user = await store.session_user(ctx.engine, ctx.request.cookies.get(SESSION_COOKIE))
    return Response(status_code=decide(*shape, user.role if user is not None else None, site))


async def summary(ctx: Ctx) -> Response:
    from reader.obs.summary import summary as build

    user = ctx.signed_in
    if user.role != "owner":
        return error(403, "forbidden")
    raw = ctx.request.query_params.get("days", "7")
    # ASCII digits only: str.isdigit accepts "²", which int() refuses (code review round 1).
    if not DAYS_TEXT.fullmatch(raw) or not DAYS[0] <= int(raw) <= DAYS[1]:
        return error(400, "days")
    days = int(raw)
    from reader.obs import alerts, phase2

    now = datetime.now(UTC)
    async with ctx.engine.connect() as conn:
        body = await build(conn, user.id, days, now)
        dead: dict[str, int] = {}
        for group in body["failures"]["jobs"]:  # FR-OBS-25 dead_jobs: the summary's own window and scope
            dead[str(group["kind"])] = dead.get(str(group["kind"]), 0) + int(group["count"])
        found = await alerts.load(conn, user.id, dead, now, tracing=phase2.tracing_on())
        stack = await alerts.stack(conn)
    return JSONResponse({"days": days, **body, "alerts": found, "stack": stack})


async def _body(ctx: Ctx, limit: int) -> tuple[Any, bool]:
    """(the JSON body or None, over the limit). FR-OBS-23's upload limit is larger than Ctx.json's."""
    declared = ctx.request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None, True
    raw = bytearray()
    async for chunk in ctx.request.stream():
        raw += chunk
        if len(raw) > limit:
            return None, True
    try:
        return json.loads(raw), False
    except ValueError, RecursionError:
        return None, False


async def _enqueue_after(ctx: Ctx, kind: str, args: dict[str, Any]) -> None:
    """The outbox row is committed; a queue in this process only makes the job prompt (never fails)."""
    jobs = getattr(ctx.request.app.state, "jobs", None)
    if jobs is None:
        return
    try:
        await jobs.enqueue(kind, args)
    except Exception:
        log.warning("obs enqueue_failed kind=%s", kind)


async def bench_upload(ctx: Ctx) -> Response:
    """FR-OBS-23: record an owner's bench result (202), never start a bench."""
    from reader.obs import bench

    user = ctx.signed_in
    if user.role != "owner":
        return error(403, "forbidden")
    body, too_large = await _body(ctx, bench.MAX_BYTES)
    if too_large:
        return error(413, "too_large")
    if not isinstance(body, dict) or set(body) != {"set", "result"} or not isinstance(body["set"], str):
        return error(400, "bad_request")
    try:
        upload = bench.validate(body["set"], body["result"])
    except bench.BenchError as refused:
        return error(400, refused.code)
    async with ctx.engine.begin() as conn:
        upload_id = await bench.record(conn, user.id, upload)
    if upload_id is None:
        return error(409, "already_recorded")
    await _enqueue_after(ctx, bench.KIND, {"upload_id": str(upload_id)})
    return JSONResponse({"upload_id": str(upload_id)}, status_code=202)


async def label(ctx: Ctx) -> Response:
    """FR-OBS-24: the owner's label of a lesson or an answer; a relabel raises the row's revision."""
    from pydantic import ValidationError

    from reader.obs import labels, phase2

    user = ctx.signed_in
    if user.role != "owner":
        return error(403, "forbidden")
    raw = await ctx.json()
    try:
        body = labels.LabelBody.model_validate(raw)
    except ValidationError:
        return error(400, "bad_request")
    mirror_on = phase2.tracing_on()
    async with ctx.engine.begin() as conn:
        got = await labels.record(conn, user.id, body, mirror_on=mirror_on)
    if got is None:
        return error(404, "not_found")
    revision, label_id = got
    if mirror_on:
        await _enqueue_after(ctx, labels.KIND, {"label_id": label_id, "revision": revision})
    return JSONResponse({"revision": revision, "mirror": "queued" if mirror_on else "off"})  # FR-OBS-24 (b)


ROUTES: Final[tuple[Route, ...]] = (
    Route("GET", "/api/obs/gate", "public", gate),
    Route("GET", "/api/obs/summary", "session", summary),
    Route("POST", "/api/obs/bench", "session", bench_upload),
    Route("POST", "/api/obs/labels", "session", label),
)
