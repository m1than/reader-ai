"""Helpers for the manage amendment's tests (SRV-AUTH FR-AUTH-40..48, INV-AUTH-20..21).

Users, sessions and credentials are written directly where the test is not about the ceremony; the modules
under test are imported inside functions.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from auth_helpers import ORIGIN, execute, ready_engine, scalar, settings_for
from conftest import Pg
from signup_helpers import SESSION, insert_pending, user_id

SNAPSHOT_SQL = (
    "SELECT (SELECT coalesce(array_agg(row(id, handle, role, source, state, disabled_at)::text ORDER BY id),"
    " '{}') FROM users),"
    " (SELECT coalesce(array_agg(row(id_sha256, owner_id, revoked_at)::text ORDER BY id_sha256), '{}')"
    " FROM sessions),"
    " (SELECT coalesce(array_agg(row(credential_id, owner_id, label)::text ORDER BY credential_id), '{}')"
    " FROM webauthn_credentials),"
    " (SELECT count(*) FROM audit_log)"
)


async def setup(pg: Pg, tmp_path: Path) -> tuple[Any, Any]:
    s = settings_for(tmp_path)
    return s, await ready_engine(pg, s)


def sid_of(session: str) -> str:
    return hashlib.sha256(session.encode("utf-8")).hexdigest()[:16]


def pid_of(credential_id: bytes) -> str:
    return hashlib.sha256(credential_id).hexdigest()[:16]


async def new_session(engine: Any, uid: Any, age_s: int = 0) -> str:
    from reader.auth.store import _start_session

    async with engine.begin() as conn:
        session = await _start_session(conn, uid, None)
    if age_s:
        await execute(
            engine,
            "UPDATE sessions SET created_at = now() - make_interval(secs => :a) WHERE id_sha256 = :h",
            a=age_s,
            h=hashlib.sha256(session.encode("utf-8")).digest(),
        )
    return session


async def add_credential(engine: Any, uid: Any, *, label: str | None = None, used: bool = False) -> str:
    """A credential row (not a usable passkey); returns its pid."""
    cred = secrets.token_bytes(32)
    await execute(
        engine,
        "INSERT INTO webauthn_credentials (credential_id, owner_id, public_key, sign_count, aaguid,"
        " backed_up, transports, label, last_used_at) VALUES (:c, :u, :pk, 0,"
        " '00000000-0000-0000-0000-000000000000', true, ARRAY['internal'], :l,"
        " CASE WHEN :used THEN now() ELSE NULL END)",
        c=cred,
        u=uid,
        pk=b"pk",
        l=label,
        used=used,
    )
    return pid_of(cred)


async def owner(engine: Any) -> tuple[Any, str, str]:
    """The configured owner `ivan` with one credential and a fresh session: (id, session, pid)."""
    uid = await user_id(engine, "ivan")
    pid = await add_credential(engine, uid)
    return uid, await new_session(engine, uid), pid


async def learner(engine: Any, handle: str = "lc-anna") -> tuple[Any, str, str]:
    """An active sign-up learner with one credential (insert_pending's) and a fresh session."""
    uid = await insert_pending(engine, handle, state="active")
    cred = await scalar(engine, "SELECT credential_id FROM webauthn_credentials WHERE owner_id = :u", u=uid)
    return uid, await new_session(engine, uid), pid_of(bytes(cred))


@asynccontextmanager
async def as_user(settings: Any, engine: Any, session: str | None) -> AsyncIterator[httpx.AsyncClient]:
    from reader.api.app import create_app

    app = create_app(settings, engine, run_cleanup_task=False)
    cookies = {SESSION: session} if session else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}, cookies=cookies
    ) as c:
        yield c


async def snapshot(engine: Any) -> Any:
    from sqlalchemy import text

    async with engine.connect() as conn:
        return tuple((await conn.execute(text(SNAPSHOT_SQL))).one())


async def audit_rows(engine: Any) -> list[tuple[str, str, Any]]:
    from sqlalchemy import text

    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT action, object_id, detail FROM audit_log ORDER BY id"))
        return [(r[0], r[1], r[2]) for r in rows]


async def live_sessions(engine: Any, uid: Any) -> int:
    return int(
        await scalar(
            engine,
            "SELECT count(*) FROM sessions WHERE owner_id = :u AND revoked_at IS NULL AND expires_at > now()",
            u=uid,
        )
    )


async def credentials(engine: Any, uid: Any) -> int:
    return int(await scalar(engine, "SELECT count(*) FROM webauthn_credentials WHERE owner_id = :u", u=uid))


async def course_for(engine: Any, uid: Any, tmp: Path) -> tuple[Any, Any, str]:
    """One course with one lesson and one revision owned by `uid` (data the user owns)."""
    from reader.lessons import store

    blobs = tmp / "blobs"
    blobs.mkdir(exist_ok=True)
    notebook = (Path(store.__file__).parent / "fixtures" / "F01.ipynb").read_bytes()
    async with engine.begin() as conn:
        course = await store.create_course(conn, uid, "A course", "c" * 64)
        lesson = await store.ensure_lesson(conn, uid, course, 1, "Lesson")
        sha, _ = await store.write_revision(conn, blobs, uid, lesson, notebook, "draft", {})
    return course, lesson, sha
