"""SRV-AUTH persistence and ceremonies: users, bootstrap tokens, challenges, credentials, sessions, failures.

Secrets that reach the browser (session ids, ceremony ids, bootstrap tokens) are stored only as SHA-256 of
their text form (INV-AUTH-5). Transaction boundaries follow FR-AUTH-5..8: ceremonies that fail roll back and
record their failure in a separate transaction, except sign-in, whose challenge consumption commits with the
failure.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from webauthn import generate_authentication_options, generate_registration_options, options_to_json
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from reader.auth import webauthn as seam
from reader.auth.handles import skeleton
from reader.auth.policy import (
    ALGORITHMS,
    BOOTSTRAP_TOKEN_BYTES,
    BOOTSTRAP_TOKEN_TTL_S,
    CHALLENGE_BYTES,
    CHALLENGE_TTL_S,
    FAILURE_MAX,
    FAILURE_WINDOW_S,
    PENDING_TTL_S,
    SESSION_ID_BYTES,
    SESSION_TTL_S,
    SIGNUP_FAILURE_MAX,
    SIGNUP_FAILURE_WINDOW_S,
    SIGNUP_WINDOW_S,
    FailureReason,
    Settings,
)

Json = dict[str, Any]
log = logging.getLogger("reader.auth")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _secret_text(n_bytes: int) -> str:
    return b64url(secrets.token_bytes(n_bytes))


class BootstrapRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SessionUser:
    id: UUID
    handle: str
    role: str
    display_name: str
    consent_evaluation: bool
    consent_tuning: bool
    session_created_at: datetime
    session_age_s: float


# Startup -------------------------------------------------------------------------------------------------


async def cleanup(engine: AsyncEngine) -> None:
    """FR-AUTH-17: delete spent or expired ceremonies and tokens, ended sessions, failures past the window."""
    async with engine.begin() as conn:
        grace = {"grace": CHALLENGE_TTL_S}
        await conn.execute(
            text(
                "DELETE FROM auth_challenges WHERE expires_at < now() "
                "OR consumed_at < now() - make_interval(secs => :grace)"
            ),
            grace,
        )
        await conn.execute(
            text(
                "DELETE FROM bootstrap_tokens WHERE expires_at < now() "
                "OR consumed_at < now() - make_interval(secs => :grace) "
                "OR revoked_at < now() - make_interval(secs => :grace)"
            ),
            grace,
        )
        await conn.execute(text("DELETE FROM sessions WHERE expires_at < now() OR revoked_at IS NOT NULL"))
        await conn.execute(
            text("DELETE FROM auth_failures WHERE at < now() - make_interval(secs => :w)"),
            {"w": max(FAILURE_WINDOW_S, SIGNUP_FAILURE_WINDOW_S)},
        )
        # FR-AUTH-30: sign-up attempts past the window.
        await conn.execute(
            text("DELETE FROM signup_attempts WHERE at < now() - make_interval(secs => :w)"),
            {"w": SIGNUP_WINDOW_S},
        )
    await _expire_pending(engine)


async def _expire_pending(engine: AsyncEngine) -> None:
    """FR-AUTH-30: delete pending sign-ups older than the TTL; a row another transaction holds is skipped."""
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT id FROM users WHERE source = 'signup' AND state = 'pending' "
                    "AND created_at < now() - make_interval(secs => :ttl) FOR UPDATE SKIP LOCKED"
                ),
                {"ttl": PENDING_TTL_S},
            )
        ).scalars().all()
        for user_id in rows:
            await _delete_signup_user(conn, user_id)
            log.info("auth signup_expired %s", user_id)


async def _delete_signup_user(conn: AsyncConnection, user_id: UUID) -> None:
    """A pending sign-up owns only its credentials (FR-AUTH-29 reject, FR-AUTH-24, FR-AUTH-30)."""
    await conn.execute(text("DELETE FROM webauthn_credentials WHERE owner_id = :u"), {"u": user_id})
    await conn.execute(text("DELETE FROM sessions WHERE owner_id = :u"), {"u": user_id})
    await conn.execute(text("DELETE FROM users WHERE id = :u AND source = 'signup'"), {"u": user_id})


class HandleTakenBySignup(Exception):
    """FR-AUTH-24: a `users[]` handle equals an active or disabled sign-up account's handle."""

    def __init__(self, handle: str) -> None:
        super().__init__("handle_taken_by_signup")
        self.handle = handle


async def sync_users(engine: AsyncEngine, settings: Settings) -> list[UUID]:
    """FR-AUTH-1 and FR-AUTH-24: one row per configured user; configured users no longer listed are disabled,
    their rows kept. A pending sign-up whose handle a `users[]` entry names exactly is deleted (returned); an
    active or disabled one stops the sync (HandleTakenBySignup, the first in `users[]` order) and nothing
    changes."""
    handles = [u.handle for u in settings.users]
    superseded: list[UUID] = []
    async with engine.begin() as conn:
        for user in settings.users:
            row = (
                await conn.execute(
                    text(
                        "SELECT id, state, disabled_at FROM users WHERE handle = :h AND source = 'signup' "
                        "FOR UPDATE"
                    ),
                    {"h": user.handle},
                )
            ).first()
            if row is None:
                continue
            if row.state != "pending" or row.disabled_at is not None:
                raise HandleTakenBySignup(user.handle)
            await _delete_signup_user(conn, row.id)
            superseded.append(row.id)
        for user in settings.users:
            await conn.execute(
                text(
                    "INSERT INTO users (handle, role, webauthn_user_id, display_name, consent_evaluation, "
                    "handle_skeleton) VALUES (:handle, :role, :uid, :handle, :consent, :skeleton) "
                    "ON CONFLICT (handle) DO UPDATE SET role = excluded.role, disabled_at = NULL, "
                    "handle_skeleton = excluded.handle_skeleton WHERE users.source = 'config'"
                ),
                {
                    "handle": user.handle,
                    "role": user.role,
                    "uid": secrets.token_bytes(64),
                    "consent": user.role == "owner",
                    "skeleton": skeleton(user.handle),
                },
            )
        await conn.execute(
            text(
                "UPDATE users SET disabled_at = now() WHERE source = 'config' AND disabled_at IS NULL "
                "AND handle <> ALL(:handles)"
            ),
            {"handles": handles},
        )
        await conn.execute(
            text(
                "UPDATE sessions SET revoked_at = now() WHERE revoked_at IS NULL AND owner_id IN "
                "(SELECT id FROM users WHERE disabled_at IS NOT NULL)"
            )
        )
    return superseded


async def prepare(engine: AsyncEngine, settings: Settings) -> list[UUID]:
    """The api startup work after migrations: cleanup, then the user sync. Returns the superseded pending
    sign-ups, which the api logs once serving starts (FR-AUTH-24; best-effort, owner note 5)."""
    await cleanup(engine)
    return await sync_users(engine, settings)


# Failures ------------------------------------------------------------------------------------------------

Scope = Literal["auth", "signup"]


async def record_failure(engine: AsyncEngine, reason: FailureReason, scope: Scope = "auth") -> None:
    async with engine.begin() as conn:
        await _insert_failure(conn, reason, scope)


async def _insert_failure(conn: AsyncConnection, reason: FailureReason, scope: Scope = "auth") -> None:
    await conn.execute(
        text("INSERT INTO auth_failures (reason, scope) VALUES (:r, :s)"), {"r": reason.value, "s": scope}
    )


async def retry_after(engine: AsyncEngine, scope: Scope = "auth") -> int | None:
    """FR-AUTH-11 (scope `auth`) and FR-AUTH-26 step 1 (scope `signup`): seconds until the oldest counted
    failure leaves the window, while that scope's limit is reached."""
    if scope == "auth":
        limit, window = FAILURE_MAX, FAILURE_WINDOW_S
    else:
        limit, window = SIGNUP_FAILURE_MAX, SIGNUP_FAILURE_WINDOW_S
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT count(*), extract(epoch FROM min(at) + make_interval(secs => :w) - now()) "
                    "FROM auth_failures WHERE scope = :s AND at > now() - make_interval(secs => :w)"
                ),
                {"w": window, "s": scope},
            )
        ).one()
    count, remaining = int(row[0]), row[1]
    if count < limit:
        return None
    return max(1, math.ceil(float(remaining)))


# Bootstrap tokens (FR-AUTH-4) ----------------------------------------------------------------------------


async def create_bootstrap_token(engine: AsyncEngine, settings: Settings, handle: str | None) -> str:
    target = handle or settings.owner_handle
    token = _secret_text(BOOTSTRAP_TOKEN_BYTES)
    async with engine.begin() as conn:
        user = (
            await conn.execute(
                text("SELECT id, disabled_at, source FROM users WHERE handle = :h FOR UPDATE"), {"h": target}
            )
        ).first()
        if user is None:
            raise BootstrapRefused("unknown_handle")
        if user.source == "signup":  # FR-AUTH-24: sign-up users registered their passkey through sign-up
            raise BootstrapRefused("signup_user")
        if user.disabled_at is not None:
            raise BootstrapRefused(FailureReason.user_disabled.value)
        if await _has_credential(conn, user.id):
            raise BootstrapRefused(FailureReason.owner_has_credential.value)
        await conn.execute(
            text(
                "UPDATE bootstrap_tokens SET revoked_at = now() "
                "WHERE user_id = :u AND consumed_at IS NULL AND revoked_at IS NULL"
            ),
            {"u": user.id},
        )
        await conn.execute(
            text(
                "INSERT INTO bootstrap_tokens (token_sha256, user_id, expires_at) "
                "VALUES (:h, :u, now() + make_interval(secs => :ttl))"
            ),
            {"h": digest(token), "u": user.id, "ttl": BOOTSTRAP_TOKEN_TTL_S},
        )
    return token


async def _has_credential(conn: AsyncConnection, user_id: UUID) -> bool:
    found = await conn.execute(
        text("SELECT 1 FROM webauthn_credentials WHERE owner_id = :u LIMIT 1"), {"u": user_id}
    )
    return found.first() is not None


# Ceremonies ----------------------------------------------------------------------------------------------


async def _new_ceremony(
    conn: AsyncConnection, kind: str, user_id: UUID | None, token_sha: bytes | None = None
) -> tuple[str, bytes]:
    ceremony = _secret_text(32)
    challenge = secrets.token_bytes(CHALLENGE_BYTES)
    await conn.execute(
        text(
            "INSERT INTO auth_challenges (ceremony_sha256, kind, challenge, user_id, bootstrap_token_sha256, "
            "expires_at) VALUES (:c, :k, :ch, :u, :t, now() + make_interval(secs => :ttl))"
        ),
        {
            "c": digest(ceremony),
            "k": kind,
            "ch": challenge,
            "u": user_id,
            "t": token_sha,
            "ttl": CHALLENGE_TTL_S,
        },
    )
    return ceremony, challenge


async def _consume_ceremony(conn: AsyncConnection, ceremony: str | None, kind: str) -> Row[Any] | None:
    if not ceremony:
        return None
    return (
        await conn.execute(
            text(
                "UPDATE auth_challenges SET consumed_at = now() WHERE ceremony_sha256 = :c AND kind = :k "
                "AND consumed_at IS NULL AND expires_at > now() "
                "RETURNING challenge, user_id, bootstrap_token_sha256"
            ),
            {"c": digest(ceremony), "k": kind},
        )
    ).first()


async def _registration_options(
    conn: AsyncConnection, settings: Settings, user_id: UUID, challenge: bytes
) -> Json:
    user = (
        await conn.execute(
            text("SELECT handle, display_name, webauthn_user_id FROM users WHERE id = :u"), {"u": user_id}
        )
    ).one()
    existing = (
        (
            await conn.execute(
                text(
                    "SELECT credential_id FROM webauthn_credentials WHERE owner_id = :u ORDER BY created_at"
                ),
                {"u": user_id},
            )
        )
        .scalars()
        .all()
    )
    options = generate_registration_options(
        rp_id=settings.rp_id,
        rp_name="reader-ai",
        user_name=user.handle,
        user_id=bytes(user.webauthn_user_id),
        user_display_name=user.display_name,
        challenge=challenge,
        timeout=CHALLENGE_TTL_S * 1000,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            require_resident_key=True,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=bytes(c)) for c in existing],
        supported_pub_key_algs=[COSEAlgorithmIdentifier(a) for a in ALGORITHMS],
    )
    parsed: Json = json.loads(options_to_json(options))
    return parsed


async def _store_credential(conn: AsyncConnection, user_id: UUID, cred: seam.RegisteredCredential) -> bool:
    inserted = await conn.execute(
        text(
            "INSERT INTO webauthn_credentials (credential_id, owner_id, public_key, sign_count, transports, "
            "aaguid, backed_up) VALUES (:id, :u, :pk, :n, :t, :a, :b) ON CONFLICT (credential_id) DO NOTHING "
            "RETURNING 1"
        ),
        {
            "id": cred.credential_id,
            "u": user_id,
            "pk": cred.public_key,
            "n": cred.sign_count,
            "t": list(cred.transports),
            "a": UUID(cred.aaguid),
            "b": cred.backed_up,
        },
    )
    return inserted.first() is not None


async def _start_session(conn: AsyncConnection, user_id: UUID, presented: str | None) -> str:
    """FR-AUTH-8: a fresh session; the session presented with the request is revoked."""
    if presented:
        await conn.execute(
            text("UPDATE sessions SET revoked_at = now() WHERE id_sha256 = :h AND revoked_at IS NULL"),
            {"h": digest(presented)},
        )
    session = _secret_text(SESSION_ID_BYTES)
    await conn.execute(
        text(
            "INSERT INTO sessions (id_sha256, owner_id, expires_at) "
            "VALUES (:h, :u, now() + make_interval(secs => :ttl))"
        ),
        {"h": digest(session), "u": user_id, "ttl": SESSION_TTL_S},
    )
    return session


class _Rollback(Exception):
    def __init__(self, reason: FailureReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


async def bootstrap_options(
    engine: AsyncEngine, settings: Settings, token: str
) -> tuple[Json, str] | FailureReason:
    try:
        token_sha = digest(token)
    except UnicodeEncodeError:  # e.g. a lone surrogate from JSON "\ud800"
        return FailureReason.token_invalid
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT t.user_id, u.disabled_at FROM bootstrap_tokens t "
                    "JOIN users u ON u.id = t.user_id "
                    "WHERE t.token_sha256 = :h AND t.consumed_at IS NULL AND t.revoked_at IS NULL "
                    "AND t.expires_at > now()"
                ),
                {"h": token_sha},
            )
        ).first()
        if row is None:
            return FailureReason.token_invalid
        if row.disabled_at is not None:
            return FailureReason.user_disabled
        if await _has_credential(conn, row.user_id):
            return FailureReason.owner_has_credential
        ceremony, challenge = await _new_ceremony(conn, "bootstrap", row.user_id, token_sha)
        return await _registration_options(conn, settings, row.user_id, challenge), ceremony


async def bootstrap_verify(
    engine: AsyncEngine, settings: Settings, ceremony: str | None, credential: Json, presented: str | None
) -> str | FailureReason:
    """FR-AUTH-5: one transaction; any failure rolls it back so the token and challenge stay usable."""
    try:
        async with engine.begin() as conn:
            pending = (
                await conn.execute(
                    text(
                        "SELECT user_id, bootstrap_token_sha256 FROM auth_challenges "
                        "WHERE ceremony_sha256 = :c "
                        "AND kind = 'bootstrap' AND consumed_at IS NULL AND expires_at > now()"
                    ),
                    {"c": digest(ceremony or "")},
                )
            ).first()
            if pending is None:
                raise _Rollback(FailureReason.ceremony_invalid)
            user = (
                await conn.execute(
                    text("SELECT id, disabled_at FROM users WHERE id = :u FOR UPDATE"), {"u": pending.user_id}
                )
            ).one()
            if user.disabled_at is not None:
                raise _Rollback(FailureReason.user_disabled)
            if await _has_credential(conn, user.id):
                raise _Rollback(FailureReason.owner_has_credential)
            token = (
                await conn.execute(
                    text(
                        "UPDATE bootstrap_tokens SET consumed_at = now() "
                        "WHERE token_sha256 = :h AND user_id = :u "
                        "AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at > now() RETURNING 1"
                    ),
                    {"h": pending.bootstrap_token_sha256, "u": user.id},
                )
            ).first()
            if token is None:
                raise _Rollback(FailureReason.token_invalid)
            consumed = await _consume_ceremony(conn, ceremony, "bootstrap")
            if consumed is None:
                raise _Rollback(FailureReason.ceremony_invalid)
            session = await _register(
                conn, settings, user.id, bytes(consumed.challenge), credential, presented
            )
            return session
    except _Rollback as failure:
        await record_failure(engine, failure.reason)
        return failure.reason


async def _register(
    conn: AsyncConnection,
    settings: Settings,
    user_id: UUID,
    challenge: bytes,
    credential: Json,
    presented: str | None,
) -> str:
    result = seam.verify_registration(settings.rp_id, settings.origin, challenge, credential)
    if isinstance(result, FailureReason):
        raise _Rollback(result)
    if not await _store_credential(conn, user_id, result):
        raise _Rollback(FailureReason.credential_id_taken)
    return await _start_session(conn, user_id, presented)


async def register_options(engine: AsyncEngine, settings: Settings, user: SessionUser) -> tuple[Json, str]:
    async with engine.begin() as conn:
        ceremony, challenge = await _new_ceremony(conn, "register", user.id)
        return await _registration_options(conn, settings, user.id, challenge), ceremony


async def register_verify(
    engine: AsyncEngine,
    settings: Settings,
    user: SessionUser,
    ceremony: str | None,
    credential: Json,
    presented: str | None,
) -> str | FailureReason:
    """FR-AUTH-6: bound to the signed-in user; failure rolls back (the challenge stays usable)."""
    try:
        async with engine.begin() as conn:
            consumed = await _consume_ceremony(conn, ceremony, "register")
            if consumed is None or consumed.user_id != user.id:
                raise _Rollback(FailureReason.ceremony_invalid)
            return await _register(conn, settings, user.id, bytes(consumed.challenge), credential, presented)
    except _Rollback as failure:
        await record_failure(engine, failure.reason)
        return failure.reason


async def signin_options(engine: AsyncEngine, settings: Settings) -> tuple[Json, str]:
    async with engine.begin() as conn:
        ceremony, challenge = await _new_ceremony(conn, "authenticate", None)
    options = generate_authentication_options(
        rp_id=settings.rp_id,
        challenge=challenge,
        timeout=CHALLENGE_TTL_S * 1000,
        allow_credentials=[],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    parsed: Json = json.loads(options_to_json(options))
    return parsed, ceremony


def _raw_id(credential: Json) -> bytes | None:
    raw = credential.get("rawId") if isinstance(credential, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except ValueError:
        return None


async def signin_verify(
    engine: AsyncEngine, settings: Settings, ceremony: str | None, credential: Json, presented: str | None
) -> str | FailureReason | PendingApproval:
    """FR-AUTH-7, two transactions: (1) consume the challenge, verify, and on failure record it, always
    committed; (2) on one connection, the conditional counter update and the new session, or a rollback.
    FR-AUTH-28: a pending user's valid assertion ends after (1): no failure row, no counter update, no
    session."""
    async with engine.begin() as conn:
        checked = await _signin_check(conn, settings, ceremony, credential)
        if isinstance(checked, FailureReason):
            await _insert_failure(conn, checked)
            return checked
    if isinstance(checked, PendingApproval):
        return checked
    authenticated, owner_id = checked
    try:
        async with engine.begin() as conn:
            if not await finish_signin(
                conn, authenticated.credential_id, authenticated.new_sign_count, authenticated.backed_up
            ):
                raise _Rollback(FailureReason.counter_regressed)
            return await _start_session(conn, owner_id, presented)
    except _Rollback as failure:
        await record_failure(engine, failure.reason)
        return failure.reason


async def _signin_check(
    conn: AsyncConnection, settings: Settings, ceremony: str | None, credential: Json
) -> tuple[seam.Authenticated, UUID] | FailureReason | PendingApproval:
    consumed = await _consume_ceremony(conn, ceremony, "authenticate")
    if consumed is None:
        return FailureReason.ceremony_invalid
    raw_id = _raw_id(credential)
    if raw_id is None:
        return FailureReason.malformed
    row = (
        await conn.execute(
            text(
                "SELECT c.public_key, c.sign_count, c.owner_id, u.webauthn_user_id, "
                "u.disabled_at, u.state FROM webauthn_credentials c JOIN users u ON u.id = c.owner_id "
                "WHERE c.credential_id = :id"
            ),
            {"id": raw_id},
        )
    ).first()
    if row is None:
        return FailureReason.unknown_credential
    if row.disabled_at is not None:
        return FailureReason.user_disabled
    result = seam.verify_authentication(
        settings.rp_id,
        settings.origin,
        bytes(consumed.challenge),
        credential,
        bytes(row.public_key),
        int(row.sign_count),
        bytes(row.webauthn_user_id),
    )
    if isinstance(result, FailureReason):
        return result
    owner_id: UUID = row.owner_id
    if row.state == "pending":  # FR-AUTH-28: after the signature, so only the passkey's holder learns it
        return PendingApproval(owner_id)
    return result, owner_id


@dataclass(frozen=True)
class PendingApproval:
    """FR-AUTH-28: a valid assertion of a pending sign-up user. Not a failure: no `auth_failures` row."""

    user_id: UUID


async def finish_signin(conn: AsyncConnection, credential_id: bytes, new_count: int, backed_up: bool) -> bool:
    """The conditional counter update of FR-AUTH-7; False when another sign-in already moved the counter."""
    updated = await conn.execute(
        text(
            "UPDATE webauthn_credentials SET sign_count = :n, last_used_at = now(), backed_up = :b "
            "WHERE credential_id = :id AND (sign_count < :n OR (sign_count = 0 AND :n = 0)) RETURNING 1"
        ),
        {"id": credential_id, "n": new_count, "b": backed_up},
    )
    return updated.first() is not None


# Sessions ------------------------------------------------------------------------------------------------


async def session_user(engine: AsyncEngine, session: str | None) -> SessionUser | None:
    if not session:
        return None
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT u.id, u.handle, u.role, u.display_name, u.consent_evaluation, u.consent_tuning, "
                    "s.created_at, extract(epoch FROM now() - s.created_at) AS age FROM sessions s "
                    "JOIN users u ON u.id = s.owner_id WHERE s.id_sha256 = :h AND s.revoked_at IS NULL "
                    "AND s.expires_at > now() AND u.disabled_at IS NULL AND u.state = 'active'"
                ),
                {"h": digest(session)},
            )
        ).first()
    if row is None:
        return None
    return SessionUser(
        row.id,
        row.handle,
        row.role,
        row.display_name,
        row.consent_evaluation,
        row.consent_tuning,
        row.created_at,
        float(row.age),
    )


async def revoke_session(engine: AsyncEngine, session: str | None) -> None:
    if not session:
        return
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET revoked_at = now() WHERE id_sha256 = :h AND revoked_at IS NULL"),
            {"h": digest(session)},
        )
