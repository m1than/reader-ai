"""Helpers for the sign-up tests (SRV-AUTH FR-AUTH-23..30). Modules under test are imported in functions."""

from __future__ import annotations

from typing import Any

import httpx
from auth_helpers import execute, scalar, unb64url
from softauthn import SoftAuthenticator

SESSION, CEREMONY = "__Host-reader_session", "__Host-reader_ceremony"


async def signup_options(
    c: httpx.AsyncClient, handle: object, display_name: object = "Test Person"
) -> httpx.Response:
    return await c.post("/api/auth/signup/options", json={"handle": handle, "display_name": display_name})


async def signup(
    c: httpx.AsyncClient,
    handle: str,
    auth: SoftAuthenticator | None = None,
    display_name: str = "Test Person",
    extra: dict[str, Any] | None = None,
    **mutation: Any,
) -> tuple[httpx.Response, SoftAuthenticator, dict[str, Any]]:
    """Options then verify; returns the verify response, the authenticator and the options."""
    auth = auth or SoftAuthenticator.random()
    options = await signup_options(c, handle, display_name)
    assert options.status_code == 200, options.text
    opts = options.json()
    credential = auth.register(challenge=unb64url(opts["challenge"]), **mutation)
    credential.update(extra or {})
    return await c.post("/api/auth/signup/verify", json=credential), auth, opts


async def signin_status(
    c: httpx.AsyncClient, auth: SoftAuthenticator, user_handle: bytes
) -> httpx.Response:
    options = await c.post("/api/auth/signin/options", json={})
    assert options.status_code == 200, options.text
    challenge = unb64url(options.json()["challenge"])
    assertion = auth.assertion(challenge=challenge, user_handle=user_handle)
    return await c.post("/api/auth/signin/verify", json=assertion)


async def user_id(engine: Any, handle: str) -> Any:
    return await scalar(engine, "SELECT id FROM users WHERE handle = :h", h=handle)


async def count(engine: Any, table: str, where: str = "true", **params: Any) -> int:
    return int(await scalar(engine, f"SELECT count(*) FROM {table} WHERE {where}", **params))


async def insert_pending(engine: Any, handle: str, *, state: str = "pending", age_s: int = 0) -> Any:
    """A sign-up row written directly (setup for caps and collisions), with one credential."""
    import secrets

    from reader.auth.handles import skeleton

    await execute(
        engine,
        "INSERT INTO users (handle, role, webauthn_user_id, display_name, consent_evaluation, source, "
        "state, handle_skeleton, created_at) VALUES (:h, 'learner', :u, :h, false, 'signup', :s, :k, "
        "now() - make_interval(secs => :age))",
        h=handle,
        u=secrets.token_bytes(64),
        s=state,
        k=skeleton(handle),
        age=age_s,
    )
    uid = await user_id(engine, handle)
    await execute(
        engine,
        "INSERT INTO webauthn_credentials (credential_id, owner_id, public_key, sign_count, aaguid, "
        "backed_up) VALUES (:c, :u, :pk, 0, '00000000-0000-0000-0000-000000000000', false)",
        c=secrets.token_bytes(32),
        u=uid,
        pk=b"pk",
    )
    return uid
