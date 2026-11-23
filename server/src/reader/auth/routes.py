"""SRV-AUTH routes: ceremonies, sign-out and the two stage-1 pages (FR-AUTH-5..9, -11, -13, -14, -18)."""

from __future__ import annotations

import logging
from importlib.resources import files
from typing import Any

from starlette.responses import JSONResponse, Response

from reader.api.http import Ctx, Handler, Route, error, set_cookie
from reader.auth import store
from reader.auth.policy import (
    CEREMONY_COOKIE,
    CHALLENGE_TTL_S,
    SESSION_COOKIE,
    SESSION_TTL_S,
    FailureReason,
)

log = logging.getLogger("reader.auth")
_STATIC = files("reader.auth") / "static"


def _failed(ctx: Ctx, reason: FailureReason, code: str) -> Response:
    # FR-AUTH-18: the route and the reason, nothing from the request or the response.
    log.warning("auth failure %s %s", ctx.request.url.path, reason.value)
    return error(400, code)


def _limited(handler: Handler) -> Handler:
    """FR-AUTH-11: while the failure limit is reached, answer 429 without touching anything else."""

    async def wrapped(ctx: Ctx) -> Response:
        retry = await store.retry_after(ctx.engine)
        if retry is not None:
            return error(429, "rate_limited", {"Retry-After": str(retry)})
        return await handler(ctx)

    return wrapped


def _options(options: dict[str, Any], ceremony: str) -> Response:
    response = JSONResponse(options)
    set_cookie(response, CEREMONY_COOKIE, ceremony, CHALLENGE_TTL_S)
    return response


def _session(session: str) -> Response:
    response = JSONResponse({"status": "ok"})
    set_cookie(response, SESSION_COOKIE, session, SESSION_TTL_S)
    return response


async def _credential(ctx: Ctx) -> dict[str, Any]:
    body = await ctx.json()
    return body if isinstance(body, dict) else {}


async def bootstrap_options(ctx: Ctx) -> Response:
    body = await ctx.json()
    token = body.get("token") if isinstance(body, dict) else None
    result = (
        await store.bootstrap_options(ctx.engine, ctx.settings, token) if isinstance(token, str) else None
    )
    if not isinstance(result, tuple):
        reason = result or FailureReason.token_invalid
        await store.record_failure(ctx.engine, reason)
        return _failed(ctx, reason, "bootstrap_failed")
    return _options(*result)


async def bootstrap_verify(ctx: Ctx) -> Response:
    result = await store.bootstrap_verify(
        ctx.engine, ctx.settings, ctx.cookie(CEREMONY_COOKIE), await _credential(ctx), ctx.session
    )
    if isinstance(result, FailureReason):
        return _failed(ctx, result, "bootstrap_failed")
    return _session(result)


# register/* are `recent_session` routes: the dispatcher (reader.api.app) has already enforced the session.


async def register_options(ctx: Ctx) -> Response:
    return _options(*await store.register_options(ctx.engine, ctx.settings, ctx.signed_in))


async def register_verify(ctx: Ctx) -> Response:
    result = await store.register_verify(
        ctx.engine,
        ctx.settings,
        ctx.signed_in,
        ctx.cookie(CEREMONY_COOKIE),
        await _credential(ctx),
        ctx.session,
    )
    if isinstance(result, FailureReason):
        return _failed(ctx, result, "register_failed")
    return _session(result)


async def signin_options(ctx: Ctx) -> Response:
    return _options(*await store.signin_options(ctx.engine, ctx.settings))


async def signin_verify(ctx: Ctx) -> Response:
    result = await store.signin_verify(
        ctx.engine, ctx.settings, ctx.cookie(CEREMONY_COOKIE), await _credential(ctx), ctx.session
    )
    if isinstance(result, FailureReason):
        return _failed(ctx, result, "signin_failed")
    return _session(result)


async def signout(ctx: Ctx) -> Response:
    await store.revoke_session(ctx.engine, ctx.session)
    response = Response(status_code=204)
    set_cookie(response, SESSION_COOKIE, "", 0)
    return response


def _static(name: str, media_type: str) -> Handler:
    async def serve(ctx: Ctx) -> Response:
        try:
            content = (_STATIC / name).read_bytes()
        except OSError:  # a damaged install, not a database failure
            log.warning("static_unavailable %s", ctx.request.url.path)
            return error(503, "unavailable")
        # FR-AUTH-21: revalidate before each reuse; with no validator sent, the browser refetches.
        return Response(content, media_type=media_type, headers={"Cache-Control": "no-cache"})

    return serve


ROUTES: tuple[Route, ...] = (
    Route("POST", "/api/auth/bootstrap/options", "public", _limited(bootstrap_options)),
    Route("POST", "/api/auth/bootstrap/verify", "public", _limited(bootstrap_verify)),
    Route("POST", "/api/auth/register/options", "recent_session", _limited(register_options)),
    Route("POST", "/api/auth/register/verify", "recent_session", _limited(register_verify)),
    Route("POST", "/api/auth/signin/options", "public", _limited(signin_options)),
    Route("POST", "/api/auth/signin/verify", "public", _limited(signin_verify)),
    Route("POST", "/api/auth/signout", "public", signout),
    Route("GET", "/api/auth/bootstrap", "public", _static("bootstrap.html", "text/html")),
    Route("GET", "/api/auth/signin", "public", _static("signin.html", "text/html")),
    Route("GET", "/api/auth/static/passkey.js", "public", _static("passkey.js", "text/javascript")),
    Route("GET", "/api/auth/static/auth.css", "public", _static("auth.css", "text/css")),  # FR-AUTH-21
)
