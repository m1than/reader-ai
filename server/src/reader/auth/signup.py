"""Open sign-up with owner approval (ADR-0031; SRV-AUTH FR-AUTH-26..30).

Options checks the limits and the rules and opens a `signup` ceremony that carries the handle, the
display name and a fresh `webauthn_user_id`; no user row exists until verify. Verify checks the passkey
through the seam first, then, under one global lock, re-checks the cap and the handle and creates a
pending learner with its credential. No route here starts a session. The owner's routes approve, reject
or disable sign-up accounts under a row lock.
"""

from __future__ import annotations

import json
import logging
import math
import secrets
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from webauthn import generate_registration_options, options_to_json
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from reader.auth import webauthn as seam
from reader.auth.handles import display_name, handle_candidate_ok, skeleton
from reader.auth.policy import (
    ALGORITHMS,
    AUTH_LOCK_NAMESPACE,
    CHALLENGE_BYTES,
    CHALLENGE_TTL_S,
    PENDING_MAX,
    SIGNUP_ATTEMPT_LOCK,
    SIGNUP_COMMIT_LOCK,
    SIGNUP_MAX,
    SIGNUP_WINDOW_S,
    FailureReason,
    Settings,
)
from reader.auth.store import Json, _secret_text, _store_credential, digest, record_failure

log = logging.getLogger("reader.auth")

Refusal = Literal["rate_limited", "display_name_invalid", "signup_full", "handle_unavailable"]


@dataclass(frozen=True)
class Refused:
    code: Refusal
    retry_after: int | None = None


async def _lock(conn: AsyncConnection, key: int) -> None:
    await conn.execute(text("SELECT pg_advisory_xact_lock(:ns, :k)"), {"ns": AUTH_LOCK_NAMESPACE, "k": key})


async def _pending_count(conn: AsyncConnection) -> int:
    found = await conn.execute(text("SELECT count(*) FROM users WHERE state = 'pending'"))
    return int(found.scalar_one())


async def _handle_free(conn: AsyncConnection, handle: str) -> bool:
    """FR-AUTH-25 against every `users` row (any source, any state, disabled included)."""
    taken = await conn.execute(
        text("SELECT 1 FROM users WHERE handle = :h OR handle_skeleton = :k LIMIT 1"),
        {"h": handle, "k": skeleton(handle)},
    )
    return taken.first() is None


async def _attempt(engine: AsyncEngine) -> Refused | None:
    """FR-AUTH-26 steps (2) and (3) under one lock: refuse at the global limit, else add the attempt row."""
    async with engine.begin() as conn:
        await _lock(conn, SIGNUP_ATTEMPT_LOCK)
        row = (
            await conn.execute(
                text(
                    "SELECT count(*), extract(epoch FROM min(at) + make_interval(secs => :w) - now()) "
                    "FROM signup_attempts WHERE at > now() - make_interval(secs => :w)"
                ),
                {"w": SIGNUP_WINDOW_S},
            )
        ).one()
        if int(row[0]) >= SIGNUP_MAX:
            return Refused("rate_limited", max(1, math.ceil(float(row[1]))))
        await conn.execute(text("INSERT INTO signup_attempts DEFAULT VALUES"))
    return None


def _registration_options(
    handle: str, name: str, user_id: bytes, challenge: bytes, settings: Settings
) -> Json:
    generated = generate_registration_options(
        rp_id=settings.rp_id,
        rp_name="reader-ai",
        user_name=handle,
        user_id=user_id,
        user_display_name=name,
        challenge=challenge,
        timeout=CHALLENGE_TTL_S * 1000,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            require_resident_key=True,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[],
        supported_pub_key_algs=[COSEAlgorithmIdentifier(a) for a in ALGORITHMS],
    )
    parsed: Json = json.loads(options_to_json(generated))
    return parsed


async def options(engine: AsyncEngine, settings: Settings, body: Any) -> tuple[Json, str] | Refused:
    """FR-AUTH-26 steps (2) to (7); step (1), the sign-up failure limit, is the route's."""
    refused = await _attempt(engine)  # committed whatever follows (step 3)
    if refused is not None:
        return refused
    fields = body if isinstance(body, dict) else {}
    raw_handle = fields.get("handle")
    name = display_name(fields.get("display_name"))
    if name is None:
        return Refused("display_name_invalid" if isinstance(raw_handle, str) else "handle_unavailable")
    async with engine.begin() as conn:
        if await _pending_count(conn) >= PENDING_MAX:
            return Refused("signup_full")
        if not isinstance(raw_handle, str) or not handle_candidate_ok(raw_handle):
            return Refused("handle_unavailable")
        handle = raw_handle
        if not await _handle_free(conn, handle):
            return Refused("handle_unavailable")
        ceremony = _secret_text(32)
        challenge = secrets.token_bytes(CHALLENGE_BYTES)
        user_id = secrets.token_bytes(64)
        await conn.execute(
            text(
                "INSERT INTO auth_challenges (ceremony_sha256, kind, challenge, signup_handle, "
                "signup_display_name, signup_webauthn_user_id, expires_at) "
                "VALUES (:c, 'signup', :ch, :h, :n, :u, now() + make_interval(secs => :ttl))"
            ),
            {
                "c": digest(ceremony),
                "ch": challenge,
                "h": handle,
                "n": name,
                "u": user_id,
                "ttl": CHALLENGE_TTL_S,
            },
        )
    return _registration_options(handle, name, user_id, challenge, settings), ceremony


class _Rollback(Exception):
    def __init__(self, outcome: FailureReason | Refused) -> None:
        super().__init__("rollback")
        self.outcome = outcome


async def verify(
    engine: AsyncEngine, settings: Settings, ceremony: str | None, credential: Json
) -> UUID | FailureReason | Refused:
    """FR-AUTH-27: one transaction; the seam before the lock; a pending learner and its credential; no
    session."""
    try:
        async with engine.begin() as conn:
            consumed = (
                await conn.execute(
                    text(
                        "UPDATE auth_challenges SET consumed_at = now() WHERE ceremony_sha256 = :c "
                        "AND kind = 'signup' AND consumed_at IS NULL AND expires_at > now() "
                        "RETURNING challenge, signup_handle, signup_display_name, signup_webauthn_user_id"
                    ),
                    {"c": digest(ceremony or "")},
                )
            ).first()
            if not ceremony or consumed is None:
                raise _Rollback(FailureReason.ceremony_invalid)
            result = seam.verify_registration(
                settings.rp_id, settings.origin, bytes(consumed.challenge), credential
            )
            if isinstance(result, FailureReason):
                raise _Rollback(result)
            await _lock(conn, SIGNUP_COMMIT_LOCK)
            if await _pending_count(conn) >= PENDING_MAX:
                raise _Rollback(Refused("signup_full"))
            handle = str(consumed.signup_handle)
            if not await _handle_free(conn, handle):
                raise _Rollback(Refused("handle_unavailable"))
            # The role, source and state are literals: no request field reaches them (INV-AUTH-10).
            user_id: UUID = (
                await conn.execute(
                    text(
                        "INSERT INTO users (handle, role, webauthn_user_id, display_name, "
                        "consent_evaluation, consent_tuning, source, state, handle_skeleton) "
                        "VALUES (:h, 'learner', :u, :n, false, false, 'signup', 'pending', :k) RETURNING id"
                    ),
                    {
                        "h": handle,
                        "u": bytes(consumed.signup_webauthn_user_id),
                        "n": consumed.signup_display_name,
                        "k": skeleton(handle),
                    },
                )
            ).scalar_one()
            if not await _store_credential(conn, user_id, result):
                raise _Rollback(FailureReason.credential_id_taken)
    except _Rollback as failure:
        if isinstance(failure.outcome, FailureReason):
            await record_failure(engine, failure.outcome, "signup")
        return failure.outcome
    log.info("auth signup_pending %s", user_id)
    return user_id


# Owner routes (FR-AUTH-29) ----------------------------------------------------------------------------------


def _row(r: Any) -> Json:
    return {
        "id": str(r.id),
        "handle": r.handle,
        "display_name": r.display_name,
        "requested_at": r.created_at.isoformat(),
    }


async def listing(engine: AsyncEngine) -> Json:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT id, handle, display_name, created_at, state, disabled_at FROM users "
                    "WHERE source = 'signup' ORDER BY created_at, handle"
                )
            )
        ).all()
    pending = [_row(r) for r in rows if r.state == "pending"]
    active = sorted((r for r in rows if r.state == "active"), key=lambda r: str(r.handle))
    accounts = [{**_row(r), "disabled": r.disabled_at is not None} for r in active]
    return {"pending": pending, "accounts": accounts}


Action = Literal["approve", "reject", "disable"]


def _uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


async def act(engine: AsyncEngine, raw_id: str, action: Action) -> bool:
    """Lock the sign-up row, re-check its state, act; False (404) for any other target."""
    target = _uuid(raw_id)
    if target is None:
        return False
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT id, handle, state, disabled_at FROM users WHERE id = :u AND source = 'signup' "
                    "FOR UPDATE"
                ),
                {"u": target},
            )
        ).first()
        if row is None:
            return False
        if action == "approve":
            if row.state != "pending":
                return False
            await conn.execute(text("UPDATE users SET state = 'active' WHERE id = :u"), {"u": target})
        elif action == "reject":
            if row.state != "pending":
                return False
            # Open ceremonies for this handle are cancelled: none recreates the account without new options.
            await conn.execute(
                text(
                    "DELETE FROM auth_challenges WHERE kind = 'signup' AND signup_handle = :h "
                    "AND consumed_at IS NULL"
                ),
                {"h": row.handle},
            )
            await conn.execute(text("DELETE FROM webauthn_credentials WHERE owner_id = :u"), {"u": target})
            await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": target})
        else:
            if row.state != "active" or row.disabled_at is not None:
                return False
            await conn.execute(text("UPDATE users SET disabled_at = now() WHERE id = :u"), {"u": target})
            await conn.execute(
                text("UPDATE sessions SET revoked_at = now() WHERE owner_id = :u AND revoked_at IS NULL"),
                {"u": target},
            )
    log.info("auth signup_%s %s", action, target)
    return True
