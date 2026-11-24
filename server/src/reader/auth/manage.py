"""People, own sessions and own passkeys (SRV-AUTH FR-AUTH-40..48, INV-AUTH-20..21; WEB-MANAGE FR-MANAGE-7).

Every write that touches one user's sessions or credentials first locks that user's `users` row (the row
FR-AUTH-29 locks), never the credential rows, so two such writes on one user serialise and cannot deadlock.
A session is named by `sid` (the first 16 hex of its `id_sha256`), a passkey by `pid` (the first 16 hex of
sha256(credential_id)); neither authenticates anyone. Each applied change writes its SRV-EVENTS audit row in
the same transaction; a no-op writes none. Logs carry ids and counts, never a handle, a label or a session.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.responses import JSONResponse, Response

from reader.api.http import Ctx, Handler, error, set_cookie
from reader.auth.handles import WHITE_SPACE
from reader.auth.policy import SESSION_COOKIE
from reader.auth.store import digest
from reader.jobs import audit

log = logging.getLogger("reader.auth")

HEX16: Final = re.compile(r"[0-9a-f]{16}")
LABEL_MAX: Final = 64
# SRV-AUTH's own tables: rows that describe the account, not data the user owns (FR-AUTH-40).
OWN_TABLES: Final = ("sessions", "webauthn_credentials", "auth_challenges", "bootstrap_tokens", "invitations")
_REFERENCES: Final = text(
    "SELECT c.conrelid::regclass::text, a.attname FROM pg_constraint c"
    " CROSS JOIN LATERAL unnest(c.conkey, c.confkey) AS k(src, dst)"
    " JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.src"
    " JOIN pg_attribute b ON b.attrelid = c.confrelid AND b.attnum = k.dst"
    " WHERE c.contype = 'f' AND c.confrelid = 'users'::regclass AND b.attname = 'id'"
    " AND c.conrelid::regclass::text <> ALL(:own) ORDER BY 1, 2"
)
_LOCK: Final = text("SELECT id, handle, role, source, state, disabled_at FROM users WHERE id = :u FOR UPDATE")
_CREDENTIAL: Final = (
    "SELECT credential_id, label FROM webauthn_credentials"
    " WHERE owner_id = :u AND left(encode(sha256(credential_id), 'hex'), 16) = :p"
)
_LIVE_SESSION: Final = (
    "SELECT id_sha256 FROM sessions WHERE owner_id = :u AND revoked_at IS NULL AND expires_at > now()"
    " AND left(encode(id_sha256, 'hex'), 16) = :s"
)
_REVOKE: Final = text(
    "UPDATE sessions SET revoked_at = now() WHERE owner_id = :u AND revoked_at IS NULL"
    " AND (CAST(:keep AS bytea) IS NULL OR id_sha256 <> :keep) RETURNING 1"
)
# FR-AUTH-43: the account's own rows, after its challenges, in foreign-key order.
_DELETE_ACCOUNT: Final = tuple(
    text(sql)
    for sql in (
        "DELETE FROM auth_challenges WHERE user_id = :u",
        "DELETE FROM auth_challenges WHERE kind = 'signup' AND signup_handle = :h AND consumed_at IS NULL",
        "DELETE FROM bootstrap_tokens WHERE user_id = :u",
        "DELETE FROM sessions WHERE owner_id = :u",
        "DELETE FROM webauthn_credentials WHERE owner_id = :u",
        "DELETE FROM invitations WHERE owner_id = :u",  # the invitations it issued: account rows (OWN_TABLES)
        "DELETE FROM users WHERE id = :u",
    )
)


class _Answer(Exception):
    """An early answer from inside a transaction; the transaction rolls back."""

    def __init__(self, response: Response) -> None:
        super().__init__("answer")
        self.response = response


def _iso(value: Any) -> str | None:
    return None if value is None else str(value.isoformat())


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def owns_data(conn: AsyncConnection, user_id: uuid.UUID) -> bool:
    """True when a column with a foreign key to `users.id`, outside SRV-AUTH's own tables, holds the id. The
    columns come from the catalog, so a table added later counts without a change here."""
    for table, column in (await conn.execute(_REFERENCES, {"own": list(OWN_TABLES)})).all():
        probe = f"SELECT 1 FROM {_ident(str(table))} WHERE {_ident(str(column))} = :u LIMIT 1"  # noqa: S608
        if (await conn.execute(text(probe), {"u": user_id})).first() is not None:
            return True
    return False


async def _one(conn: AsyncConnection, sql: str, params: dict[str, Any], what: str) -> Any:
    """The single row a 16-hex name selects: 404 for none, 409 `ambiguous` (logged) for a prefix collision."""
    rows = (await conn.execute(text(sql), params)).all()
    if not rows:
        raise _Answer(error(404, "not_found"))
    if len(rows) > 1:
        log.warning("auth ambiguous_%s %s", what, params.get("u"))
        raise _Answer(error(409, "ambiguous"))
    return rows[0]


def guarded(handler: Handler, *, owner: bool = False) -> Handler:
    """The handler as a route's: an owner-only route answers 403 `owner_only` to anyone else first; an early
    `_Answer` becomes the response."""

    async def run(ctx: Ctx) -> Response:
        if owner and ctx.signed_in.role != "owner":
            return error(403, "owner_only")
        try:
            return await handler(ctx)
        except _Answer as answer:
            return answer.response

    return run


def _target(ctx: Ctx) -> uuid.UUID:
    try:
        return uuid.UUID(str(ctx.request.path_params.get("user_id")))
    except ValueError:
        raise _Answer(error(404, "not_found")) from None


def _hex16(ctx: Ctx, name: str) -> str:
    value = str(ctx.request.path_params.get(name, ""))
    if HEX16.fullmatch(value) is None:
        raise _Answer(error(404, "not_found"))
    return value


async def _body(ctx: Ctx) -> dict[str, Any]:
    body = await ctx.json()
    return body if isinstance(body, dict) else {}


async def _revoke(conn: AsyncConnection, user_id: uuid.UUID, keep: bytes | None = None) -> int:
    return len((await conn.execute(_REVOKE, {"u": user_id, "keep": keep})).all())


# FR-AUTH-40 — people ----------------------------------------------------------------------------------------

_PEOPLE: Final = text(
    "SELECT u.id, u.handle, u.display_name, u.role, u.source, u.state, u.disabled_at, u.created_at,"
    " (SELECT max(c.last_used_at) FROM webauthn_credentials c WHERE c.owner_id = u.id),"
    " (SELECT count(*) FROM sessions s WHERE s.owner_id = u.id AND s.revoked_at IS NULL"
    " AND s.expires_at > now())"
    " FROM users u ORDER BY u.handle"
)
_PASSKEYS: Final = text(
    "SELECT owner_id, left(encode(sha256(credential_id), 'hex'), 16), label, created_at, last_used_at"
    " FROM webauthn_credentials ORDER BY created_at, credential_id"
)


async def people(ctx: Ctx) -> Response:
    async with ctx.engine.connect() as conn:
        keys: dict[uuid.UUID, list[dict[str, Any]]] = {}
        for owner, pid, label, created, used in await conn.execute(_PASSKEYS):
            keys.setdefault(owner, []).append(
                {"pid": pid, "label": label, "created_at": _iso(created), "last_used_at": _iso(used)}
            )
        listed = []
        for uid, handle, name, role, source, state, disabled, created, last, active in (
            await conn.execute(_PEOPLE)
        ).all():
            passkeys = keys.get(uid, [])
            listed.append({
                "id": str(uid),
                "handle": handle,
                "display_name": name,
                "role": role,
                "source": source,
                "state": state,
                "disabled_at": _iso(disabled),
                "locked": source == "signup" and not passkeys,
                "created_at": _iso(created),
                "last_signin_at": _iso(last),
                "passkeys": passkeys,
                "sessions_active": int(active),
                "owns_data": await owns_data(conn, uid),
            })
    return JSONResponse({"users": listed})


# FR-AUTH-41, FR-AUTH-42 — another person's sessions and passkeys -------------------------------------------


async def revoke_person(ctx: Ctx) -> Response:
    target = _target(ctx)
    if target == ctx.signed_in.id:
        return error(409, "use_own_sessions")
    async with ctx.engine.begin() as conn:
        if (await conn.execute(_LOCK, {"u": target})).first() is None:
            return error(404, "not_found")
        count = await _revoke(conn, target)
        if count:
            await audit.record(conn, ctx.signed_in.id, "user.sessions_revoke", str(target), {"count": count})
    log.info("auth sessions_revoked %s %d", target, count)
    return JSONResponse({"revoked": count})


async def remove_persons_passkey(ctx: Ctx) -> Response:
    target, pid = _target(ctx), _hex16(ctx, "pid")
    if target == ctx.signed_in.id:
        return error(409, "use_own_passkeys")
    async with ctx.engine.begin() as conn:
        if (await conn.execute(_LOCK, {"u": target})).first() is None:
            return error(404, "not_found")
        row = await _one(conn, _CREDENTIAL, {"u": target, "p": pid}, "pid")
        await conn.execute(text("DELETE FROM webauthn_credentials WHERE credential_id = :c"), {"c": row[0]})
        await _revoke(conn, target)  # a lost device's sessions end with its passkey
        await audit.record(conn, ctx.signed_in.id, "user.credential_remove", str(target), {"pid": pid})
    log.info("auth credential_removed %s", target)
    return Response(status_code=204)


# FR-AUTH-43 — delete a sign-up learner ---------------------------------------------------------------------


async def delete_person(ctx: Ctx) -> Response:
    target = _target(ctx)
    confirm = (await _body(ctx)).get("confirm")
    async with ctx.engine.begin() as conn:
        user = (await conn.execute(_LOCK, {"u": target})).first()
        if user is None:
            return error(404, "not_found")
        if user.source != "signup":
            return error(409, "configured_user")
        if user.role != "learner":
            return error(409, "not_learner")
        if not isinstance(confirm, str) or confirm != user.handle:
            return error(400, "confirm_mismatch")
        if await owns_data(conn, target):
            return error(409, "user_has_data")
        try:
            async with conn.begin_nested():
                for statement in _DELETE_ACCOUNT:
                    await conn.execute(statement, {"u": target, "h": user.handle})
        except IntegrityError:
            # A row written by a table owns_data read before the lock reached it: back to the savepoint.
            return error(409, "user_has_data")
        await audit.record(conn, ctx.signed_in.id, "user.delete", str(target), {})
    log.info("auth user_deleted %s", target)
    return Response(status_code=204)


# FR-AUTH-48 — enable a sign-up account ---------------------------------------------------------------------


async def enable(ctx: Ctx) -> Response:
    target = _target(ctx)
    async with ctx.engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT state, disabled_at FROM users WHERE id = :u AND source = 'signup' FOR UPDATE"),
                {"u": target},
            )
        ).first()
        if row is None:
            return error(404, "not_found")
        if row.state == "pending":
            return error(409, "pending")
        if row.disabled_at is None:
            return JSONResponse({"status": "active"})  # already enabled: a no-op
        await conn.execute(text("UPDATE users SET disabled_at = NULL WHERE id = :u"), {"u": target})
        await audit.record(conn, ctx.signed_in.id, "user.enable", str(target), {})
    log.info("auth signup_enable %s", target)
    return JSONResponse({"status": "active"})


# FR-AUTH-44 — own sessions ---------------------------------------------------------------------------------


def _current(ctx: Ctx) -> bytes:
    return digest(ctx.session or "")


_SESSIONS: Final = text(
    "SELECT left(encode(id_sha256, 'hex'), 16), created_at, expires_at, id_sha256 FROM sessions"
    " WHERE owner_id = :u AND revoked_at IS NULL AND expires_at > now() ORDER BY created_at DESC, id_sha256"
)


async def own_sessions(ctx: Ctx) -> Response:
    current = _current(ctx)
    async with ctx.engine.connect() as conn:
        listed = [
            {"sid": sid, "created_at": _iso(c), "expires_at": _iso(e), "current": bytes(h) == current}
            for sid, c, e, h in await conn.execute(_SESSIONS, {"u": ctx.signed_in.id})
        ]
    return JSONResponse({"sessions": listed})


async def revoke_own_session(ctx: Ctx) -> Response:
    sid, me = _hex16(ctx, "sid"), ctx.signed_in.id
    async with ctx.engine.begin() as conn:
        await conn.execute(_LOCK, {"u": me})
        row = await _one(conn, _LIVE_SESSION, {"u": me, "s": sid}, "sid")
        await conn.execute(text("UPDATE sessions SET revoked_at = now() WHERE id_sha256 = :h"), {"h": row[0]})
        await audit.record(conn, me, "self.session_revoke", str(me), {"sid": sid, "count": 1})
    response = Response(status_code=204)
    if bytes(row[0]) == _current(ctx):  # sign-out: the cookie is cleared as POST /api/auth/signout does
        set_cookie(response, SESSION_COOKIE, "", 0)
    return response


async def revoke_other_sessions(ctx: Ctx) -> Response:
    me = ctx.signed_in.id
    async with ctx.engine.begin() as conn:
        await conn.execute(_LOCK, {"u": me})
        count = await _revoke(conn, me, keep=_current(ctx))
        if count:
            await audit.record(conn, me, "self.session_revoke", str(me), {"count": count})
    return JSONResponse({"revoked": count})


# FR-AUTH-45, FR-AUTH-46 — own passkeys ---------------------------------------------------------------------

_OWN_PASSKEYS: Final = text(
    "SELECT left(encode(sha256(credential_id), 'hex'), 16), label, created_at, last_used_at, backed_up,"
    " transports FROM webauthn_credentials WHERE owner_id = :u ORDER BY created_at, credential_id"
)


async def own_passkeys(ctx: Ctx) -> Response:
    async with ctx.engine.connect() as conn:
        listed = [
            {
                "pid": pid,
                "label": label,
                "created_at": _iso(created),
                "last_used_at": _iso(used),
                "backed_up": bool(backed),
                "transports": list(transports or []),
            }
            for pid, label, created, used, backed, transports in await conn.execute(
                _OWN_PASSKEYS, {"u": ctx.signed_in.id}
            )
        ]
    return JSONResponse({"credentials": listed})


class _Invalid:
    """A label that FR-AUTH-45 refuses."""


INVALID: Final = _Invalid()


def label_of(body: dict[str, Any]) -> str | _Invalid | None:
    """FR-AUTH-45: the trimmed label (1..64 characters, no Cc), None to clear, or INVALID."""
    if "label" not in body:
        return INVALID
    value = body["label"]
    if value is None:
        return None
    if not isinstance(value, str):
        return INVALID
    label = value.strip(WHITE_SPACE)
    if not 1 <= len(label) <= LABEL_MAX or any(unicodedata.category(ch) in ("Cc", "Cs") for ch in label):
        return INVALID
    return label


async def label_passkey(ctx: Ctx) -> Response:
    pid, me = _hex16(ctx, "pid"), ctx.signed_in.id
    label = label_of(await _body(ctx))
    if isinstance(label, _Invalid):
        return error(400, "label_invalid")
    async with ctx.engine.begin() as conn:
        await conn.execute(_LOCK, {"u": me})
        row = await _one(conn, _CREDENTIAL, {"u": me, "p": pid}, "pid")
        if row[1] != label:
            await conn.execute(
                text("UPDATE webauthn_credentials SET label = :l WHERE credential_id = :c"),
                {"l": label, "c": row[0]},
            )
            await audit.record(conn, me, "self.credential_label", str(me), {"pid": pid})
    return JSONResponse({"pid": pid, "label": label})


async def remove_own_passkey(ctx: Ctx) -> Response:
    pid, me = _hex16(ctx, "pid"), ctx.signed_in.id
    async with ctx.engine.begin() as conn:
        await conn.execute(_LOCK, {"u": me})
        row = await _one(conn, _CREDENTIAL, {"u": me, "p": pid}, "pid")
        total = await conn.execute(
            text("SELECT count(*) FROM webauthn_credentials WHERE owner_id = :u"), {"u": me}
        )
        if int(total.scalar_one()) <= 1:
            return error(409, "last_passkey")
        await conn.execute(text("DELETE FROM webauthn_credentials WHERE credential_id = :c"), {"c": row[0]})
        count = await _revoke(conn, me, keep=_current(ctx))  # that device's sessions end; this one stays
        detail = {"pid": pid, "revoked_sessions": count}
        await audit.record(conn, me, "self.credential_remove", str(me), detail)
    return JSONResponse({"revoked_sessions": count})
