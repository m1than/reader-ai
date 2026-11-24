"""SRV-OBS routes (FR-OBS-9, FR-OBS-10): the owner gate Caddy's forward_auth calls for every request to the
dashboard, and the owner's summary. Registered through SRV-AUTH's ROUTERS (FR-AUTH-33)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Final

from starlette.responses import JSONResponse, Response

from reader.api.http import Ctx, Route, error
from reader.auth import store
from reader.auth.policy import SESSION_COOKIE
from reader.obs.gate_rules import DASHBOARD_ORIGIN, decide, precheck

__all__ = ["DASHBOARD_ORIGIN", "ROUTES", "decide"]

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
    async with ctx.engine.connect() as conn:
        body = await build(conn, user.id, days, datetime.now(UTC))
    return JSONResponse({"days": days, **body})


ROUTES: Final[tuple[Route, ...]] = (
    Route("GET", "/api/obs/gate", "public", gate),
    Route("GET", "/api/obs/summary", "session", summary),
)
