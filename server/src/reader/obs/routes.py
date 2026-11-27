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
    try:
        async with ctx.engine.begin() as conn:
            got = await labels.record(conn, user.id, body, mirror_on=mirror_on)
    except labels.NotLabelable:
        return error(409, "not_labelable")
    if got is None:
        return error(404, "not_found")
    revision, label_id = got
    if mirror_on:
        await _enqueue_after(ctx, labels.KIND, {"label_id": label_id, "revision": revision})
    return JSONResponse({"revision": revision, "mirror": "queued" if mirror_on else "off"})  # FR-OBS-24 (b)


NO_STORE: Final = {"Cache-Control": "no-store"}
LIMIT_TEXT: Final = re.compile(r"[0-9]{1,2}")
MAX_EVAL_BYTES: Final = 262_144


async def judge_queue(ctx: Ctx) -> Response:
    """ADR-0039 FR-OBS-40: unlabelled judge items of one slot, in B7 item 3's order."""
    from reader.obs import judge_labels, labels

    user = ctx.signed_in
    if user.role != "owner":
        return error(403, "forbidden")
    slot = ctx.request.query_params.get("slot", "")
    raw = ctx.request.query_params.get("limit", str(judge_labels.QUEUE_MAX))
    if slot not in labels.JUDGE_SLOTS or not LIMIT_TEXT.fullmatch(raw) or not 1 <= int(raw) <= 25:
        return error(400, "bad_request")
    async with ctx.engine.connect() as conn:
        items = await judge_labels.queue(conn, user.id, slot, int(raw))
    return JSONResponse({"items": items}, headers=NO_STORE)


async def judge_item(ctx: Ctx) -> Response:
    """ADR-0039 FR-OBS-40: one item with its query and the cards as the owner may read them under B8."""
    from reader.obs import judge_labels

    user = ctx.signed_in
    if user.role != "owner":
        return error(403, "forbidden")
    async with ctx.engine.connect() as conn:
        try:
            found = await judge_labels.item(conn, user.id, str(ctx.request.path_params["item_id"]))
        except judge_labels.NotLabelable:
            return error(409, "not_labelable")
    if found is None:
        return error(404, "not_found")
    return JSONResponse(found, headers=NO_STORE)


async def eval_start(ctx: Ctx) -> Response:
    """ADR-0039 FR-OBS-39 (a): start a local judge evaluation over a pinned set (202 {eval_id})."""
    from reader.obs import eval as evaluation

    user = ctx.signed_in
    if user.role != "owner":
        return error(403, "forbidden")
    body, too_large = await _body(ctx, MAX_EVAL_BYTES)
    if too_large:
        return error(413, "too_large")
    keys = set(body) if isinstance(body, dict) else set()
    if not {"set", "budget_profile"} <= keys <= {"set", "budget_profile", "judges"}:
        return error(400, "bad_request")
    judges = body.get("judges", list(evaluation.EVAL_SLOTS))
    ok = isinstance(body["set"], str) and isinstance(body["budget_profile"], str) and isinstance(judges, list)
    if not ok or not all(isinstance(j, str) for j in judges):
        return error(400, "bad_request")
    cap, floors = evaluation.eval_budget()
    try:
        async with ctx.engine.begin() as conn:
            eval_id = await evaluation.start(conn, user.id, body["budget_profile"], judges,
                                             set_text=body["set"], cap_usd=cap, floors=floors)
    except evaluation.StartRefused as refused:
        return error(400, refused.code)
    return JSONResponse({"eval_id": str(eval_id)}, status_code=202)


ROUTES: Final[tuple[Route, ...]] = (
    Route("GET", "/api/obs/gate", "public", gate),
    Route("GET", "/api/obs/summary", "session", summary),
    Route("POST", "/api/obs/bench", "session", bench_upload),
    Route("POST", "/api/obs/labels", "session", label),
    # ADR-0039 (FR-OBS-39, -40)
    Route("GET", "/api/obs/judge-queue", "session", judge_queue),
    Route("GET", "/api/obs/judge-items/{item_id}", "session", judge_item),
    Route("POST", "/api/obs/eval", "session", eval_start),
)
