"""SRV-AUTH HTTP ceremonies, sessions, CSRF, failure limit and pages, through the ASGI app against Postgres.

@trace FR-AUTH-4 FR-AUTH-5 FR-AUTH-6 FR-AUTH-7 FR-AUTH-8 FR-AUTH-9 FR-AUTH-10 FR-AUTH-11 FR-AUTH-12 FR-AUTH-13
@trace FR-AUTH-14 FR-AUTH-18 INV-AUTH-1 INV-AUTH-2 INV-AUTH-3 INV-AUTH-5 INV-AUTH-6 INV-AUTH-7 INV-AUTH-9
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from auth_helpers import (
    ORIGIN,
    bootstrap,
    bootstrap_token,
    client,
    execute,
    owner_with_passkey,
    ready_engine,
    scalar,
    set_cookies,
    settings_for,
    signin,
    unb64url,
)
from conftest import Pg
from softauthn import UP, SoftAuthenticator

pytestmark = pytest.mark.anyio

SESSION, CEREMONY = "__Host-reader_session", "__Host-reader_ceremony"


async def _setup(pg: Pg, tmp_path: Path) -> tuple[Any, Any]:
    settings = settings_for(tmp_path)
    return settings, await ready_engine(pg, settings)


def _cookie_attrs(header: str) -> dict[str, str]:
    parts = [p.strip() for p in header.split(";")]
    attrs = {p.split("=", 1)[0].lower(): (p.split("=", 1)[1] if "=" in p else "") for p in parts[1:]}
    attrs["__name"] = parts[0].split("=", 1)[0]
    attrs["__value"] = parts[0].split("=", 1)[1]
    return attrs


def _assert_host_cookie(header: str) -> dict[str, str]:
    a = _cookie_attrs(header)
    assert a["__name"].startswith("__Host-")
    assert "secure" in a and "httponly" in a
    assert a.get("samesite", "").lower() == "lax"
    assert a.get("path") == "/"
    assert "domain" not in a
    return a


# FR-AUTH-4 (store side of bootstrap-link) -----------------------------------------------------------------


async def test_bootstrap_token_refusals_and_revocation(pg: Pg, tmp_path: Path) -> None:
    from reader.auth.store import BootstrapRefused, create_bootstrap_token

    settings, engine = await _setup(pg, tmp_path)
    with pytest.raises(BootstrapRefused) as unknown:
        await create_bootstrap_token(engine, settings, "nobody")
    assert unknown.value.code == "unknown_handle"
    first = await create_bootstrap_token(engine, settings, None)
    second = await create_bootstrap_token(engine, settings, None)
    assert first != second and len(unb64url(second)) == 32
    stored = await scalar(
        engine,
        "SELECT count(*) FROM bootstrap_tokens WHERE token_sha256 = :h",
        h=hashlib.sha256(second.encode()).digest(),
    )
    assert stored == 1
    assert await scalar(engine, "SELECT count(*) FROM bootstrap_tokens WHERE revoked_at IS NULL") == 1
    assert (
        await scalar(
            engine,
            "SELECT bool_and(expires_at BETWEEN now() + interval '899 s' AND now() + "
            "interval '901 s') FROM bootstrap_tokens",
        )
        is True
    )
    async with client(settings, engine) as c:
        await owner_with_passkey(c, engine, settings)
    with pytest.raises(BootstrapRefused) as has:
        await create_bootstrap_token(engine, settings, None)
    assert has.value.code == "owner_has_credential"
    await execute(engine, "UPDATE users SET disabled_at = now() WHERE handle = 'live-check'")
    with pytest.raises(BootstrapRefused) as disabled:
        await create_bootstrap_token(engine, settings, "live-check")
    assert disabled.value.code == "user_disabled"
    await engine.dispose()


# FR-AUTH-5, FR-AUTH-8, FR-AUTH-9, INV-AUTH-3, INV-AUTH-5 --------------------------------------------------


async def test_bootstrap_registers_owner_and_starts_session(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    token = await bootstrap_token(engine, settings)
    async with client(settings, engine) as c:
        options = await c.post("/api/auth/bootstrap/options", json={"token": token})
        assert options.status_code == 200
        opts = options.json()
        assert opts["rp"]["id"] == "localhost"
        assert opts["authenticatorSelection"]["residentKey"] == "required"
        assert opts["authenticatorSelection"]["userVerification"] == "required"
        assert opts["attestation"] == "none"
        assert [p["alg"] for p in opts["pubKeyCredParams"]] == [-7, -8, -257]
        assert len(unb64url(opts["challenge"])) == 32
        assert len(unb64url(opts["user"]["id"])) == 64
        ceremony = [h for h in set_cookies(options) if h.startswith(CEREMONY)]
        assert len(ceremony) == 1 and _assert_host_cookie(ceremony[0])["max-age"] == "300"
        auth = SoftAuthenticator.random()
        verify = await c.post(
            "/api/auth/bootstrap/verify", json=auth.register(challenge=unb64url(opts["challenge"]))
        )
        assert verify.status_code == 200, verify.text
        session = [h for h in set_cookies(verify) if h.startswith(SESSION)]
        assert len(session) == 1
        attrs = _assert_host_cookie(session[0])
        assert attrs["max-age"] == "1209600"
        me = await c.get("/api/me")
        assert me.status_code == 200
        body = me.json()
        assert body["handle"] == "ivan" and body["role"] == "owner"
        assert body["consent"] == {"evaluation": True, "tuning": False}
        assert set(body) == {"id", "handle", "role", "display_name", "consent"}
        stored = await scalar(
            engine,
            "SELECT count(*) FROM sessions WHERE id_sha256 = :h",
            h=hashlib.sha256(attrs["__value"].encode()).digest(),
        )
        assert stored == 1
        assert await scalar(engine, "SELECT count(*) FROM webauthn_credentials") == 1
        assert await scalar(engine, "SELECT bool_and(consumed_at IS NOT NULL) FROM bootstrap_tokens") is True
    await engine.dispose()


async def test_bootstrap_with_invalid_token(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        r = await c.post("/api/auth/bootstrap/options", json={"token": "A" * 43})
        assert r.status_code == 400 and r.json() == {"error": "bootstrap_failed"}
        assert not [h for h in set_cookies(r) if h.startswith(SESSION)]
    assert await scalar(engine, "SELECT count(*) FROM auth_failures WHERE reason = 'token_invalid'") == 1
    await engine.dispose()


async def test_bootstrap_with_unencodable_or_non_string_token(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        for body in [b'{"token":"\\ud800"}', b'{"token":12}', b'{"token":null}', b"[]"]:
            r = await c.post(
                "/api/auth/bootstrap/options", content=body, headers={"Content-Type": "application/json"}
            )
            assert r.status_code == 400, body
            assert r.json() == {"error": "bootstrap_failed"}
    assert await scalar(engine, "SELECT count(*) FROM auth_failures WHERE reason = 'token_invalid'") == 4
    await engine.dispose()


DEEP_OVER_CAP = b"[" * 200_000 + b"]" * 200_000  # 400 KB, over the 64 KiB bound
DEEP_UNDER_CAP = b"[" * 30_000 + b"]" * 30_000  # 60 KB, under the bound, deeper than the recursion limit
JSON_HEADERS = {"Content-Type": "application/json"}


async def test_oversized_or_too_deep_bodies_are_unparseable(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        for body in (DEEP_OVER_CAP, DEEP_UNDER_CAP):
            r = await c.post("/api/auth/bootstrap/options", content=body, headers=JSON_HEADERS)
            assert r.status_code == 400
            assert r.json() == {"error": "bootstrap_failed"}
            await c.post("/api/auth/signin/options", json={})  # a live ceremony, so the body is what fails
            r = await c.post("/api/auth/signin/verify", content=body, headers=JSON_HEADERS)
            assert r.status_code == 400
            assert r.json() == {"error": "signin_failed"}
    assert await scalar(engine, "SELECT count(*) FROM auth_failures WHERE reason = 'token_invalid'") == 2
    assert await scalar(engine, "SELECT count(*) FROM auth_failures WHERE reason = 'malformed'") == 2
    await engine.dispose()


# INV-AUTH-7----------------------------------------------------------------------------------------------


async def test_token_revoked_after_options_fails(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    token = await bootstrap_token(engine, settings)
    async with client(settings, engine) as c:
        options = await c.post("/api/auth/bootstrap/options", json={"token": token})
        await bootstrap_token(engine, settings)  # revokes the first
        auth = SoftAuthenticator.random()
        r = await c.post(
            "/api/auth/bootstrap/verify", json=auth.register(challenge=unb64url(options.json()["challenge"]))
        )
        assert r.status_code == 400 and r.json() == {"error": "bootstrap_failed"}
    assert await scalar(engine, "SELECT count(*) FROM webauthn_credentials") == 0
    await engine.dispose()


async def test_token_expiring_between_options_and_verify_fails(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    token = await bootstrap_token(engine, settings)
    async with client(settings, engine) as c:
        options = await c.post("/api/auth/bootstrap/options", json={"token": token})
        await execute(engine, "UPDATE bootstrap_tokens SET expires_at = now() - interval '1 s'")
        auth = SoftAuthenticator.random()
        r = await c.post(
            "/api/auth/bootstrap/verify", json=auth.register(challenge=unb64url(options.json()["challenge"]))
        )
        assert r.status_code == 400
    assert await scalar(engine, "SELECT count(*) FROM webauthn_credentials") == 0
    await engine.dispose()


async def test_concurrent_bootstraps_leave_exactly_one_credential(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    tokens: list[str] = []

    async def link() -> None:
        tokens.append(await bootstrap_token(engine, settings))

    async with anyio.create_task_group() as tg:
        tg.start_soon(link)
        tg.start_soon(link)
    # Each token gets its own options, then both verifies run concurrently. The later link revoked the
    # earlier token, so its options may already fail; the invariant is about credentials.
    results: list[int] = []

    async def attempt(token: str) -> None:
        async with client(settings, engine) as c:
            options = await c.post("/api/auth/bootstrap/options", json={"token": token})
            if options.status_code != 200:
                results.append(options.status_code)
                return
            auth = SoftAuthenticator.random()
            r = await c.post(
                "/api/auth/bootstrap/verify",
                json=auth.register(challenge=unb64url(options.json()["challenge"])),
            )
            results.append(r.status_code)

    async with anyio.create_task_group() as tg:
        for t in tokens:
            tg.start_soon(attempt, t)
    # The later link revoked the earlier token, so exactly one ceremony can succeed.
    assert results.count(200) == 1
    assert await scalar(engine, "SELECT count(*) FROM webauthn_credentials") == 1
    # One token, two ceremonies, two concurrent verifies: exactly one credential.
    await execute(engine, "DELETE FROM webauthn_credentials")
    t1 = await bootstrap_token(engine, settings)
    async with client(settings, engine) as c1, client(settings, engine) as c2:
        o1 = await c1.post("/api/auth/bootstrap/options", json={"token": t1})
        assert o1.status_code == 200
        o2 = await c2.post("/api/auth/bootstrap/options", json={"token": t1})
        assert o2.status_code == 200
        a1, a2 = SoftAuthenticator.random(), SoftAuthenticator.random()
        outcomes: list[int] = []

        async def verify(c: httpx.AsyncClient, a: SoftAuthenticator, o: httpx.Response) -> None:
            r = await c.post(
                "/api/auth/bootstrap/verify", json=a.register(challenge=unb64url(o.json()["challenge"]))
            )
            outcomes.append(r.status_code)

        async with anyio.create_task_group() as tg:
            tg.start_soon(verify, c1, a1, o1)
            tg.start_soon(verify, c2, a2, o2)
    assert sorted(outcomes) == [200, 400]
    assert await scalar(engine, "SELECT count(*) FROM webauthn_credentials") == 1
    await engine.dispose()


async def test_bad_response_then_good_one_with_same_token(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    token = await bootstrap_token(engine, settings)
    async with client(settings, engine) as c:
        options = await c.post("/api/auth/bootstrap/options", json={"token": token})
        challenge = unb64url(options.json()["challenge"])
        auth = SoftAuthenticator.random()
        bad = await c.post("/api/auth/bootstrap/verify", json=auth.register(challenge=challenge, flags=UP))
        assert bad.status_code == 400
        good = await c.post("/api/auth/bootstrap/verify", json=auth.register(challenge=challenge))
        assert good.status_code == 200, good.text
    assert await scalar(engine, "SELECT count(*) FROM auth_failures") == 1
    assert await scalar(engine, "SELECT count(*) FROM webauthn_credentials") == 1
    await engine.dispose()


async def test_bootstrap_for_user_disabled_after_token(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    token = await bootstrap_token(engine, settings, "live-check")
    async with client(settings, engine) as c:
        options = await c.post("/api/auth/bootstrap/options", json={"token": token})
        assert options.status_code == 200
        await execute(engine, "UPDATE users SET disabled_at = now() WHERE handle = 'live-check'")
        auth = SoftAuthenticator.random()
        r = await c.post(
            "/api/auth/bootstrap/verify", json=auth.register(challenge=unb64url(options.json()["challenge"]))
        )
        assert r.status_code == 400
    assert await scalar(engine, "SELECT count(*) FROM auth_failures WHERE reason = 'user_disabled'") == 1
    assert await scalar(engine, "SELECT count(*) FROM webauthn_credentials") == 0
    await engine.dispose()


# FR-AUTH-6 -----------------------------------------------------------------------------------------------


async def test_add_passkey_requires_recent_session_and_rotates(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        first, _ = await owner_with_passkey(c, engine, settings)
        old_cookie = c.cookies.get(SESSION)
        options = await c.post("/api/auth/register/options", json={})
        assert options.status_code == 200
        opts = options.json()
        assert [unb64url(x["id"]) for x in opts["excludeCredentials"]] == [first.credential_id]
        second = SoftAuthenticator.random()
        challenge = unb64url(opts["challenge"])
        bad = await c.post("/api/auth/register/verify", json=second.register(challenge=challenge, flags=UP))
        assert bad.status_code == 400
        ok = await c.post("/api/auth/register/verify", json=second.register(challenge=challenge))
        assert ok.status_code == 200, ok.text
        assert c.cookies.get(SESSION) != old_cookie
        assert await scalar(engine, "SELECT count(*) FROM webauthn_credentials") == 2
        assert await scalar(engine, "SELECT count(*) FROM auth_failures") == 1
        stale = await c.get("/api/me", cookies={SESSION: old_cookie})
        assert stale.status_code == 401
        await execute(engine, "UPDATE sessions SET created_at = now() - interval '601 s'")
        late = await c.post("/api/auth/register/options", json={})
        assert late.status_code == 401 and late.json() == {"error": "reauth_required"}
    async with client(settings, engine) as anonymous:
        r = await anonymous.post("/api/auth/register/options", json={})
        assert r.status_code == 401
    await engine.dispose()


async def test_registering_an_existing_credential_id_is_refused(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        auth, _ = await owner_with_passkey(c, engine, settings)
        options = await c.post("/api/auth/register/options", json={})
        r = await c.post(
            "/api/auth/register/verify", json=auth.register(challenge=unb64url(options.json()["challenge"]))
        )
        assert r.status_code == 400
    assert (
        await scalar(engine, "SELECT count(*) FROM auth_failures WHERE reason = 'credential_id_taken'") == 1
    )
    await engine.dispose()


# FR-AUTH-7, INV-AUTH-1, INV-AUTH-2, INV-AUTH-6 ------------------------------------------------------------


async def test_signin_success_and_counter(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        auth, handle = await owner_with_passkey(c, engine, settings)
        await c.post("/api/auth/signout")
    async with client(settings, engine) as c:
        options = await c.post("/api/auth/signin/options", json={})
        opts = options.json()
        assert opts["rpId"] == "localhost" and opts["userVerification"] == "required"
        assert opts.get("allowCredentials", []) == []
        r = await c.post(
            "/api/auth/signin/verify",
            json=auth.assertion(challenge=unb64url(opts["challenge"]), user_handle=handle, sign_count=5),
        )
        assert r.status_code == 200, r.text
        assert (await c.get("/api/me")).json()["role"] == "owner"
    assert await scalar(engine, "SELECT sign_count FROM webauthn_credentials") == 5
    assert await scalar(engine, "SELECT last_used_at IS NOT NULL FROM webauthn_credentials") is True
    async with client(settings, engine) as c:
        r = await signin(c, auth, handle, sign_count=4)
        assert r.status_code == 400 and r.json() == {"error": "signin_failed"}
    assert await scalar(engine, "SELECT sign_count FROM webauthn_credentials") == 5
    await engine.dispose()


@pytest.mark.parametrize(
    ("stored", "received", "ok"),
    [(0, 0, True), (0, 3, True), (5, 6, True), (5, 5, False), (5, 0, False)],
)
async def test_finish_signin_updates_counter_conditionally(
    pg: Pg, tmp_path: Path, stored: int, received: int, ok: bool
) -> None:
    from reader.auth.store import finish_signin

    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        auth, _ = await owner_with_passkey(c, engine, settings)
    await execute(engine, "UPDATE webauthn_credentials SET sign_count = :n", n=stored)
    async with engine.begin() as conn:
        assert await finish_signin(conn, auth.credential_id, received, True) is ok
    assert await scalar(engine, "SELECT sign_count FROM webauthn_credentials") == (received if ok else stored)
    await engine.dispose()


async def test_counter_race_has_one_winner(pg: Pg, tmp_path: Path) -> None:
    """Two ceremonies, stored count 5, both assertions carry 6: one session, one `counter_regressed`."""
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        auth, handle = await owner_with_passkey(c, engine, settings)
    await execute(engine, "UPDATE webauthn_credentials SET sign_count = 5")
    async with client(settings, engine) as c1, client(settings, engine) as c2:
        challenges = []
        for c in (c1, c2):
            options = await c.post("/api/auth/signin/options", json={})
            challenges.append(unb64url(options.json()["challenge"]))
        statuses: list[int] = []

        async def verify(c: httpx.AsyncClient, challenge: bytes) -> None:
            r = await c.post(
                "/api/auth/signin/verify",
                json=auth.assertion(challenge=challenge, user_handle=handle, sign_count=6),
            )
            statuses.append(r.status_code)

        async with anyio.create_task_group() as tg:
            tg.start_soon(verify, c1, challenges[0])
            tg.start_soon(verify, c2, challenges[1])
    assert sorted(statuses) == [200, 400]
    assert await scalar(engine, "SELECT sign_count FROM webauthn_credentials") == 6
    assert await scalar(engine, "SELECT count(*) FROM auth_failures WHERE reason = 'counter_regressed'") == 1
    await engine.dispose()


async def test_late_counter_regression_writes_a_failure(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import reader.auth.store as store

    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        auth, handle = await owner_with_passkey(c, engine, settings)

    async def lost_race(*_: Any) -> bool:
        return False

    monkeypatch.setattr(store, "finish_signin", lost_race)
    async with client(settings, engine) as c:
        r = await signin(c, auth, handle)
        assert r.status_code == 400
        assert c.cookies.get(SESSION) is None
    assert await scalar(engine, "SELECT count(*) FROM auth_failures WHERE reason = 'counter_regressed'") == 1
    await engine.dispose()


async def test_signin_failures(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        auth, handle = await owner_with_passkey(c, engine, settings)
    sessions_before = await scalar(engine, "SELECT count(*) FROM sessions")
    async with client(settings, engine) as c:
        for r in [
            await signin(c, SoftAuthenticator.random(), handle),  # unknown credential
            await signin(c, auth, b"\x07" * 64),  # other user handle
            await signin(c, auth, handle, flags=UP),  # no UV
        ]:
            assert r.status_code == 400 and r.json() == {"error": "signin_failed"}
            assert not [h for h in set_cookies(r) if h.startswith(SESSION)]
        no_ceremony = await c.post(
            "/api/auth/signin/verify", json=auth.assertion(user_handle=handle), cookies={CEREMONY: "nope"}
        )
        assert no_ceremony.status_code == 400
    reasons = {r async for r in _reasons(engine)}
    assert {
        "unknown_credential",
        "user_handle_mismatch",
        "user_verified_missing",
        "ceremony_invalid",
    } <= reasons
    assert await scalar(engine, "SELECT count(*) FROM sessions") == sessions_before
    await engine.dispose()


async def _reasons(engine: Any) -> Any:
    from sqlalchemy import text

    async with engine.connect() as conn:
        for row in await conn.execute(text("SELECT reason FROM auth_failures")):
            yield row[0]


async def test_challenge_is_single_use(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        auth, handle = await owner_with_passkey(c, engine, settings)
    async with client(settings, engine) as c:
        options = await c.post("/api/auth/signin/options", json={})
        challenge = unb64url(options.json()["challenge"])
        statuses: list[int] = []

        async def verify(count: int) -> None:
            r = await c.post(
                "/api/auth/signin/verify",
                json=auth.assertion(challenge=challenge, user_handle=handle, sign_count=count),
            )
            statuses.append(r.status_code)

        async with anyio.create_task_group() as tg:
            tg.start_soon(verify, 1)
            tg.start_soon(verify, 2)
        assert statuses.count(200) == 1
        again = await c.post(
            "/api/auth/signin/verify",
            json=auth.assertion(challenge=challenge, user_handle=handle, sign_count=9),
        )
        assert again.status_code == 400
    await engine.dispose()


async def test_failed_signin_consumes_the_challenge(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        auth, handle = await owner_with_passkey(c, engine, settings)
    async with client(settings, engine) as c:
        options = await c.post("/api/auth/signin/options", json={})
        challenge = unb64url(options.json()["challenge"])
        bad = await c.post(
            "/api/auth/signin/verify", json=auth.assertion(challenge=challenge, user_handle=handle, flags=UP)
        )
        assert bad.status_code == 400
        good = await c.post(
            "/api/auth/signin/verify", json=auth.assertion(challenge=challenge, user_handle=handle)
        )
        assert good.status_code == 400
    await engine.dispose()


async def test_expired_and_other_kind_ceremonies_never_verify(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        auth, handle = await owner_with_passkey(c, engine, settings)
        reg = await c.post("/api/auth/register/options", json={})  # a register ceremony in this client
        r = await c.post(
            "/api/auth/signin/verify",
            json=auth.assertion(challenge=unb64url(reg.json()["challenge"]), user_handle=handle),
        )
        assert r.status_code == 400
    async with client(settings, engine) as c:
        options = await c.post("/api/auth/signin/options", json={})
        await execute(engine, "UPDATE auth_challenges SET expires_at = now() - interval '1 s'")
        r = await c.post(
            "/api/auth/signin/verify",
            json=auth.assertion(challenge=unb64url(options.json()["challenge"]), user_handle=handle),
        )
        assert r.status_code == 400
    await engine.dispose()


async def test_register_ceremony_of_one_user_never_serves_another(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as owner:
        await owner_with_passkey(owner, engine, settings)
        options = await owner.post("/api/auth/register/options", json={})
        ceremony = owner.cookies.get(CEREMONY)
    async with client(settings, engine) as learner:
        response, _ = await bootstrap(
            learner, await bootstrap_token(engine, settings, "live-check"), SoftAuthenticator.random()
        )
        assert response.status_code == 200
        learner.cookies.set(CEREMONY, ceremony, domain="localhost")
        r = await learner.post(
            "/api/auth/register/verify",
            json=SoftAuthenticator.random().register(challenge=unb64url(options.json()["challenge"])),
        )
        assert r.status_code == 400
    owner_creds = await scalar(
        engine,
        "SELECT count(*) FROM webauthn_credentials w JOIN users u ON u.id = w.owner_id "
        "WHERE u.handle = 'live-check'",
    )
    assert owner_creds == 1
    await engine.dispose()


# FR-AUTH-9, FR-AUTH-10, INV-AUTH-9 -----------------------------------------------------------------------


async def test_signout_and_me(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        anon = await c.get("/api/me")
        assert anon.status_code == 401 and anon.json() == {"error": "unauthenticated"}
        assert (await c.post("/api/auth/signout")).status_code == 204
        await owner_with_passkey(c, engine, settings)
        cookie = c.cookies.get(SESSION)
        out = await c.post("/api/auth/signout")
        assert out.status_code == 204
        cleared = [h for h in set_cookies(out) if h.startswith(SESSION)]
        assert len(cleared) == 1 and _assert_host_cookie(cleared[0])["max-age"] == "0"
        assert (await c.get("/api/me", cookies={SESSION: cookie})).status_code == 401
    await engine.dispose()


async def test_expired_session_and_disabled_user_do_not_authenticate(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        await bootstrap(c, await bootstrap_token(engine, settings, "live-check"), SoftAuthenticator.random())
        assert (await c.get("/api/me")).status_code == 200
        await execute(engine, "UPDATE users SET disabled_at = now() WHERE handle = 'live-check'")
        assert (await c.get("/api/me")).status_code == 401
        await execute(engine, "UPDATE users SET disabled_at = NULL")
        assert (await c.get("/api/me")).status_code == 200
        await execute(engine, "UPDATE sessions SET expires_at = now() - interval '1 s'")
        assert (await c.get("/api/me")).status_code == 401
    await engine.dispose()


async def test_disabled_user_cannot_sign_in(pg: Pg, tmp_path: Path) -> None:
    from reader.auth.store import prepare

    settings, engine = await _setup(pg, tmp_path)
    auth = SoftAuthenticator.random()
    async with client(settings, engine) as c:
        _, opts = await bootstrap(c, await bootstrap_token(engine, settings, "live-check"), auth)
        handle = unb64url(opts["user"]["id"])
    await prepare(engine, settings_for(tmp_path / "x", users=(("ivan", "owner"),)))
    async with client(settings, engine) as c:
        r = await signin(c, auth, handle)
        assert r.status_code == 400
    assert await scalar(engine, "SELECT count(*) FROM auth_failures WHERE reason = 'user_disabled'") == 1
    await engine.dispose()


def test_routers_are_a_declared_list() -> None:
    from reader.api.app import ROUTERS

    # SRV-LESSON, SRV-KERNEL, SRV-INGEST and SRV-SEARCH add their routers (paired changes, FR-AUTH-10 notes).
    assert list(ROUTERS) == [
        "reader.api.routes",
        "reader.auth.routes",
        "reader.lessons.routes",
        "reader.kernels.routes",
        "reader.ingest.routes",
        "reader.search.routes",
    ]


def test_route_table_is_declared_as_data() -> None:
    from reader.api.app import ROUTES

    public = {(r.method, r.path) for r in ROUTES if r.auth == "public"}
    assert public == {
        ("GET", "/api/healthz"),
        ("POST", "/api/auth/bootstrap/options"),
        ("POST", "/api/auth/bootstrap/verify"),
        ("POST", "/api/auth/signin/options"),
        ("POST", "/api/auth/signin/verify"),
        ("POST", "/api/auth/signout"),
        ("GET", "/api/auth/bootstrap"),
        ("GET", "/api/auth/signin"),
        ("GET", "/api/auth/static/passkey.js"),
    }
    by_path = {(r.method, r.path): r.auth for r in ROUTES}
    assert by_path[("GET", "/api/me")] == "session"
    assert by_path[("POST", "/api/auth/register/options")] == "recent_session"
    assert by_path[("POST", "/api/auth/register/verify")] == "recent_session"


async def test_get_routes_write_nothing(pg: Pg, tmp_path: Path) -> None:
    from reader.api.app import ROUTES

    settings, engine = await _setup(pg, tmp_path)
    count_sql = (
        "SELECT (SELECT count(*) FROM users) + (SELECT count(*) FROM sessions) + (SELECT count(*) FROM "
        "auth_challenges) + (SELECT count(*) FROM auth_failures) + (SELECT count(*) FROM bootstrap_tokens)"
    )
    async with client(settings, engine) as c:
        await owner_with_passkey(c, engine, settings)
        before = await scalar(engine, count_sql)
        for route in ROUTES:
            if route.method == "GET":
                await c.get(route.path)
        await c.get("/api/me", cookies={SESSION: "garbage"})
        assert await scalar(engine, count_sql) == before
    await engine.dispose()


# FR-AUTH-11 ----------------------------------------------------------------------------------------------


async def test_failure_limit(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    await execute(
        engine,
        "INSERT INTO auth_failures (reason, at) SELECT 'malformed', now() - interval '10 min' "
        "FROM generate_series(1, 100)",
    )
    async with client(settings, engine) as c:
        for path, body in [
            ("/api/auth/signin/options", {}),
            ("/api/auth/signin/verify", {}),
            ("/api/auth/bootstrap/options", {"token": "x"}),
            ("/api/auth/bootstrap/verify", {}),
        ]:
            r = await c.post(path, json=body)
            assert r.status_code == 429, path
            retry = int(r.headers["retry-after"])
            assert 3000 <= retry <= 3000 + 5
    assert await scalar(engine, "SELECT count(*) FROM auth_failures") == 100
    await execute(
        engine,
        "UPDATE auth_failures SET at = now() - interval '2 hour' WHERE id IN "
        "(SELECT id FROM auth_failures LIMIT 1)",
    )
    async with client(settings, engine) as c:
        assert (await c.post("/api/auth/signin/options", json={})).status_code == 200
    await engine.dispose()


async def test_failure_limit_is_reached_through_the_routes(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        for _ in range(100):
            r = await c.post("/api/auth/signin/verify", json={})  # no ceremony: ceremony_invalid
            assert r.status_code == 400
        assert await scalar(engine, "SELECT count(*) FROM auth_failures") == 100
        blocked = await c.post("/api/auth/signin/verify", json={})
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) > 3500
        assert (await c.post("/api/auth/signin/options", json={})).status_code == 429
    assert await scalar(engine, "SELECT count(*) FROM auth_failures") == 100
    await engine.dispose()


# FR-AUTH-12----------------------------------------------------------------------------------------------


async def test_csrf_origin_and_content_type(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        no_origin = await c.post("/api/auth/signin/options", json={}, headers={"Origin": ""})
        assert no_origin.status_code == 403 and no_origin.json() == {"error": "origin"}
        for bad in ["https://evil.example", "http://localhost", "https://localhost:8443", "null"]:
            r = await c.post("/api/auth/signin/options", json={}, headers={"Origin": bad})
            assert r.status_code == 403, bad
        r = await c.post("/api/auth/signin/options", content=b"{}", headers={"Content-Type": "text/plain"})
        assert r.status_code == 415
        r = await c.post(
            "/api/auth/signin/options",
            content=b"{}",
            headers={"Content-Type": "Application/JSON; charset=utf-8"},
        )
        assert r.status_code == 200
        r = await c.post("/api/auth/signout", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        assert (await c.post("/api/auth/signout")).status_code == 204
        ws = await c.get(
            "/api/anything",
            headers={"Origin": "https://evil.example", "Connection": "Upgrade", "Upgrade": "websocket"},
        )
        assert ws.status_code == 403
    assert await scalar(engine, "SELECT count(*) FROM auth_challenges") == 1
    await engine.dispose()


async def _through_gate(scope: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """One ASGI scope through the CSRF gate: the messages it sent, and whether the app was reached."""
    from reader.api.app import CsrfGate

    reached: list[bool] = []
    sent: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        reached.append(True)

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"} if scope["type"] == "websocket" else {"type": "http.request"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await CsrfGate(app, ORIGIN)(scope, receive, send)
    return sent, bool(reached)


def _scope(kind: str, origins: list[str], method: str = "GET") -> dict[str, Any]:
    scope: dict[str, Any] = {
        "type": kind,
        "path": "/api/kernels/ws",
        "headers": [(b"origin", o.encode()) for o in origins],
    }
    if kind == "http":
        scope["method"] = method
    return scope


async def test_websocket_scope_needs_the_exact_origin() -> None:
    for origins in [[], ["https://evil.example"], [ORIGIN, ORIGIN], ["null"]]:
        sent, reached = await _through_gate(_scope("websocket", origins))
        assert not reached, origins
        assert sent == [{"type": "websocket.close", "code": 1008}]
    sent, reached = await _through_gate(_scope("websocket", [ORIGIN]))
    assert reached and sent == []


async def test_more_than_one_origin_header_is_refused() -> None:
    evil = "https://evil.example"
    for origins in ([ORIGIN, ORIGIN], [evil, ORIGIN], [ORIGIN, evil]):
        sent, reached = await _through_gate(_scope("http", origins, method="POST"))
        assert not reached, origins
        assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 403
    _, reached = await _through_gate(_scope("http", [ORIGIN], method="POST"))
    assert reached


# FR-AUTH-13, FR-AUTH-14 ----------------------------------------------------------------------------------


async def test_signup_routes_are_404(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        for path in ["/api/auth/signup/options", "/api/auth/signup/verify"]:
            assert (await c.post(path, json={})).status_code == 404
        assert (await c.post("/api/auth/nothing", json={})).status_code == 404
    await engine.dispose()


async def test_pages_use_one_same_origin_script(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        for page in ["/api/auth/bootstrap", "/api/auth/signin"]:
            r = await c.get(page)
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
            scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", r.text, re.S | re.I)
            assert scripts == [""], "exactly one script element, without inline code"
            assert re.search(r'<script\b[^>]*src="/api/auth/static/passkey\.js"', r.text)
            assert "on" + "click=" not in r.text.lower()
        js = await c.get("/api/auth/static/passkey.js")
        assert js.status_code == 200 and "javascript" in js.headers["content-type"]
        for needle in [
            "history.replaceState",
            "parseCreationOptionsFromJSON",
            "parseRequestOptionsFromJSON",
            "toJSON()",
            "location.hash",
        ]:
            assert needle in js.text, needle
    await engine.dispose()


# FR-AUTH-18, INV-AUTH-5 ----------------------------------------------------------------------------------


async def test_failure_logs_only_route_and_reason(
    pg: Pg, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        auth, handle = await owner_with_passkey(c, engine, settings)
    caplog.clear()
    caplog.set_level(logging.DEBUG)
    async with client(settings, engine) as c:
        r = await signin(c, auth, handle, flags=UP)
        assert r.status_code == 400
    failures = [rec for rec in caplog.records if "user_verified_missing" in rec.getMessage()]
    assert len(failures) == 1
    message = failures[0].getMessage()
    assert "/api/auth/signin/verify" in message
    from softauthn import b64url

    for secret in [b64url(auth.credential_id), b64url(handle)]:
        assert secret not in message
    await engine.dispose()


async def test_tokens_and_ids_never_reach_logs_or_bodies(
    pg: Pg, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    settings, engine = await _setup(pg, tmp_path)
    token = await bootstrap_token(engine, settings)
    bodies: list[str] = []
    async with client(settings, engine) as c:
        r = await c.post("/api/auth/bootstrap/options", json={"token": token})
        bodies.append(r.text)
        ceremony = c.cookies.get(CEREMONY)
        auth = SoftAuthenticator.random()
        bad = await c.post(
            "/api/auth/bootstrap/verify",
            json=auth.register(challenge=unb64url(r.json()["challenge"]), flags=UP),
        )
        bodies.append(bad.text)
        good = await c.post(
            "/api/auth/bootstrap/verify", json=auth.register(challenge=unb64url(r.json()["challenge"]))
        )
        bodies.append(good.text)
        session = c.cookies.get(SESSION)
        bodies.append((await c.get("/api/me")).text)
        bodies.append((await c.post("/api/auth/bootstrap/options", json={"token": token})).text)
    assert ceremony and session
    text = (
        "\n".join(bodies)
        + "\n"
        + "\n".join(f"{r.getMessage()} {r.args!r} {r.exc_text}" for r in caplog.records)
    )
    for value in [token, ceremony, session]:
        assert value not in text
    for table, column in [
        ("sessions", "id_sha256"),
        ("auth_challenges", "ceremony_sha256"),
        ("bootstrap_tokens", "token_sha256"),
    ]:
        raw = await scalar(
            engine,
            f"SELECT count(*) FROM {table} WHERE position(convert_to(:v, 'UTF8') in {column}) > 0",
            v=token,
        )
        assert raw == 0
    assert (
        await scalar(
            engine,
            "SELECT count(*) FROM auth_challenges WHERE ceremony_sha256 = :h",
            h=hashlib.sha256(ceremony.encode()).digest(),
        )
        == 1
    )
    await engine.dispose()
