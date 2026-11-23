"""SRV-AUTH sign-up against Postgres: switch, schema and sync, options, verify, pending, owner routes, cleanup.

@trace FR-AUTH-23 FR-AUTH-24 FR-AUTH-25 FR-AUTH-26 FR-AUTH-27 FR-AUTH-28 FR-AUTH-29 FR-AUTH-30 FR-AUTH-31
@trace INV-AUTH-10 INV-AUTH-11 INV-AUTH-12 INV-AUTH-13 INV-AUTH-14
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from auth_helpers import (
    ORIGIN,
    client,
    execute,
    owner_with_passkey,
    ready_engine,
    scalar,
    settings_for,
    target,
    unb64url,
)
from conftest import Pg
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from signup_helpers import (
    CEREMONY,
    SESSION,
    count,
    insert_pending,
    signin_status,
    signup,
    signup_options,
    user_id,
)
from softauthn import SoftAuthenticator

pytestmark = pytest.mark.anyio

USERS = (("ivan", "owner"), ("live-check", "learner"))


async def _setup(pg: Pg, tmp_path: Path, **kwargs: Any) -> tuple[Any, Any]:
    settings = settings_for(tmp_path, **kwargs)
    return settings, await ready_engine(pg, settings)


# FR-AUTH-23 / INV-AUTH-14 — the switch -------------------------------------------------------------------

ROW_SQL = (
    "SELECT (SELECT count(*) FROM users) + (SELECT count(*) FROM webauthn_credentials) + "
    "(SELECT count(*) FROM auth_challenges) + (SELECT count(*) FROM signup_attempts)"
)


async def test_off_answers_404_and_writes_nothing(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path, sharing='{ signup: "off" }')
    async with client(settings, engine) as c:
        before = await scalar(engine, ROW_SQL)
        page = await c.get("/api/auth/signup")
        assert (page.status_code, page.json()) == (404, {"error": "not_found"})
        for path in ["/api/auth/signup/options", "/api/auth/signup/verify"]:
            r = await c.post(path, json={"handle": "abcd", "display_name": "x"})
            assert (r.status_code, r.json()) == (404, {"error": "not_found"})
        assert await scalar(engine, ROW_SQL) == before
        signin = await c.get("/api/auth/signin")
        assert 'href="/api/auth/signup"' not in signin.text
    await engine.dispose()


@settings(max_examples=12, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    body=st.one_of(
        st.none(), st.integers(), st.text(max_size=20),
        st.dictionaries(st.text(max_size=12), st.one_of(st.text(max_size=12), st.integers(), st.none()), max_size=4),
    ),
    header=st.sampled_from([{}, {"X-Forwarded-For": "203.0.113.7"}, {"Cookie": f"{CEREMONY}=x"}]),
)
def test_off_mode_property(pg: Pg, tmp_path: Path, body: object, header: dict[str, str]) -> None:
    """INV-AUTH-14: in `off`, no body or header makes a sign-up route write a row."""

    async def run() -> None:
        settings = settings_for(tmp_path, sharing="{ signup: false }")
        from reader.db import make_engine

        engine = make_engine(target(pg))
        from reader.db import migrate

        await migrate(engine)
        from reader.auth.store import prepare

        await prepare(engine, settings)
        async with client(settings, engine) as c:
            before = await scalar(engine, ROW_SQL)
            for path in ["/api/auth/signup/options", "/api/auth/signup/verify"]:
                assert (await c.post(path, json=body, headers=header)).status_code == 404
            assert await scalar(engine, ROW_SQL) == before
        await engine.dispose()

    anyio.run(run)


async def test_open_signin_page_links_to_signup(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    assert settings.signup == "open"
    async with client(settings, engine) as c:
        signin = await c.get("/api/auth/signin")
        assert signin.text.count('href="/api/auth/signup"') == 1
        page = await c.get("/api/auth/signup")
        assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
        assert page.headers["cache-control"] == "no-cache"
        assert 'data-ceremony="signup"' in page.text
    await engine.dispose()


# FR-AUTH-24 / INV-AUTH-13 — schema and sync --------------------------------------------------------------


async def test_migration_backfills_config_rows(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    rows = await scalar(
        engine,
        "SELECT array_agg(handle || ':' || source || ':' || state || ':' || handle_skeleton ORDER BY handle) FROM users",
    )
    assert rows == ["ivan:config:active:lvan", "live-check:config:active:llvecheck"]
    assert await scalar(engine, "SELECT column_default FROM information_schema.columns "
                                "WHERE table_name = 'auth_failures' AND column_name = 'scope'") == "'auth'::text"
    await engine.dispose()


async def test_config_handles_may_share_a_skeleton(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path, users=(("ivan", "owner"), ("mike", "learner"), ("rnike", "learner")))
    assert await count(engine, "users", "handle_skeleton = 'mlke'") == 2
    await engine.dispose()


async def test_signup_user_survives_a_restart_without_it(pg: Pg, tmp_path: Path) -> None:
    from reader.auth.store import prepare

    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        r, _, _ = await signup(c, "lc-aaaa")
        assert r.status_code == 202, r.text
    uid = await user_id(engine, "lc-aaaa")
    await execute(engine, "UPDATE users SET state = 'active' WHERE id = :u", u=uid)
    before = await scalar(engine, "SELECT row(handle, role, source, state, disabled_at)::text FROM users WHERE id = :u", u=uid)
    await prepare(engine, settings_for(tmp_path / "b", users=(("ivan", "owner"),)))
    after = await scalar(engine, "SELECT row(handle, role, source, state, disabled_at)::text FROM users WHERE id = :u", u=uid)
    assert before == after
    await engine.dispose()


async def test_config_handle_supersedes_a_pending_signup(pg: Pg, tmp_path: Path) -> None:
    from reader.auth.store import prepare

    settings, engine = await _setup(pg, tmp_path)
    old = await insert_pending(engine, "maria")
    async with client(settings, engine) as c:
        # An open ceremony for that handle, as if a second person had asked before the restart (written directly:
        # options refuses a handle that is already pending).
        opts = (await signup_options(c, "marta")).json()
        await execute(engine, "UPDATE auth_challenges SET signup_handle = 'maria' WHERE signup_handle = 'marta'")
        superseded = await prepare(engine, settings_for(tmp_path / "b", users=(("ivan", "owner"), ("maria", "learner"))))
        assert superseded == [old]
        assert await count(engine, "users", "id = :u", u=old) == 0
        assert await count(engine, "webauthn_credentials", "owner_id = :u", u=old) == 0
        assert await scalar(engine, "SELECT source || ':' || role FROM users WHERE handle = 'maria'") == "config:learner"
        # The ceremony left to expire cannot create the account: verify re-checks the handle under its lock.
        cred = SoftAuthenticator.random().register(challenge=unb64url(opts["challenge"]))
        r = await c.post("/api/auth/signup/verify", json=cred)
        assert (r.status_code, r.json()) == (409, {"error": "handle_unavailable"})
    await engine.dispose()


@pytest.mark.parametrize("state", ["active", "disabled"])
async def test_config_handle_equal_to_an_active_or_disabled_signup_stops(pg: Pg, tmp_path: Path, state: str) -> None:
    from reader.auth.store import HandleTakenBySignup, prepare

    settings, engine = await _setup(pg, tmp_path)
    await insert_pending(engine, "zora", state="active")
    await insert_pending(engine, "maria", state="active")
    if state == "disabled":
        await execute(engine, "UPDATE users SET disabled_at = now() WHERE handle IN ('zora', 'maria')")
    snapshot = "SELECT array_agg(row(id, handle, source, state, role, disabled_at)::text ORDER BY handle) FROM users"
    before = await scalar(engine, snapshot)
    users = (("ivan", "owner"), ("zora", "learner"), ("maria", "learner"))
    with pytest.raises(HandleTakenBySignup) as raised:
        await prepare(engine, settings_for(tmp_path / "b", users=users))
    assert raised.value.handle == "zora"  # the first in users[] order
    assert await scalar(engine, snapshot) == before
    await engine.dispose()


async def test_skeleton_only_collision_starts(pg: Pg, tmp_path: Path) -> None:
    from reader.auth.store import prepare

    settings, engine = await _setup(pg, tmp_path)
    await insert_pending(engine, "rnike", state="active")
    assert await prepare(engine, settings_for(tmp_path / "b", users=(("ivan", "owner"), ("mike", "learner")))) == []
    assert await count(engine, "users", "handle IN ('mike', 'rnike')") == 2
    await engine.dispose()


async def test_bootstrap_link_refuses_signup_users(pg: Pg, tmp_path: Path) -> None:
    from reader.auth.store import BootstrapRefused, create_bootstrap_token

    settings, engine = await _setup(pg, tmp_path)
    await insert_pending(engine, "lc-bbbb", state="active")
    with pytest.raises(BootstrapRefused) as raised:
        await create_bootstrap_token(engine, settings, "lc-bbbb")
    assert raised.value.code == "signup_user"
    await engine.dispose()


# FR-AUTH-25 — availability against the database ------------------------------------------------------------


async def test_skeleton_is_compared_with_every_row(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path, users=(("ivan", "owner"), ("mike", "learner")))
    await insert_pending(engine, "watt")
    await insert_pending(engine, "olga", state="active")
    await insert_pending(engine, "petr", state="active")
    await execute(engine, "UPDATE users SET disabled_at = now() WHERE handle = 'petr'")
    async with client(settings, engine) as c:
        for taken in ["rnike", "lvan", "i-van", "vvatt", "o1ga", "petr", "ivan", "adm1n", "Ivan", "a--b"]:
            r = await signup_options(c, taken)
            assert (r.status_code, r.json()) == (409, {"error": "handle_unavailable"}), taken
        assert (await signup_options(c, "lc-1a2b")).status_code == 200
    await engine.dispose()


# FR-AUTH-26 — options --------------------------------------------------------------------------------------


async def test_options_shape_and_ceremony(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        r = await signup_options(c, "lc-1a2b", "  Анна  ")
        assert r.status_code == 200
        o = r.json()
        assert o["user"]["name"] == "lc-1a2b" and o["user"]["displayName"] == "Анна"
        assert len(unb64url(o["user"]["id"])) == 64
        assert o.get("excludeCredentials", []) == []
        assert o["authenticatorSelection"]["userVerification"] == "required"
        assert c.cookies.get(CEREMONY)
        row = await scalar(engine, "SELECT signup_handle || ':' || signup_display_name FROM auth_challenges "
                                   "WHERE kind = 'signup'")
        assert row == "lc-1a2b:Анна"
        assert await count(engine, "users", "handle = 'lc-1a2b'") == 0
    await engine.dispose()


async def test_options_refusals_in_order(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        r = await signup_options(c, "lc-aaaa", "")
        assert (r.status_code, r.json()) == (400, {"error": "display_name_invalid"})
        r = await c.post("/api/auth/signup/options", json=["not", "an", "object"])
        assert (r.status_code, r.json()) == (409, {"error": "handle_unavailable"})
        r = await signup_options(c, 7, "")
        assert (r.status_code, r.json()) == (409, {"error": "handle_unavailable"})
        assert await count(engine, "signup_attempts") == 3  # every call that passed the limits added a row
        for i in range(20):
            await insert_pending(engine, f"pend-{i:02d}")
        r = await signup_options(c, "admin")  # the cap comes before the handle check
        assert (r.status_code, r.json()) == (429, {"error": "signup_full"})
        assert "retry-after" not in r.headers
    await engine.dispose()


async def test_global_limit(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    await execute(engine, "INSERT INTO signup_attempts (at) SELECT now() - interval '10 minutes' FROM generate_series(1, 60)")
    async with client(settings, engine) as c:
        r = await signup_options(c, "lc-aaaa")
        assert (r.status_code, r.json()) == (429, {"error": "rate_limited"})
        assert 2995 <= int(r.headers["retry-after"]) <= 3000
        assert await count(engine, "signup_attempts") == 60
    await execute(engine, "DELETE FROM signup_attempts")
    await execute(engine, "INSERT INTO signup_attempts (at) SELECT now() FROM generate_series(1, 59)")
    async with client(settings, engine) as c:
        statuses: list[int] = []

        async def one(i: int) -> None:
            statuses.append((await signup_options(c, f"lc-c{i:03d}")).status_code)

        async with anyio.create_task_group() as tg:
            for i in range(80):
                tg.start_soon(one, i)
        assert statuses.count(200) == 1 and statuses.count(429) == 79
        assert await count(engine, "signup_attempts") == 60
    await engine.dispose()


async def test_failure_scopes_are_separate(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    await execute(engine, "INSERT INTO auth_failures (reason, scope) SELECT 'malformed', 'signup' FROM generate_series(1, 100)")
    async with client(settings, engine) as c:
        assert (await signup_options(c, "lc-aaaa")).status_code == 429
        assert (await c.post("/api/auth/signup/verify", json={})).status_code == 429
        assert (await c.post("/api/auth/signin/options", json={})).status_code == 200
    await execute(engine, "UPDATE auth_failures SET scope = 'auth'")
    async with client(settings, engine) as c:
        assert (await c.post("/api/auth/signin/options", json={})).status_code == 429
        assert (await signup_options(c, "lc-aaaa")).status_code == 200
    await engine.dispose()


# FR-AUTH-27 / INV-AUTH-10 — verify -------------------------------------------------------------------------


async def test_verify_creates_a_pending_learner_and_no_session(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        r, _, _ = await signup(c, "lc-aaaa", display_name="Анна", extra={"role": "owner", "state": "active"})
        assert (r.status_code, r.json()) == (202, {"status": "pending"})
        assert SESSION not in r.headers.get("set-cookie", "")
        assert f"{CEREMONY}=" in r.headers.get("set-cookie", "") and "Max-Age=0" in r.headers.get("set-cookie", "")
    row = await scalar(engine, "SELECT role || ':' || source || ':' || state || ':' || display_name || ':' || "
                               "handle_skeleton || ':' || consent_evaluation FROM users WHERE handle = 'lc-aaaa'")
    assert row == "learner:signup:pending:Анна:lcaaaa:false"
    uid = await user_id(engine, "lc-aaaa")
    assert await count(engine, "webauthn_credentials", "owner_id = :u", u=uid) == 1
    assert await count(engine, "sessions") == 0
    await engine.dispose()


async def test_verify_failure_rolls_back_with_a_signup_failure(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        r, auth, opts = await signup(c, "lc-aaaa", origin="https://evil.example")
        assert (r.status_code, r.json()) == (400, {"error": "signup_failed"})
        assert await count(engine, "users", "source = 'signup'") == 0
        assert await scalar(engine, "SELECT reason || ':' || scope FROM auth_failures") == "origin_mismatch:signup"
        # The challenge stays usable until it expires: a good response with the same ceremony succeeds.
        good = auth.register(challenge=unb64url(opts["challenge"]))
        r2 = await c.post("/api/auth/signup/verify", json=good)
        assert r2.status_code == 202
        # A reused ceremony fails.
        r3 = await c.post("/api/auth/signup/verify", json=good)
        assert (r3.status_code, r3.json()) == (400, {"error": "signup_failed"})
    await engine.dispose()


async def test_concurrent_verifies_for_one_handle(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    prepared = []
    for _ in range(2):
        async with client(settings, engine) as c:
            opts = (await signup_options(c, "lc-same")).json()
            prepared.append((c.cookies.get(CEREMONY), opts))
    results: list[httpx.Response] = []
    async with client(settings, engine) as c:

        async def one(cookie: str, opts: dict[str, Any]) -> None:
            cred = SoftAuthenticator.random().register(challenge=unb64url(opts["challenge"]))
            results.append(await c.post("/api/auth/signup/verify", json=cred, cookies={CEREMONY: cookie}))

        async with anyio.create_task_group() as tg:
            for cookie, opts in prepared:
                tg.start_soon(one, cookie, opts)
    assert sorted(r.status_code for r in results) == [202, 409]
    assert await count(engine, "users", "handle = 'lc-same'") == 1
    await engine.dispose()


async def test_cap_holds_under_concurrent_verifies(pg: Pg, tmp_path: Path) -> None:
    """INV-AUTH-12: 19 pending plus concurrent verifies leave exactly 20."""
    settings, engine = await _setup(pg, tmp_path)
    for i in range(18):
        await insert_pending(engine, f"pend-{i:02d}")
    prepared = []
    for i in range(4):
        async with client(settings, engine) as c:
            opts = (await signup_options(c, f"lc-new{i}")).json()
            prepared.append((c.cookies.get(CEREMONY), opts))
    results: list[int] = []
    async with client(settings, engine) as c:

        async def one(cookie: str, opts: dict[str, Any]) -> None:
            cred = SoftAuthenticator.random().register(challenge=unb64url(opts["challenge"]))
            results.append((await c.post("/api/auth/signup/verify", json=cred, cookies={CEREMONY: cookie})).status_code)

        async with anyio.create_task_group() as tg:
            for cookie, opts in prepared:
                tg.start_soon(one, cookie, opts)
    assert sorted(results) == [202, 202, 429, 429]
    assert await count(engine, "users", "state = 'pending'") == 20
    await engine.dispose()


async def test_concurrent_options_and_verifies_near_full(pg: Pg, tmp_path: Path) -> None:
    """INV-AUTH-12: under any interleaving, pending ≤ 20 and attempts within the window ≤ 60."""
    settings, engine = await _setup(pg, tmp_path)
    for i in range(15):
        await insert_pending(engine, f"pend-{i:02d}")
    await execute(engine, "INSERT INTO signup_attempts (at) SELECT now() FROM generate_series(1, 20)")
    async with client(settings, engine) as c:

        async def flow(i: int) -> None:
            async with client(settings, engine) as own:
                o = await signup_options(own, f"lc-x{i:03d}")
                if o.status_code == 200:
                    cred = SoftAuthenticator.random().register(challenge=unb64url(o.json()["challenge"]))
                    await own.post("/api/auth/signup/verify", json=cred)

        async with anyio.create_task_group() as tg:
            for i in range(40):
                tg.start_soon(flow, i)
        del c
    assert await count(engine, "users", "state = 'pending'") <= 20
    assert await count(engine, "signup_attempts") <= 60
    await engine.dispose()


@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(extra=st.dictionaries(st.sampled_from(["role", "source", "state", "handle", "user", "id"]),
                             st.one_of(st.text(max_size=8), st.integers(), st.none()), max_size=4))
def test_no_body_changes_role_source_or_state(pg: Pg, tmp_path: Path, extra: dict[str, Any]) -> None:
    """INV-AUTH-10: whatever the verify body holds, every sign-up user is a pending learner and no session exists."""

    async def run() -> None:
        settings = settings_for(tmp_path, users=(("ivan", "owner"),))
        engine = await ready_engine(pg, settings)
        async with client(settings, engine) as c:
            import secrets

            await signup(c, f"lc-{secrets.token_hex(4)}", extra=extra)
        assert await count(engine, "users", "source = 'signup' AND NOT (role = 'learner' AND state = 'pending')") == 0
        assert await count(engine, "sessions") == 0
        await engine.dispose()

    anyio.run(run)


# FR-AUTH-28 / INV-AUTH-11 — pending users cannot authenticate ---------------------------------------------


async def test_pending_user_cannot_sign_in(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as c:
        r, auth, opts = await signup(c, "lc-aaaa")
        handle = unb64url(opts["user"]["id"])
        s = await signin_status(c, auth, handle)
        assert (s.status_code, s.json()) == (403, {"error": "pending_approval"})
        assert await count(engine, "sessions") == 0
        assert await count(engine, "auth_failures") == 0
        assert (await c.get("/api/me")).status_code == 401
        # A forged assertion is checked first: signin_failed, not the state.
        options = await c.post("/api/auth/signin/options", json={})
        forged = auth.assertion(challenge=unb64url(options.json()["challenge"]), user_handle=handle, corrupt_signature=True)
        f = await c.post("/api/auth/signin/verify", json=forged)
        assert (f.status_code, f.json()) == (400, {"error": "signin_failed"})
    # A session row written directly for a pending user does not authenticate.
    import hashlib

    uid = await user_id(engine, "lc-aaaa")
    await execute(engine, "INSERT INTO sessions (id_sha256, owner_id, expires_at) VALUES (:h, :u, now() + interval '1 day')",
                  h=hashlib.sha256(b"forged-session").digest(), u=uid)
    async with client(settings, engine) as c:
        assert (await c.get("/api/me", cookies={SESSION: "forged-session"})).status_code == 401
    await engine.dispose()


async def test_approved_learner_has_no_other_kernel(pg: Pg, tmp_path: Path) -> None:
    from reader.kernels.service import KernelService
    from reader.kernels.upstream import Endpoint

    settings, engine = await _setup(pg, tmp_path)
    resolved: list[str] = []

    def resolve(handle: str) -> Endpoint:
        resolved.append(handle)
        return Endpoint("kernel-user-" + handle)

    async def refuse(*_: Any, **__: Any) -> Any:
        raise ConnectionRefusedError

    async with client(settings, engine) as c:
        r, auth, opts = await signup(c, "lc-kern")
        await execute(engine, "UPDATE users SET state = 'active' WHERE handle = 'lc-kern'")
        assert (await signin_status(c, auth, unb64url(opts["user"]["id"]))).status_code == 200
        app = c._transport.app  # type: ignore[attr-defined]
        app.state.kernel_service = KernelService(resolve, http=refuse)
        k = await c.post("/api/kernel/sessions", json={"lesson_id": "w01"})
        assert k.status_code >= 400
        assert set(resolved) <= {"lc-kern"}
    await engine.dispose()


# FR-AUTH-29 — owner routes ---------------------------------------------------------------------------------


async def _owner(c: httpx.AsyncClient, engine: Any, settings: Any) -> None:
    await owner_with_passkey(c, engine, settings)


async def test_owner_routes_list_approve_and_signin(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as visitor:
        r, auth, opts = await signup(visitor, "lc-aaaa", display_name="Анна")
        assert r.status_code == 202
    uid = str(await user_id(engine, "lc-aaaa"))
    async with client(settings, engine) as c:
        await _owner(c, engine, settings)
        listed = (await c.get("/api/auth/signups")).json()
        assert [(p["id"], p["handle"], p["display_name"]) for p in listed["pending"]] == [(uid, "lc-aaaa", "Анна")]
        assert "requested_at" in listed["pending"][0] and listed["accounts"] == []
        a = await c.post(f"/api/auth/signups/{uid}/approve", json={})
        assert (a.status_code, a.json()) == (200, {"status": "active"})
        listed = (await c.get("/api/auth/signups")).json()
        assert listed["pending"] == [] and [x["handle"] for x in listed["accounts"]] == ["lc-aaaa"]
        assert listed["accounts"][0]["disabled"] is False
    async with client(settings, engine) as visitor:
        assert (await signin_status(visitor, auth, unb64url(opts["user"]["id"]))).status_code == 200
        me = (await visitor.get("/api/me")).json()
        assert (me["handle"], me["role"]) == ("lc-aaaa", "learner")
    await engine.dispose()


async def test_owner_routes_refuse_others(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    pending = str(await insert_pending(engine, "lc-pend"))
    async with client(settings, engine) as anon:
        assert (await anon.get("/api/auth/signups")).status_code == 401
        for action in ["approve", "reject", "disable"]:
            assert (await anon.post(f"/api/auth/signups/{pending}/{action}", json={})).status_code == 401
    # A learner with a recent session: 403 owner_only (the role check, not the age check).
    async with client(settings, engine) as learner:
        r, auth, opts = await signup(learner, "lc-lrn")
        await execute(engine, "UPDATE users SET state = 'active' WHERE handle = 'lc-lrn'")
        assert (await signin_status(learner, auth, unb64url(opts["user"]["id"]))).status_code == 200
        assert (await learner.get("/api/auth/signups")).json() == {"error": "owner_only"}
        for action in ["approve", "reject", "disable"]:
            x = await learner.post(f"/api/auth/signups/{pending}/{action}", json={})
            assert (x.status_code, x.json()) == (403, {"error": "owner_only"})
    assert await scalar(engine, "SELECT state FROM users WHERE handle = 'lc-pend'") == "pending"
    await engine.dispose()


async def test_owner_routes_wrong_targets(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    pending = str(await insert_pending(engine, "lc-pend"))
    active = str(await insert_pending(engine, "lc-act", state="active"))
    config_learner = str(await user_id(engine, "live-check"))
    snapshot = "SELECT array_agg(row(handle, state, disabled_at)::text ORDER BY handle) FROM users"
    async with client(settings, engine) as c:
        await _owner(c, engine, settings)
        before = await scalar(engine, snapshot)
        for target_id, action in [
            ("00000000-0000-0000-0000-000000000000", "approve"), ("not-a-uuid", "approve"),
            (config_learner, "approve"), (config_learner, "reject"), (config_learner, "disable"),
            (active, "approve"), (active, "reject"), (pending, "disable"),
        ]:
            r = await c.post(f"/api/auth/signups/{target_id}/{action}", json={})
            assert (r.status_code, r.json()) == (404, {"error": "not_found"}), (target_id, action)
        assert await scalar(engine, snapshot) == before
    await engine.dispose()


async def test_reject_frees_the_handle_and_cancels_open_ceremonies(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as first:
        assert (await signup(first, "lc-twin"))[0].status_code == 202
    uid = str(await user_id(engine, "lc-twin"))
    # Another person's open ceremony for the same handle: written as if issued before the first verify.
    async with client(settings, engine) as second:
        opts = (await signup_options(second, "lc-other")).json()
        await execute(engine, "UPDATE auth_challenges SET signup_handle = 'lc-twin' WHERE signup_handle = 'lc-other'")
        async with client(settings, engine) as owner:
            await _owner(owner, engine, settings)
            assert (await owner.post(f"/api/auth/signups/{uid}/reject", json={})).status_code == 204
        assert await count(engine, "users", "handle = 'lc-twin'") == 0
        assert await count(engine, "webauthn_credentials", "owner_id = :u", u=uid) == 0
        cred = SoftAuthenticator.random().register(challenge=unb64url(opts["challenge"]))
        r = await second.post("/api/auth/signup/verify", json=cred)
        assert (r.status_code, r.json()) == (400, {"error": "signup_failed"})
        assert (await signup_options(second, "lc-twin")).status_code == 200  # the handle is free again
    await engine.dispose()


async def test_disable_revokes_sessions_in_one_transaction(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    async with client(settings, engine) as learner:
        r, auth, opts = await signup(learner, "lc-dis")
        uid = str(await user_id(engine, "lc-dis"))
        await execute(engine, "UPDATE users SET state = 'active' WHERE handle = 'lc-dis'")
        assert (await signin_status(learner, auth, unb64url(opts["user"]["id"]))).status_code == 200
        async with client(settings, engine) as owner:
            await _owner(owner, engine, settings)
            assert (await owner.post(f"/api/auth/signups/{uid}/disable", json={})).status_code == 204
            listed = (await owner.get("/api/auth/signups")).json()
            assert listed["accounts"][0]["disabled"] is True
        assert (await learner.get("/api/me")).status_code == 401
        assert await count(engine, "sessions", "owner_id = :u AND revoked_at IS NULL", u=uid) == 0
        assert await count(engine, "webauthn_credentials", "owner_id = :u", u=uid) == 1
    await engine.dispose()


async def test_old_owner_session_needs_reauth(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    pending = str(await insert_pending(engine, "lc-pend"))
    async with client(settings, engine) as c:
        await _owner(c, engine, settings)
        await execute(engine, "UPDATE sessions SET created_at = now() - interval '15 minutes'")
        assert (await c.get("/api/auth/signups")).status_code == 200
        r = await c.post(f"/api/auth/signups/{pending}/approve", json={})
        assert (r.status_code, r.json()) == (401, {"error": "reauth_required"})
    await engine.dispose()


async def test_approve_racing_reject_has_one_outcome(pg: Pg, tmp_path: Path) -> None:
    settings, engine = await _setup(pg, tmp_path)
    pending = str(await insert_pending(engine, "lc-race"))
    async with client(settings, engine) as c:
        await _owner(c, engine, settings)
        codes: list[int] = []

        async def act(action: str) -> None:
            codes.append((await c.post(f"/api/auth/signups/{pending}/{action}", json={})).status_code)

        async with anyio.create_task_group() as tg:
            tg.start_soon(act, "approve")
            tg.start_soon(act, "reject")
    assert sorted(codes) in ([200, 404], [204, 404])
    left = await count(engine, "users", "handle = 'lc-race'")
    assert left == (1 if 200 in codes else 0)
    await engine.dispose()


# FR-AUTH-30 — cleanup and logs -----------------------------------------------------------------------------


async def test_expiry_and_attempt_cleanup(pg: Pg, tmp_path: Path) -> None:
    from reader.auth.policy import PENDING_TTL_S
    from reader.auth.store import cleanup

    settings, engine = await _setup(pg, tmp_path)
    young = await insert_pending(engine, "lc-young", age_s=PENDING_TTL_S - 1)
    old = await insert_pending(engine, "lc-old", age_s=PENDING_TTL_S + 1)
    active_old = await insert_pending(engine, "lc-actold", state="active", age_s=PENDING_TTL_S + 100)
    await execute(engine, "INSERT INTO signup_attempts (at) VALUES (now() - interval '2 hours'), (now())")
    await cleanup(engine)
    assert await count(engine, "users", "id = :u", u=young) == 1
    assert await count(engine, "users", "id = :u", u=old) == 0
    assert await count(engine, "webauthn_credentials", "owner_id = :u", u=old) == 0
    assert await count(engine, "users", "id = :u", u=active_old) == 1
    assert await count(engine, "signup_attempts") == 1
    await engine.dispose()


async def test_expiry_skips_a_row_an_approve_holds(pg: Pg, tmp_path: Path) -> None:
    from reader.auth.policy import PENDING_TTL_S
    from reader.auth.store import cleanup
    from sqlalchemy import text

    settings, engine = await _setup(pg, tmp_path)
    old = await insert_pending(engine, "lc-held", age_s=PENDING_TTL_S + 1)
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1 FROM users WHERE id = :u FOR UPDATE"), {"u": old})
        await cleanup(engine)  # SKIP LOCKED: does not wait, does not delete
        await conn.execute(text("UPDATE users SET state = 'active' WHERE id = :u"), {"u": old})
    assert await count(engine, "users", "id = :u AND state = 'active'", u=old) == 1
    await engine.dispose()


async def test_logs_never_carry_handles_or_display_names(
    pg: Pg, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings, engine = await _setup(pg, tmp_path)
    caplog.set_level(logging.DEBUG)
    handle, name = "lc-canary9", "Canary Name Zq77"
    async with client(settings, engine) as c:
        r, auth, opts = await signup(c, handle, display_name=name)
        bad = await c.post("/api/auth/signup/verify", json={"handle": handle}, headers={"X-Forwarded-For": "198.51.100.23"})
        await signin_status(c, auth, unb64url(opts["user"]["id"]))
        uid = str(await user_id(engine, handle))
        async with client(settings, engine) as owner:
            await _owner(owner, engine, settings)
            await owner.post(f"/api/auth/signups/{uid}/approve", json={})
            await owner.post(f"/api/auth/signups/{uid}/disable", json={})
        bodies = r.text + bad.text
    text_all = caplog.text + bodies
    for needle in [handle, name, "198.51.100.23"]:
        assert needle not in text_all, needle
    assert f"signup_pending {uid}" in caplog.text
    assert f"pending_signin {uid}" in caplog.text
    assert f"signup_approve {uid}" in caplog.text and f"signup_disable {uid}" in caplog.text
    await engine.dispose()


__all__ = ["ORIGIN"]
