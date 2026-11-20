"""Core api routes: health and the signed-in user (SRV-AUTH FR-AUTH-9, FR-AUTH-10)."""

from __future__ import annotations

from sqlalchemy import text
from starlette.responses import JSONResponse, Response

from reader.api.http import Ctx, Route, error


async def healthz(ctx: Ctx) -> Response:
    try:
        async with ctx.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        return error(503, "unhealthy")
    return JSONResponse({"status": "ok"})


async def me(ctx: Ctx) -> Response:
    user = ctx.user
    if user is None:
        return error(401, "unauthenticated")
    return JSONResponse(
        {
            "id": str(user.id),
            "handle": user.handle,
            "role": user.role,
            "display_name": user.display_name,
            "consent": {"evaluation": user.consent_evaluation, "tuning": user.consent_tuning},
        }
    )


ROUTES: tuple[Route, ...] = (
    Route("GET", "/api/healthz", "public", healthz),
    Route("GET", "/api/me", "session", me),
)
