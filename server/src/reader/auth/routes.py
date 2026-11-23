"""SRV-AUTH routes: ceremonies, sign-out, the pages (FR-AUTH-5..9, -11, -13, -14, -18) and open sign-up with
the owner's approval routes (FR-AUTH-23, FR-AUTH-26..29, FR-AUTH-31)."""

from __future__ import annotations

import logging
from functools import cache
from importlib.resources import files
from typing import Any, Final

from starlette.responses import JSONResponse, Response

from reader.api.http import Ctx, Handler, Route, error, set_cookie
from reader.auth import signup, store
from reader.auth.policy import (
    CEREMONY_COOKIE,
    CHALLENGE_TTL_S,
    SESSION_COOKIE,
    SESSION_TTL_S,
    FailureReason,
    SignupMode,
)

log = logging.getLogger("reader.auth")
_STATIC = files("reader.auth") / "static"
# FR-AUTH-31: the sign-in page carries this marker once; the api replaces it with the fixed link fragment
# while sign-up is open and removes it while it is off. No request data reaches the page.
SIGNIN_MARKER: Final = "<!--signup-link-->"


def _failed(ctx: Ctx, reason: FailureReason, code: str) -> Response:
    # FR-AUTH-18: the route and the reason, nothing from the request or the response.
    log.warning("auth failure %s %s", ctx.request.url.path, reason.value)
    return error(400, code)


def _limited(handler: Handler, scope: store.Scope = "auth") -> Handler:
    """FR-AUTH-11 (scope `auth`) and FR-AUTH-26 step 1 (scope `signup`): while that scope's failure limit is
    reached, answer 429 without touching anything else."""

    async def wrapped(ctx: Ctx) -> Response:
        retry = await store.retry_after(ctx.engine, scope)
        if retry is not None:
            return error(429, "rate_limited", {"Retry-After": str(retry)})
        return await handler(ctx)

    return wrapped


def _signup_open(handler: Handler) -> Handler:
    """FR-AUTH-23: while sign-up is off, 404 before the body is read, a row is written or a limit applies."""

    async def wrapped(ctx: Ctx) -> Response:
        if ctx.settings.signup != "open":
            return error(404, "not_found")
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
    if isinstance(result, store.PendingApproval):  # FR-AUTH-28
        log.info("auth pending_signin %s", result.user_id)
        return error(403, "pending_approval")
    return _session(result)


async def signout(ctx: Ctx) -> Response:
    await store.revoke_session(ctx.engine, ctx.session)
    response = Response(status_code=204)
    set_cookie(response, SESSION_COOKIE, "", 0)
    return response


# Sign-up (FR-AUTH-26, FR-AUTH-27) ---------------------------------------------------------------------------

_REFUSAL_STATUS: Final = {
    "rate_limited": 429,
    "display_name_invalid": 400,
    "signup_full": 429,
    "handle_unavailable": 409,
}


def _refused(refusal: signup.Refused) -> Response:
    headers = {"Retry-After": str(refusal.retry_after)} if refusal.retry_after is not None else None
    return error(_REFUSAL_STATUS[refusal.code], refusal.code, headers)


async def signup_options(ctx: Ctx) -> Response:
    result = await signup.options(ctx.engine, ctx.settings, await ctx.json())
    if isinstance(result, signup.Refused):
        return _refused(result)
    return _options(*result)


async def signup_verify(ctx: Ctx) -> Response:
    result = await signup.verify(
        ctx.engine, ctx.settings, ctx.cookie(CEREMONY_COOKIE), await _credential(ctx)
    )
    if isinstance(result, FailureReason):
        return _failed(ctx, result, "signup_failed")
    if isinstance(result, signup.Refused):
        return _refused(result)
    response = JSONResponse({"status": "pending"}, status_code=202)
    set_cookie(response, CEREMONY_COOKIE, "", 0)
    return response


# The owner's sign-up routes (FR-AUTH-29) --------------------------------------------------------------------


def _owner_only(handler: Handler) -> Handler:
    async def wrapped(ctx: Ctx) -> Response:
        if ctx.signed_in.role != "owner":
            return error(403, "owner_only")
        return await handler(ctx)

    return wrapped


async def signups_list(ctx: Ctx) -> Response:
    return JSONResponse(await signup.listing(ctx.engine))


def _action(action: signup.Action) -> Handler:
    async def run(ctx: Ctx) -> Response:
        if not await signup.act(ctx.engine, str(ctx.request.path_params.get("user_id", "")), action):
            return error(404, "not_found")
        return JSONResponse({"status": "active"}) if action == "approve" else Response(status_code=204)

    return run


# Static files (FR-AUTH-14, FR-AUTH-21, FR-AUTH-31) ------------------------------------------------------


def _static_bytes(name: str) -> bytes:
    return (_STATIC / name).read_bytes()


@cache
def signin_page(mode: SignupMode) -> bytes:
    """The sign-in page for a sign-up mode: the marker becomes the fixed link fragment, or nothing."""
    page = _static_bytes("signin.html").decode("utf-8")
    fragment = _static_bytes("signup-link.html").decode("utf-8").strip() if mode == "open" else ""
    return page.replace(SIGNIN_MARKER, fragment).encode("utf-8")


def _serve(content: bytes, media_type: str) -> Response:
    # FR-AUTH-21: revalidate before each reuse; with no validator sent, the browser refetches.
    return Response(content, media_type=media_type, headers={"Cache-Control": "no-cache"})


def _static(name: str, media_type: str) -> Handler:
    async def serve(ctx: Ctx) -> Response:
        try:
            content = _static_bytes(name)
        except OSError:  # a damaged install, not a database failure
            log.warning("static_unavailable %s", ctx.request.url.path)
            return error(503, "unavailable")
        return _serve(content, media_type)

    return serve


async def signin_html(ctx: Ctx) -> Response:
    try:
        content = signin_page(ctx.settings.signup)
    except OSError:
        log.warning("static_unavailable %s", ctx.request.url.path)
        return error(503, "unavailable")
    return _serve(content, "text/html")


ROUTES: tuple[Route, ...] = (
    Route("POST", "/api/auth/bootstrap/options", "public", _limited(bootstrap_options)),
    Route("POST", "/api/auth/bootstrap/verify", "public", _limited(bootstrap_verify)),
    Route("POST", "/api/auth/register/options", "recent_session", _limited(register_options)),
    Route("POST", "/api/auth/register/verify", "recent_session", _limited(register_verify)),
    Route("POST", "/api/auth/signin/options", "public", _limited(signin_options)),
    Route("POST", "/api/auth/signin/verify", "public", _limited(signin_verify)),
    Route("POST", "/api/auth/signout", "public", signout),
    Route("GET", "/api/auth/bootstrap", "public", _static("bootstrap.html", "text/html")),
    Route("GET", "/api/auth/signin", "public", signin_html),
    Route("GET", "/api/auth/static/passkey.js", "public", _static("passkey.js", "text/javascript")),
    Route("GET", "/api/auth/static/auth.css", "public", _static("auth.css", "text/css")),  # FR-AUTH-21
    # FR-AUTH-23, FR-AUTH-26, FR-AUTH-27, FR-AUTH-31: open sign-up.
    Route("GET", "/api/auth/signup", "public", _signup_open(_static("signup.html", "text/html"))),
    Route("POST", "/api/auth/signup/options", "public", _signup_open(_limited(signup_options, "signup"))),
    Route("POST", "/api/auth/signup/verify", "public", _signup_open(_limited(signup_verify, "signup"))),
    # FR-AUTH-29: the owner's sign-up routes.
    Route("GET", "/api/auth/signups", "session", _owner_only(signups_list)),
    Route("POST", "/api/auth/signups/{user_id}/approve", "recent_session", _owner_only(_action("approve"))),
    Route("POST", "/api/auth/signups/{user_id}/reject", "recent_session", _owner_only(_action("reject"))),
    Route("POST", "/api/auth/signups/{user_id}/disable", "recent_session", _owner_only(_action("disable"))),
)
