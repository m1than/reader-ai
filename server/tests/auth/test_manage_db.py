"""SRV-AUTH manage amendment against Postgres: people, sessions, passkeys, delete and enable (owner and self).

@trace FR-AUTH-40 FR-AUTH-41 FR-AUTH-42 FR-AUTH-43 FR-AUTH-44 FR-AUTH-45 FR-AUTH-46 FR-AUTH-48
@trace INV-AUTH-20 INV-AUTH-21
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

import anyio
import pytest
from auth_helpers import execute, scalar, set_cookies, unb64url
from conftest import Pg
from manage_helpers import (
    add_credential,
    as_user,
    audit_rows,
    course_for,
    credentials,
    learner,
    live_sessions,
    new_session,
    owner,
    setup,
    sid_of,
    snapshot,
)
from signup_helpers import SESSION, insert_pending, signin_status, signup, signup_options, user_id

pytestmark = pytest.mark.anyio
NO_ID = "00000000-0000-0000-0000-000000000000"
BY_TEXT = "id_sha256 = sha256(convert_to(:s, 'UTF8'))"
LAST_USED = "SELECT max(last_used_at) FROM webauthn_credentials WHERE owner_id = :u"
LABEL = "SELECT label FROM webauthn_credentials WHERE owner_id = :u"


async def delete(c: Any, path: str, body: Any = None) -> int:
    return int((await c.request("DELETE", path, json={} if body is None else body)).status_code)


# FR-AUTH-40 — people ---------------------------------------------------------------------------------------


async def test_people_lists_every_user_with_its_fields(pg: Pg, tmp_path: Path) -> None:
    s, engine = await setup(pg, tmp_path)
    _, session, _ = await owner(engine)
    anna, _, _ = await learner(engine, "lc-anna")
    await add_credential(engine, anna, label="Phone", used=True)
    await new_session(engine, anna)
    revoked = await new_session(engine, anna)
    await execute(engine, f"UPDATE sessions SET revoked_at = now() WHERE {BY_TEXT}", s=revoked)
    expired = await new_session(engine, anna)
    await execute(engine, f"UPDATE sessions SET expires_at = now() - interval '1s' WHERE {BY_TEXT}",
                  s=expired)
    locked = await insert_pending(engine, "lc-lock", state="active")
    await execute(engine, "DELETE FROM webauthn_credentials WHERE owner_id = :u", u=locked)
    await insert_pending(engine, "lc-pend")
    async with as_user(s, engine, session) as c:
        r = await c.get("/api/auth/users")
    assert r.status_code == 200, r.text
    users = r.json()["users"]
    assert [u["handle"] for u in users] == sorted(["ivan", "live-check", "lc-anna", "lc-lock", "lc-pend"])
    by = {u["handle"]: u for u in users}
    keys = {"id", "handle", "display_name", "role", "source", "state", "disabled_at", "locked", "created_at",
            "last_signin_at", "passkeys", "sessions_active", "owns_data"}
    assert all(set(u) == keys for u in users)
    a = by["lc-anna"]
    assert (a["id"], a["role"], a["source"], a["state"]) == (str(anna), "learner", "signup", "active")
    assert a["sessions_active"] == 2  # the fresh one from learner() and one more; revoked and expired are not
    assert a["locked"] is False and a["owns_data"] is False and a["disabled_at"] is None
    assert len(a["passkeys"]) == 2
    assert all(set(p) == {"pid", "label", "created_at", "last_used_at"} for p in a["passkeys"])
    assert sorted(str(p["label"]) for p in a["passkeys"]) == ["None", "Phone"]
    used = await scalar(engine, LAST_USED, u=anna)
    assert a["last_signin_at"] == used.isoformat()
    assert by["lc-lock"]["locked"] is True and by["lc-lock"]["passkeys"] == []
    assert by["lc-lock"]["last_signin_at"] is None
    assert by["lc-pend"]["state"] == "pending"
    assert by["live-check"]["source"] == "config"
    assert by["live-check"]["locked"] is False  # a configured user is never `locked`
    assert by["ivan"]["role"] == "owner" and by["ivan"]["sessions_active"] == 1
    body = r.text
    for secret in (session, revoked, expired):
        assert secret not in body and sid_of(secret) not in body  # people show counts, never sessions
    await engine.dispose()


async def test_owns_data_reads_the_catalog(pg: Pg, tmp_path: Path) -> None:
    from reader.auth.manage import owns_data

    s, engine = await setup(pg, tmp_path)
    anna, session, _ = await learner(engine, "lc-anna")
    bob, _, _ = await learner(engine, "lc-bob")
    async with engine.connect() as conn:
        assert await owns_data(conn, anna) is False  # sessions and credentials are SRV-AUTH's own tables
    await course_for(engine, anna, tmp_path)
    await execute(engine, "CREATE TABLE t_probe (id serial PRIMARY KEY, who uuid REFERENCES users (id))")
    async with engine.connect() as conn:
        assert await owns_data(conn, anna) is True
        assert await owns_data(conn, bob) is False
    await execute(engine, "INSERT INTO t_probe (who) VALUES (:u)", u=bob)  # a table added later counts
    # audit_log.actor_id has no foreign key: an audit row never makes data.
    await execute(
        engine,
        "INSERT INTO audit_log (actor_id, action, object_kind, object_id, detail)"
        " VALUES (:u, 'user.enable', 'user', :o, '{}')",
        u=uuid.uuid4(),
        o=str(uuid.uuid4()),
    )
    async with engine.connect() as conn:
        assert await owns_data(conn, bob) is True
    del session, s
    await engine.dispose()


async def test_people_is_owner_only(pg: Pg, tmp_path: Path) -> None:
    s, engine = await setup(pg, tmp_path)
    _, lsession, _ = await learner(engine)
    async with as_user(s, engine, lsession) as c:
        r = await c.get("/api/auth/users")
        assert (r.status_code, r.json()) == (403, {"error": "owner_only"})
    async with as_user(s, engine, None) as c:
        assert (await c.get("/api/auth/users")).status_code == 401
    await engine.dispose()


# FR-AUTH-41 — sign a person out everywhere -----------------------------------------------------------------


async def test_revoke_a_persons_sessions(pg: Pg, tmp_path: Path) -> None:
    s, engine = await setup(pg, tmp_path)
    me, session, _ = await owner(engine)
    anna, _, _ = await learner(engine)
    await new_session(engine, anna)
    config_learner = await user_id(engine, "live-check")
    await new_session(engine, config_learner)
    async with as_user(s, engine, session) as c:
        r = await c.post(f"/api/auth/users/{anna}/sessions/revoke", json={})
        assert (r.status_code, r.json()) == (200, {"revoked": 2})
        again = await c.post(f"/api/auth/users/{anna}/sessions/revoke", json={})
        assert (again.status_code, again.json()) == (200, {"revoked": 0})
        r = await c.post(f"/api/auth/users/{config_learner}/sessions/revoke", json={})  # any source
        assert (r.status_code, r.json()) == (200, {"revoked": 1})
        own = await c.post(f"/api/auth/users/{me}/sessions/revoke", json={})
        assert (own.status_code, own.json()) == (409, {"error": "use_own_sessions"})
        for bad in (NO_ID, "not-a-uuid"):
            assert (await c.post(f"/api/auth/users/{bad}/sessions/revoke", json={})).status_code == 404
        assert (await c.get("/api/me")).status_code == 200  # the owner's own session is untouched
    assert await live_sessions(engine, anna) == 0
    rows = await audit_rows(engine)
    assert rows == [
        ("user.sessions_revoke", str(anna), {"count": 2}),
        ("user.sessions_revoke", str(config_learner), {"count": 1}),
    ]  # no row for the no-op repeat
    await engine.dispose()


# FR-AUTH-42 — remove a person's passkey ------------------------------------------------------------------


async def test_remove_a_persons_passkey(pg: Pg, tmp_path: Path) -> None:
    s, engine = await setup(pg, tmp_path)
    me, session, my_pid = await owner(engine)
    anna, _, anna_pid = await learner(engine)
    other_pid = await add_credential(engine, anna)
    async with as_user(s, engine, session) as c:
        r = await c.request("DELETE", f"/api/auth/users/{anna}/credentials/{anna_pid}", json={})
        assert r.status_code == 204, r.text
        assert await credentials(engine, anna) == 1 and await live_sessions(engine, anna) == 0
        again = await c.request("DELETE", f"/api/auth/users/{anna}/credentials/{anna_pid}", json={})
        assert (again.status_code, again.json()) == (404, {"error": "not_found"})
        # The last passkey of a sign-up user may go (the account is then locked).
        assert await delete(c, f"/api/auth/users/{anna}/credentials/{other_pid}") == 204
        listed = {u["handle"]: u for u in (await c.get("/api/auth/users")).json()["users"]}
        assert listed["lc-anna"]["locked"] is True
        own = await c.request("DELETE", f"/api/auth/users/{me}/credentials/{my_pid}", json={})
        assert (own.status_code, own.json()) == (409, {"error": "use_own_passkeys"})
        # A pid of another user under this user's path: 404.
        bob, _, bob_pid = await learner(engine, "lc-bob")
        assert await delete(c, f"/api/auth/users/{anna}/credentials/{bob_pid}") == 404
        # A configured learner's passkey may be removed too (`reader bootstrap` gives a new one).
        config_learner = await user_id(engine, "live-check")
        cpid = await add_credential(engine, config_learner)
        assert await delete(c, f"/api/auth/users/{config_learner}/credentials/{cpid}") == 204
    assert await credentials(engine, me) == 1 and await credentials(engine, bob) == 1
    actions = [(a, o, d) for a, o, d in await audit_rows(engine)]
    assert actions == [
        ("user.credential_remove", str(anna), {"pid": anna_pid}),
        ("user.credential_remove", str(anna), {"pid": other_pid}),
        ("user.credential_remove", str(config_learner), {"pid": cpid}),
    ]
    await engine.dispose()


# FR-AUTH-43 — delete a sign-up learner ---------------------------------------------------------------------


async def test_delete_refusals(pg: Pg, tmp_path: Path) -> None:
    s, engine = await setup(pg, tmp_path)
    me, session, _ = await owner(engine)
    anna, _, _ = await learner(engine, "lc-anna")
    rich, _, _ = await learner(engine, "lc-rich")
    await course_for(engine, rich, tmp_path)
    boss = await insert_pending(engine, "lc-boss", state="active")
    await execute(engine, "UPDATE users SET role = 'owner' WHERE id = :u", u=boss)
    config_learner = await user_id(engine, "live-check")
    before = await snapshot(engine)
    async with as_user(s, engine, session) as c:
        cases = [
            (config_learner, "live-check", 409, "configured_user"),
            (me, "ivan", 409, "configured_user"),
            (boss, "lc-boss", 409, "not_learner"),
            (anna, "lc-ann", 400, "confirm_mismatch"),
            (anna, None, 400, "confirm_mismatch"),
            (rich, "lc-rich", 409, "user_has_data"),
            (NO_ID, "x", 404, "not_found"),
            ("not-a-uuid", "x", 404, "not_found"),
        ]
        for target, confirm, status, code in cases:
            r = await c.request("DELETE", f"/api/auth/users/{target}", json={"confirm": confirm})
            assert (r.status_code, r.json()) == (status, {"error": code}), (target, confirm)
    assert await snapshot(engine) == before
    await engine.dispose()


async def test_a_user_with_one_tutor_question_cannot_be_deleted(pg: Pg, tmp_path: Path) -> None:
    s, engine = await setup(pg, tmp_path)
    _, session, _ = await owner(engine)
    anna, _, _ = await learner(engine, "lc-anna")
    course, lesson, sha = await course_for(engine, anna, tmp_path)
    # A tutor question needs a lesson of the same owner (its foreign key), so the course is there too.
    await execute(
        engine,
        "INSERT INTO tutor_questions (owner_id, lesson_id, revision_sha256, question, state)"
        " VALUES (:o, :l, :r, 'Why?', 'answered')",
        o=anna, l=lesson, r=sha,
    )
    async with as_user(s, engine, session) as c:
        r = await c.request("DELETE", f"/api/auth/users/{anna}", json={"confirm": "lc-anna"})
        assert (r.status_code, r.json()) == (409, {"error": "user_has_data"})
    assert await scalar(engine, "SELECT count(*) FROM users WHERE id = :u", u=anna) == 1
    del course
    await engine.dispose()


async def test_delete_ends_an_open_ceremony_and_frees_the_handle(
    pg: Pg, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    s, engine = await setup(pg, tmp_path)
    me, session, _ = await owner(engine)
    anna, anna_session, _ = await learner(engine, "lc-anna")
    async with as_user(s, engine, anna_session) as person:
        opened = await person.post("/api/auth/register/options", json={})
        assert opened.status_code == 200
        assert await scalar(engine, "SELECT count(*) FROM auth_challenges WHERE user_id = :u", u=anna) == 1
        caplog.set_level(logging.INFO, logger="reader.auth")
        async with as_user(s, engine, session) as c:
            r = await c.request("DELETE", f"/api/auth/users/{anna}", json={"confirm": "lc-anna"})
            assert r.status_code == 204, r.text
            again = await c.request("DELETE", f"/api/auth/users/{anna}", json={"confirm": "lc-anna"})
            assert (again.status_code, again.json()) == (404, {"error": "not_found"})
        from softauthn import SoftAuthenticator

        cred = SoftAuthenticator.random().register(challenge=unb64url(opened.json()["challenge"]))
        late = await person.post("/api/auth/register/verify", json=cred)
        assert (late.status_code, late.json()) == (401, {"error": "unauthenticated"})
    for table, column in [("users", "id"), ("webauthn_credentials", "owner_id"), ("sessions", "owner_id"),
                          ("auth_challenges", "user_id")]:
        assert await scalar(engine, f"SELECT count(*) FROM {table} WHERE {column} = :u", u=anna) == 0, table
    assert await audit_rows(engine) == [("user.delete", str(anna), {})]
    assert await scalar(engine, "SELECT actor_id FROM audit_log") == me
    assert f"auth user_deleted {anna}" in [r.getMessage() for r in caplog.records]
    assert all("lc-anna" not in r.getMessage() for r in caplog.records)
    # The handle is free again: a new sign-up may take it.
    async with as_user(s, engine, None) as visitor:
        assert (await signup_options(visitor, "lc-anna")).status_code == 200
        r, _, _ = await signup(visitor, "lc-anna")
        assert r.status_code == 202
    await engine.dispose()


async def test_delete_cancels_open_signup_ceremonies_for_the_handle(pg: Pg, tmp_path: Path) -> None:
    s, engine = await setup(pg, tmp_path)
    _, session, _ = await owner(engine)
    anna, _, _ = await learner(engine, "lc-anna")
    async with as_user(s, engine, None) as visitor:
        assert (await signup_options(visitor, "lc-other")).status_code == 200
    await execute(
        engine, "UPDATE auth_challenges SET signup_handle = 'lc-anna' WHERE signup_handle = 'lc-other'"
    )
    async with as_user(s, engine, session) as c:
        assert await delete(c, f"/api/auth/users/{anna}", {"confirm": "lc-anna"}) == 204
    assert await scalar(engine, "SELECT count(*) FROM auth_challenges WHERE signup_handle = 'lc-anna'") == 0
    await engine.dispose()


async def test_delete_takes_the_invitations_the_learner_issued(pg: Pg, tmp_path: Path) -> None:
    """`invitations.owner_id` is the issuing account: an account row (not in owns_data), deleted with it."""
    s, engine = await setup(pg, tmp_path)
    me, session, _ = await owner(engine)
    anna, _, _ = await learner(engine, "lc-anna")
    await execute(engine, "INSERT INTO invitations (owner_id) VALUES (:u)", u=anna)
    await execute(engine, "INSERT INTO invitations (owner_id) VALUES (:u)", u=me)
    async with as_user(s, engine, session) as c:
        people = (await c.get("/api/auth/users")).json()["users"]
        assert next(p for p in people if p["id"] == str(anna))["owns_data"] is False
        r = await c.request("DELETE", f"/api/auth/users/{anna}", json={"confirm": "lc-anna"})
        assert r.status_code == 204, r.text
    assert await scalar(engine, "SELECT count(*) FROM users WHERE id = :u", u=anna) == 0
    assert await scalar(engine, "SELECT count(*) FROM invitations WHERE owner_id = :u", u=anna) == 0
    assert await scalar(engine, "SELECT count(*) FROM invitations WHERE owner_id = :u", u=me) == 1
    await engine.dispose()


async def test_late_foreign_key_violation_answers_user_has_data(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import reader.auth.manage as manage

    s, engine = await setup(pg, tmp_path)
    _, session, _ = await owner(engine)
    anna, _, _ = await learner(engine, "lc-anna")
    # Fault injection: a row written "after" owns_data read its table.
    await course_for(engine, anna, tmp_path)

    async def stale_read(conn: Any, uid: Any) -> bool:
        return False

    monkeypatch.setattr(manage, "owns_data", stale_read)
    before = await snapshot(engine)
    async with as_user(s, engine, session) as c:
        r = await c.request("DELETE", f"/api/auth/users/{anna}", json={"confirm": "lc-anna"})
        assert (r.status_code, r.json()) == (409, {"error": "user_has_data"})
    assert await snapshot(engine) == before  # sessions and credentials rolled back with the savepoint
    await engine.dispose()


# FR-AUTH-44 — own sessions ---------------------------------------------------------------------------------


async def test_own_sessions_list_revoke_one_and_others(pg: Pg, tmp_path: Path) -> None:
    s, engine = await setup(pg, tmp_path)
    anna, current, _ = await learner(engine, "lc-anna")
    older = await new_session(engine, anna, age_s=120)
    oldest = await new_session(engine, anna, age_s=240)
    gone = await new_session(engine, anna)
    await execute(engine, f"UPDATE sessions SET revoked_at = now() WHERE {BY_TEXT}", s=gone)
    bob, bob_session, _ = await learner(engine, "lc-bob")
    async with as_user(s, engine, current) as c:
        listed = (await c.get("/api/auth/sessions")).json()["sessions"]
        assert [x["sid"] for x in listed] == [sid_of(current), sid_of(older), sid_of(oldest)]
        assert [x["current"] for x in listed] == [True, False, False]
        assert all(set(x) == {"sid", "created_at", "expires_at", "current"} for x in listed)
        assert current not in str(listed)
        foreign = await c.request("DELETE", f"/api/auth/sessions/{sid_of(bob_session)}", json={})
        assert (foreign.status_code, foreign.json()) == (404, {"error": "not_found"})
        for bad in (sid_of(gone), "zz", "0" * 16):
            assert (await c.request("DELETE", f"/api/auth/sessions/{bad}", json={})).status_code == 404
        one = await c.request("DELETE", f"/api/auth/sessions/{sid_of(older)}", json={})
        assert one.status_code == 204
        others = await c.post("/api/auth/sessions/revoke-others", json={})
        assert (others.status_code, others.json()) == (200, {"revoked": 1})
        none = await c.post("/api/auth/sessions/revoke-others", json={})
        assert (none.status_code, none.json()) == (200, {"revoked": 0})
        assert (await c.get("/api/me")).status_code == 200  # INV-AUTH-21: the current session stays
        signed_out = await c.request("DELETE", f"/api/auth/sessions/{sid_of(current)}", json={})
        assert signed_out.status_code == 204
        cookie = next(h for h in set_cookies(signed_out) if h.startswith(SESSION + "="))
        for attr in ("Max-Age=0", "Path=/", "Secure", "HttpOnly", "SameSite=lax"):
            assert attr.lower() in cookie.lower(), attr
        assert "domain" not in cookie.lower()
    async with as_user(s, engine, current) as c:
        assert (await c.get("/api/me")).status_code == 401
    assert await live_sessions(engine, bob) == 1
    assert await audit_rows(engine) == [
        ("self.session_revoke", str(anna), {"sid": sid_of(older), "count": 1}),
        ("self.session_revoke", str(anna), {"count": 1}),
        ("self.session_revoke", str(anna), {"sid": sid_of(current), "count": 1}),
    ]
    await engine.dispose()


# FR-AUTH-45 — own passkeys ---------------------------------------------------------------------------------


async def test_own_passkeys_list_and_label(pg: Pg, tmp_path: Path) -> None:
    s, engine = await setup(pg, tmp_path)
    anna, session, pid = await learner(engine, "lc-anna")
    _, _, bob_pid = await learner(engine, "lc-bob")
    async with as_user(s, engine, session) as c:
        listed = (await c.get("/api/auth/credentials")).json()["credentials"]
        assert [x["pid"] for x in listed] == [pid]
        assert set(listed[0]) == {"pid", "label", "created_at", "last_used_at", "backed_up", "transports"}
        r = await c.post(f"/api/auth/credentials/{pid}", json={"label": "  Phone  "})
        assert r.status_code == 200, r.text
        assert await scalar(engine, LABEL, u=anna) == "Phone"
        same = await c.post(f"/api/auth/credentials/{pid}", json={"label": "Phone"})  # a no-op: no audit row
        assert same.status_code == 200
        for bad in ["", "   ", "x" * 65, "a\x07b", "a\nb", 5, True, ["x"], {"x": 1}]:
            r = await c.post(f"/api/auth/credentials/{pid}", json={"label": bad})
            assert (r.status_code, r.json()) == (400, {"error": "label_invalid"}), bad
        assert (await c.post(f"/api/auth/credentials/{pid}", json={})).status_code == 400
        assert (await c.post(f"/api/auth/credentials/{pid}", json={"label": "é" * 64})).status_code == 200
        cleared = await c.post(f"/api/auth/credentials/{pid}", json={"label": None})
        assert cleared.status_code == 200
        foreign = await c.post(f"/api/auth/credentials/{bob_pid}", json={"label": "Mine"})
        assert (foreign.status_code, foreign.json()) == (404, {"error": "not_found"})
    assert await scalar(engine, LABEL, u=anna) is None
    assert [a for a, _, _ in await audit_rows(engine)] == ["self.credential_label"] * 3
    await engine.dispose()


async def test_label_column_has_its_check(pg: Pg, tmp_path: Path) -> None:
    from sqlalchemy.exc import IntegrityError

    _, engine = await setup(pg, tmp_path)
    anna, _, _ = await learner(engine)
    for bad in ["", "x" * 65]:
        with pytest.raises(IntegrityError):
            await execute(
                engine, "UPDATE webauthn_credentials SET label = :l WHERE owner_id = :u", l=bad, u=anna
            )
    await engine.dispose()


# FR-AUTH-46 — remove an own passkey ------------------------------------------------------------------------


async def test_remove_own_passkey(pg: Pg, tmp_path: Path) -> None:
    s, engine = await setup(pg, tmp_path)
    anna, current, first = await learner(engine, "lc-anna")
    second = await add_credential(engine, anna)
    await new_session(engine, anna)
    await new_session(engine, anna)
    _, _, bob_pid = await learner(engine, "lc-bob")
    async with as_user(s, engine, current) as c:
        foreign = await c.request("DELETE", f"/api/auth/credentials/{bob_pid}", json={})
        assert (foreign.status_code, foreign.json()) == (404, {"error": "not_found"})
        r = await c.request("DELETE", f"/api/auth/credentials/{first}", json={})
        assert (r.status_code, r.json()) == (200, {"revoked_sessions": 2})
        last = await c.request("DELETE", f"/api/auth/credentials/{second}", json={})
        assert (last.status_code, last.json()) == (409, {"error": "last_passkey"})
        assert (await c.get("/api/me")).status_code == 200
    assert await credentials(engine, anna) == 1 and await live_sessions(engine, anna) == 1
    assert await audit_rows(engine) == [
        ("self.credential_remove", str(anna), {"pid": first, "revoked_sessions": 2}),
    ]
    await engine.dispose()


async def test_remove_own_passkey_needs_a_recent_session(pg: Pg, tmp_path: Path) -> None:
    s, engine = await setup(pg, tmp_path)
    anna, _, first = await learner(engine, "lc-anna")
    await add_credential(engine, anna)
    old = await new_session(engine, anna, age_s=900)
    async with as_user(s, engine, old) as c:
        r = await c.request("DELETE", f"/api/auth/credentials/{first}", json={})
        assert (r.status_code, r.json()) == (401, {"error": "reauth_required"})
    assert await credentials(engine, anna) == 2
    await engine.dispose()


async def test_concurrent_removal_of_the_last_two_passkeys(pg: Pg, tmp_path: Path) -> None:
    s, engine = await setup(pg, tmp_path)
    anna, current, first = await learner(engine, "lc-anna")
    second = await add_credential(engine, anna)
    other = await new_session(engine, anna)
    codes: list[int] = []
    async with as_user(s, engine, current) as c:

        async def remove(pid: str) -> None:
            codes.append((await c.request("DELETE", f"/api/auth/credentials/{pid}", json={})).status_code)

        with anyio.fail_after(30):
            async with anyio.create_task_group() as tg:
                tg.start_soon(remove, first)
                tg.start_soon(remove, second)
        assert (await c.get("/api/me")).status_code == 200
    assert sorted(codes) == [200, 409]
    assert await credentials(engine, anna) == 1
    revoked = await scalar(engine, f"SELECT revoked_at IS NOT NULL FROM sessions WHERE {BY_TEXT}", s=other)
    assert revoked is True
    await engine.dispose()


# FR-AUTH-48 — enable a sign-up account ---------------------------------------------------------------------


async def test_disable_enable_then_sign_in_with_the_same_passkey(pg: Pg, tmp_path: Path) -> None:
    s, engine = await setup(pg, tmp_path)
    me, session, _ = await owner(engine)
    async with as_user(s, engine, None) as visitor:
        r, auth, opts = await signup(visitor, "lc-dis")
        assert r.status_code == 202
    uid = await user_id(engine, "lc-dis")
    async with as_user(s, engine, session) as c:
        assert (await c.post(f"/api/auth/signups/{uid}/approve", json={})).status_code == 200
        assert (await c.post(f"/api/auth/signups/{uid}/disable", json={})).status_code == 204
        async with as_user(s, engine, None) as person:
            assert (await signin_status(person, auth, unb64url(opts["user"]["id"]))).status_code == 400
        for _ in range(2):
            e = await c.post(f"/api/auth/signups/{uid}/enable", json={})
            assert (e.status_code, e.json()) == (200, {"status": "active"})
    assert await scalar(engine, "SELECT disabled_at FROM users WHERE id = :u", u=uid) is None
    async with as_user(s, engine, None) as person:
        assert (await signin_status(person, auth, unb64url(opts["user"]["id"]))).status_code == 200
        assert (await person.get("/api/me")).json()["handle"] == "lc-dis"
    assert await audit_rows(engine) == [("user.enable", str(uid), {})]  # one row: the repeat is a no-op
    assert await scalar(engine, "SELECT actor_id FROM audit_log") == me
    await engine.dispose()


async def test_enable_refusals(pg: Pg, tmp_path: Path) -> None:
    s, engine = await setup(pg, tmp_path)
    _, session, _ = await owner(engine)
    pending = await insert_pending(engine, "lc-pend")
    config_learner = await user_id(engine, "live-check")
    await execute(engine, "UPDATE users SET disabled_at = now() WHERE id = :u", u=config_learner)
    disabled = await insert_pending(engine, "lc-off", state="active")
    await execute(engine, "UPDATE users SET disabled_at = now() WHERE id = :u", u=disabled)
    _, lsession, _ = await learner(engine, "lc-lrn")
    before = await snapshot(engine)
    async with as_user(s, engine, session) as c:
        r = await c.post(f"/api/auth/signups/{pending}/enable", json={})
        assert (r.status_code, r.json()) == (409, {"error": "pending"})
        for target in (config_learner, NO_ID, "x"):
            r = await c.post(f"/api/auth/signups/{target}/enable", json={})
            assert (r.status_code, r.json()) == (404, {"error": "not_found"}), target
    async with as_user(s, engine, lsession) as c:
        r = await c.post(f"/api/auth/signups/{disabled}/enable", json={})
        assert (r.status_code, r.json()) == (403, {"error": "owner_only"})
    assert await snapshot(engine) == before
    await engine.dispose()


# INV-AUTH-20 — access matrix; INV-AUTH-21 — at least one way in ------------------------------------------

OWNER_ONLY = [
    ("GET", "/api/auth/users"),
    ("POST", "/api/auth/users/{uid}/sessions/revoke"),
    ("DELETE", "/api/auth/users/{uid}/credentials/{pid}"),
    ("DELETE", "/api/auth/users/{uid}"),
    ("POST", "/api/auth/signups/{uid}/enable"),
]
OWN_OBJECT = [
    ("DELETE", "/api/auth/sessions/{sid}"),
    ("POST", "/api/auth/credentials/{pid}"),
    ("DELETE", "/api/auth/credentials/{pid}"),
]
SELF = [
    ("GET", "/api/auth/sessions"),
    ("POST", "/api/auth/sessions/revoke-others"),
    ("GET", "/api/auth/credentials"),
]


async def test_learner_and_second_user_matrix(pg: Pg, tmp_path: Path) -> None:
    s, engine = await setup(pg, tmp_path)
    me, owner_session, owner_pid = await owner(engine)
    anna, anna_session, anna_pid = await learner(engine, "lc-anna")
    victim, victim_session, victim_pid = await learner(engine, "lc-vic")
    values = {"uid": str(victim), "pid": victim_pid, "sid": sid_of(victim_session)}
    body = {"confirm": "lc-vic", "label": "x"}
    before = await snapshot(engine)
    async with as_user(s, engine, anna_session) as c:
        for method, path in OWNER_ONLY:
            r = await c.request(method, path.format(**values), json=None if method == "GET" else body)
            assert (r.status_code, r.json()) == (403, {"error": "owner_only"}), (method, path)
        for method, path in OWN_OBJECT:
            r = await c.request(method, path.format(**values), json=body)
            assert (r.status_code, r.json()) == (404, {"error": "not_found"}), (method, path)
        # The owner's own sid and pid are another user's objects for a learner too.
        for method, path in OWN_OBJECT:
            target = path.format(sid=sid_of(owner_session), pid=owner_pid)
            assert (await c.request(method, target, json=body)).status_code == 404
    assert await snapshot(engine) == before
    async with as_user(s, engine, anna_session) as c:
        for method, path in OWNER_ONLY + OWN_OBJECT + SELF:
            r = await c.request(method, path.format(**values), json=None if method == "GET" else body,
                                headers={"Origin": "https://evil.example"})
            if method != "GET":
                assert (r.status_code, r.json()) == (403, {"error": "origin"}), (method, path)
    assert await snapshot(engine) == before
    del me, anna, anna_pid
    await engine.dispose()


RECENT_OWNER = [
    ("POST", "/api/auth/users/{uid}/sessions/revoke"),
    ("DELETE", "/api/auth/users/{uid}/credentials/{pid}"),
    ("DELETE", "/api/auth/users/{uid}"),
    ("POST", "/api/auth/signups/{uid}/enable"),
]


async def test_no_session_and_a_stale_session_get_the_exact_401_and_change_nothing(
    pg: Pg, tmp_path: Path
) -> None:
    """Each target is one the request would change (a disabled, deletable learner with a live session and two
    passkeys; the caller's own second passkey), so a relaxed gate shows up in the row snapshot."""
    s, engine = await setup(pg, tmp_path)
    me, _, _ = await owner(engine)
    anna, _, anna_pid = await learner(engine, "lc-anna")
    await add_credential(engine, anna)
    victim, victim_session, victim_pid = await learner(engine, "lc-vic")
    await add_credential(engine, victim)
    await execute(engine, "UPDATE users SET disabled_at = now() WHERE id = :u", u=victim)
    stale_owner = await new_session(engine, me, age_s=900)
    stale_anna = await new_session(engine, anna, age_s=900)
    values = {"uid": str(victim), "pid": victim_pid, "sid": sid_of(victim_session)}
    body = {"confirm": "lc-vic", "label": "x"}
    before = await snapshot(engine)
    async with as_user(s, engine, None) as c:
        for method, path in OWNER_ONLY + OWN_OBJECT + SELF:
            r = await c.request(method, path.format(**values), json=None if method == "GET" else body)
            assert (r.status_code, r.json()) == (401, {"error": "unauthenticated"}), (method, path)
    assert await snapshot(engine) == before
    async with as_user(s, engine, stale_owner) as c:
        for method, path in RECENT_OWNER:
            r = await c.request(method, path.format(**values), json=body)
            assert (r.status_code, r.json()) == (401, {"error": "reauth_required"}), (method, path)
    async with as_user(s, engine, stale_anna) as c:
        r = await c.request("DELETE", f"/api/auth/credentials/{anna_pid}", json={})
        assert (r.status_code, r.json()) == (401, {"error": "reauth_required"})
    assert await snapshot(engine) == before
    assert await credentials(engine, anna) == 2 and await credentials(engine, victim) == 2
    assert await live_sessions(engine, victim) == 1
    await engine.dispose()


async def test_no_route_changes_a_configured_user_row(pg: Pg, tmp_path: Path) -> None:
    s, engine = await setup(pg, tmp_path)
    _, session, _ = await owner(engine)
    config_learner = await user_id(engine, "live-check")
    cpid = await add_credential(engine, config_learner)
    await add_credential(engine, config_learner)
    csession = await new_session(engine, config_learner)
    row = "SELECT row(id, handle, role, source, state, disabled_at)::text FROM users WHERE id = :u"
    before = await scalar(engine, row, u=config_learner)
    async with as_user(s, engine, session) as c:
        await c.post(f"/api/auth/users/{config_learner}/sessions/revoke", json={})
        await c.request("DELETE", f"/api/auth/users/{config_learner}/credentials/{cpid}", json={})
        await c.request("DELETE", f"/api/auth/users/{config_learner}", json={"confirm": "live-check"})
        await c.post(f"/api/auth/signups/{config_learner}/enable", json={})
    async with as_user(s, engine, csession) as c:
        await c.post("/api/auth/sessions/revoke-others", json={})
    assert await scalar(engine, row, u=config_learner) == before
    await engine.dispose()


async def test_the_owner_always_keeps_a_way_in(pg: Pg, tmp_path: Path) -> None:
    s, engine = await setup(pg, tmp_path)
    me, session, pid = await owner(engine)
    await new_session(engine, me)
    async with as_user(s, engine, session) as c:
        assert (await c.request("DELETE", f"/api/auth/credentials/{pid}", json={})).status_code == 409
        assert await delete(c, f"/api/auth/users/{me}/credentials/{pid}") == 409
        assert await delete(c, f"/api/auth/users/{me}", {"confirm": "ivan"}) == 409
        assert (await c.post(f"/api/auth/users/{me}/sessions/revoke", json={})).status_code == 409
        assert (await c.post("/api/auth/sessions/revoke-others", json={})).json() == {"revoked": 1}
        assert (await c.get("/api/me")).status_code == 200
    assert await credentials(engine, me) == 1
    await engine.dispose()
