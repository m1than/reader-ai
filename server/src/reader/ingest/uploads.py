"""The `uploads` state machine (SRV-INGEST FR-INGEST-19, -20, -23; VEC-INGEST-6).

Stored states: queued → claimed → converting → sending → received, or failed. `importing`, `indexing`,
`ready` and job failures are derived from the SRV-EVENTS keys. Every transition is a guarded UPDATE; the
import key is authoritative: once it exists, a row in claimed, converting, sending or failed becomes
`received` and the requested transition is refused. Lease expiry is applied by the stack itself on every
command and every api read.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

import reader.ingest as ingest
from reader.ingest import params, store
from reader.ingest.jobs import KIND, key_for
from reader.jobs.queue import job_status_of

log = logging.getLogger("reader.ingest")

LIVE: Final = ("claimed", "converting", "sending")
OPEN: Final = ("queued", *LIVE, "failed")
UNDER_AUTHORITY: Final = ("queued", *LIVE, "failed")  # `queued`: code review round 1
EMBED_KIND: Final = "search.embed"
VIEW_KEYS: Final = (
    "id", "material_id", "filename", "size", "language", "state", "code", "pages_done", "pages_total",
    "attempts", "created_at", "retry",
)


class Busy(Exception):
    """The instance's upload lock is held (409 `upload_busy`)."""


class StateRefused(Exception):
    """The requested owner transition is not allowed from the current state (409 `state`)."""


def material_id(sha256: str) -> str:
    return f"up-{sha256[:12]}"


# ---- VEC-INGEST-6: the derived state ----


def _by_embed(embed_status: str, embed_code: str | None) -> tuple[str, str | None, str | None]:
    if embed_status == "succeeded":
        return "ready", None, None
    if embed_status == "dead":
        return "failed", embed_code, "requeue"
    return "indexing", None, None


def derive(
    *,
    row: str,
    attempts: int,
    expired: bool,
    row_code: str | None,
    import_status: str,
    import_code: str | None,
    ingest: bool,
    embed_status: str,
    embed_code: str | None,
) -> tuple[str, str | None, str | None]:
    """(state, code, retry) of an upload view; an existing import key wins over the row's state."""
    if import_status != "none":
        if import_status in ("queued", "running"):
            return "importing", None, None
        if import_status == "dead":
            return "failed", import_code, "requeue"
        return _by_embed(embed_status, embed_code)
    if row == "received":
        return _by_embed(embed_status, embed_code) if ingest else ("failed", "import_missing", "reconvert")
    if row in LIVE and expired:
        lost = attempts + 1 >= params.ATTEMPTS_MAX
        return ("failed", "host_lost", "reconvert") if lost else ("queued", None, None)
    if row == "failed":
        return "failed", row_code, "reconvert"
    return row, None, None


# ---- the keys that count ----


async def import_key(conn: AsyncConnection, sha256: str) -> str:
    """The `ingest.import` key: (sha256, the material's frozen version or CONVERTER_VERSION)."""
    return key_for(sha256, await store.frozen_version(conn, sha256) or ingest.CONVERTER_VERSION)


async def _key_code(conn: AsyncConnection, kind: str, key: str) -> tuple[bool, str | None]:
    row = (
        await conn.execute(
            text("SELECT error_code FROM job_keys WHERE kind = :k AND key = :key"), {"k": kind, "key": key}
        )
    ).first()
    return (row is not None, None if row is None else row[0])


async def key_exists(conn: AsyncConnection, sha256: str) -> bool:
    return (await _key_code(conn, KIND, await import_key(conn, sha256)))[0]


async def _ingest_id(conn: AsyncConnection, sha256: str) -> uuid.UUID | None:
    row = (
        await conn.execute(
            text("SELECT i.id FROM ingests i JOIN materials m ON m.id = i.material_id WHERE m.sha256 = :s"),
            {"s": sha256},
        )
    ).first()
    return None if row is None else row[0]


async def embed_key(conn: AsyncConnection, sha256: str) -> str | None:
    """SRV-SEARCH FR-SEARCH-5's key of the material's frozen ingest under the current params, if any."""
    ingest_id = await _ingest_id(conn, sha256)
    if ingest_id is None:
        return None
    from reader.jobs.model import idempotency_key
    from reader.search.jobs import JOB_KINDS, embed_args
    from reader.search.store import fts_config_of

    args = embed_args(sha256, ingest_id, await fts_config_of(conn, sha256, ingest_id))
    return idempotency_key(JOB_KINDS[0], args.model_dump(mode="json"))


async def _status(conn: AsyncConnection, kind: str, key: str | None) -> tuple[str, str | None]:
    if key is None:
        return "none", None
    status = await job_status_of(conn, kind, key)
    return status, (await _key_code(conn, kind, key))[1] if status == "dead" else None


# ---- expiry and key authority (applied first by every reader) ----


async def _apply_expiry(conn: AsyncConnection, upload_id: Any, sha256: str, attempts: int) -> None:
    if await key_exists(conn, sha256):
        await _mark_received(conn, upload_id)
        return
    if attempts + 1 >= params.ATTEMPTS_MAX:
        sql = (
            "UPDATE uploads SET state = 'failed', code = 'host_lost', attempts = attempts + 1, claim = NULL,"
            " lease_until = NULL, updated_at = now()"
        )
    else:
        sql = (
            "UPDATE uploads SET state = 'queued', attempts = attempts + 1, claim = NULL, lease_until = NULL,"
            " pages_done = NULL, pages_total = NULL, updated_at = now()"
        )
    await conn.execute(
        text(sql + " WHERE id = :i AND state IN ('claimed', 'converting', 'sending')"
             " AND lease_until < now()"),
        {"i": upload_id},
    )
    log.info("upload_expired id=%s attempts=%d", upload_id, attempts + 1)


async def expire(engine: AsyncEngine) -> None:
    """Every lease that has passed: back to queued with attempts + 1, or failed host_lost at the third."""
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, sha256, attempts FROM uploads WHERE state IN ('claimed', 'converting', 'sending')"
                " AND lease_until < now() FOR UPDATE SKIP LOCKED"
            )
        )
        for upload_id, sha256, attempts in rows.all():
            await _apply_expiry(conn, upload_id, sha256, attempts)


async def _mark_received(conn: AsyncConnection, upload_id: Any) -> None:
    await conn.execute(
        text(
            "UPDATE uploads SET state = 'received', claim = NULL, lease_until = NULL, updated_at = now()"
            " WHERE id = :i AND state <> 'received'"
        ),
        {"i": upload_id},
    )


async def _authority(conn: AsyncConnection, upload_id: Any, state: str, sha256: str) -> bool:
    """The import key exists: mark the row received (idempotent) and refuse the requested transition."""
    if state in UNDER_AUTHORITY and await key_exists(conn, sha256):
        await _mark_received(conn, upload_id)
        return True
    return False


# ---- the views (FR-INGEST-23) ----


@dataclass(frozen=True)
class Row:
    id: uuid.UUID
    owner_id: uuid.UUID
    sha256: str
    size: int
    filename: str
    language: str
    state: str
    code: str | None
    attempts: int
    claim: str | None
    pages_done: int | None
    pages_total: int | None
    created_at: Any
    expired: bool


_ROW_SQL: Final = (
    "SELECT id, owner_id, sha256, size, filename, language, state, code, attempts, claim, pages_done,"
    " pages_total, created_at, (lease_until IS NOT NULL AND lease_until < now()) AS expired FROM uploads"
)


async def row_by_id(conn: AsyncConnection, upload_id: Any, lock: bool = False) -> Row | None:
    found = (
        await conn.execute(
            text(_ROW_SQL + " WHERE id = :i" + (" FOR UPDATE" if lock else "")), {"i": upload_id}
        )
    ).first()
    return None if found is None else Row(*found)


async def row_by_sha(conn: AsyncConnection, sha256: str) -> Row | None:
    found = (await conn.execute(text(_ROW_SQL + " WHERE sha256 = :s"), {"s": sha256})).first()
    return None if found is None else Row(*found)


async def state_of(conn: AsyncConnection, row: Row) -> tuple[str, str | None, str | None]:
    key = await import_key(conn, row.sha256)
    import_status, import_code = await _status(conn, KIND, key)
    embed_status, embed_code = await _status(conn, EMBED_KIND, await embed_key(conn, row.sha256))
    return derive(
        row=row.state,
        attempts=row.attempts,
        expired=row.expired,
        row_code=row.code,
        import_status=import_status,
        import_code=import_code,
        ingest=await _ingest_id(conn, row.sha256) is not None,
        embed_status=embed_status,
        embed_code=embed_code,
    )


async def view_of(conn: AsyncConnection, row: Row) -> dict[str, Any]:
    state, code, retry = await state_of(conn, row)
    return {
        "id": str(row.id),
        "material_id": material_id(row.sha256),
        "filename": row.filename,
        "size": row.size,
        "language": row.language,
        "state": state,
        "code": code,
        "pages_done": row.pages_done,
        "pages_total": row.pages_total,
        "attempts": row.attempts,
        "created_at": row.created_at.isoformat(),
        "retry": retry,
    }


async def settle(engine: AsyncEngine, upload_id: Any | None = None) -> None:
    """What every read applies first: lease expiry, then the key authority of the row read (if any)."""
    await expire(engine)
    if upload_id is None:
        return
    async with engine.begin() as conn:
        row = await row_by_id(conn, upload_id, lock=True)
        if row is not None:
            await _authority(conn, row.id, row.state, row.sha256)


async def view(engine: AsyncEngine, owner: uuid.UUID, upload_id: uuid.UUID) -> dict[str, Any] | None:
    await settle(engine, upload_id)
    async with engine.connect() as conn:
        row = await row_by_id(conn, upload_id)
        return None if row is None or row.owner_id != owner else await view_of(conn, row)


async def listed(engine: AsyncEngine, owner: uuid.UUID) -> list[Row]:
    await expire(engine)
    async with engine.connect() as conn:
        sql = _ROW_SQL + " WHERE owner_id = :o ORDER BY created_at DESC, id"
        rows = await conn.execute(text(sql), {"o": owner})
        return [Row(*r) for r in rows.all()]


# ---- quota (FR-INGEST-18 step 9) ----


async def open_usage(conn: AsyncConnection) -> tuple[int, int, int]:
    """(open uploads, their bytes, retained bytes of uploads handed to the import)."""
    rows = (await conn.execute(text("SELECT sha256, size, state FROM uploads"))).all()
    count = total = retained = 0
    for sha256, size, state in rows:
        if state in OPEN and not await key_exists(conn, sha256):
            count, total = count + 1, total + int(size)
        else:
            retained += int(size)
    return count, total, retained


# ---- the upload lock (instance-wide) ----


@contextlib.asynccontextmanager
async def upload_lock(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """`pg_try_advisory_lock(UPLOAD_LOCK_KEY)` on one connection held for the whole operation; held: Busy."""
    async with engine.connect() as conn:
        key = {"k": params.UPLOAD_LOCK_KEY}
        got = (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), key)).scalar()
        await conn.commit()
        if not got:
            raise Busy
        try:
            yield conn
        finally:
            try:  # shielded: a cancelled request still unlocks; a failed unlock closes the session instead
                await asyncio.shield(_unlock(conn))
            except BaseException:
                await conn.invalidate()  # the session (and its advisory lock) ends with the connection
                raise


async def _unlock(conn: AsyncConnection) -> None:
    with contextlib.suppress(Exception):
        await conn.rollback()
    await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": params.UPLOAD_LOCK_KEY})
    await conn.commit()


# ---- owner transitions (routes) ----


async def cancel_or_discard(
    engine: AsyncEngine, owner: uuid.UUID, upload_id: Any, allowed: tuple[str, ...], inbox_dir: Path
) -> None:
    """Delete the row, then unlink its file, both under the upload lock (so no upload of the same bytes
    renames a new file into that path in between). StateRefused when not in `allowed` or once the import key
    exists."""
    async with upload_lock(engine):
        await expire(engine)
        async with engine.begin() as conn:
            row = await row_by_id(conn, upload_id, lock=True)
            if row is None or row.owner_id != owner:
                raise LookupError
            # decided inside, refused after the commit, so the authority's `received` mark is kept (round 1)
            refused = await _authority(conn, row.id, row.state, row.sha256) or row.state not in allowed
            if not refused:
                await conn.execute(text("DELETE FROM uploads WHERE id = :i"), {"i": row.id})
        if refused:
            raise StateRefused
        with contextlib.suppress(FileNotFoundError):
            (inbox_dir / f"{row.sha256}.pdf").unlink()  # a crash before this is swept by the next upload
        log.info("upload_deleted id=%s sha=%s", row.id, row.sha256[:12])


async def reconvert(engine: AsyncEngine, owner: uuid.UUID, upload_id: Any) -> bool:
    """Retry `reconvert`: failed (or received without key nor ingest) → queued with attempts 0."""
    async with engine.begin() as conn:
        row = await row_by_id(conn, upload_id, lock=True)
        if row is None or row.owner_id != owner:
            raise LookupError
        if await _authority(conn, row.id, row.state, row.sha256):
            return False
        imported = row.state == "received" and (
            await key_exists(conn, row.sha256) or await _ingest_id(conn, row.sha256) is not None
        )
        if imported:
            return False
        if row.state not in ("failed", "received"):
            return False
        await conn.execute(
            text(
                "UPDATE uploads SET state = 'queued', attempts = 0, code = NULL, claim = NULL,"
                " lease_until = NULL, pages_done = NULL, pages_total = NULL, updated_at = now()"
                " WHERE id = :i AND state = :s"
            ),
            {"i": row.id, "s": row.state},
        )
        await conn.execute(text(f"SELECT pg_notify('{params.NOTIFY_CHANNEL}', '')"))
        return True


async def dead_key(engine: AsyncEngine, sha256: str) -> tuple[str, str] | None:
    """The key Retry `requeue` targets: the dead import first, else the dead embed key that counts."""
    async with engine.connect() as conn:
        key = await import_key(conn, sha256)
        if (await _status(conn, KIND, key))[0] == "dead":
            return KIND, key
        embed = await embed_key(conn, sha256)
        if embed is not None and (await _status(conn, EMBED_KIND, embed))[0] == "dead":
            return EMBED_KIND, embed
        return None


# ---- the host side of the state machine (FR-INGEST-20) ----


async def host_seen(conn: AsyncConnection, values: Mapping[str, Any]) -> None:
    await conn.execute(
        text(
            "INSERT INTO ingest_hosts"
            " (singleton, seen_at, state, strays, converter_version, watcher_sha, busy)"
            " VALUES (true, now(), :state, :strays, :version, :sha, :busy)"
            " ON CONFLICT (singleton) DO UPDATE SET seen_at = now(), state = EXCLUDED.state,"
            " strays = EXCLUDED.strays, converter_version = EXCLUDED.converter_version,"
            " watcher_sha = EXCLUDED.watcher_sha, busy = EXCLUDED.busy"
        ),
        dict(values),
    )


async def queued_count(conn: AsyncConnection) -> int:
    return int((await conn.execute(text("SELECT count(*) FROM uploads WHERE state = 'queued'"))).scalar_one())


async def claim_next(conn: AsyncConnection) -> dict[str, Any] | None:
    """The oldest `queued` upload; one whose import key exists is marked `received` and skipped
    (FR-INGEST-19)."""
    sql = (
        "SELECT id, sha256, size FROM uploads WHERE state = 'queued' ORDER BY created_at, id LIMIT 1"
        " FOR UPDATE SKIP LOCKED"
    )
    while True:
        row = (await conn.execute(text(sql))).first()
        if row is None:
            return None
        if not await _authority(conn, row[0], "queued", row[1]):
            break
    token = secrets.token_hex(16)
    await conn.execute(
        text(
            "UPDATE uploads SET state = 'claimed', claim = :c, pages_done = NULL, pages_total = NULL,"
            " lease_until = now() + make_interval(secs => :l), updated_at = now()"
            " WHERE id = :i AND state = 'queued'"
        ),
        {"c": token, "l": params.LEASE_S, "i": row[0]},
    )
    log.info("upload_claimed id=%s sha=%s size=%d", row[0], row[1][:12], row[2])
    return {"id": str(row[0]), "sha256": row[1], "size": int(row[2]), "claim": token}


async def under_claim(
    conn: AsyncConnection, upload_id: Any, claim: str, states: tuple[str, ...] = LIVE
) -> Row | None:
    """The row of a live claim, locked; None is the cancel shape (deleted, other claim, expired lease, import
    key present). A lease that has passed is expired here, never renewed. The caller commits before it answers
    the cancel, so the expiry or the `received` mark written here is kept."""
    row = await row_by_id(conn, upload_id, lock=True)
    if row is None or row.claim != claim or row.state not in LIVE:
        return None
    if await _authority(conn, row.id, row.state, row.sha256):
        return None
    if row.expired:
        await _apply_expiry(conn, row.id, row.sha256, row.attempts)
        return None
    return row if row.state in states else None


async def progress(
    conn: AsyncConnection, upload_id: Any, claim: str, pages: tuple[int, int] | None, phase: str | None
) -> bool:
    """False is the cancel shape (FR-INGEST-20); the caller commits first."""
    row = await under_claim(conn, upload_id, claim)
    if row is None:
        return False
    state = row.state
    if pages is not None and state == "claimed":
        state = "converting"
    if phase == "sending" and state in ("claimed", "converting"):
        state = "sending"
    done, total = pages if pages is not None else (row.pages_done, row.pages_total)
    await conn.execute(
        text(
            "UPDATE uploads SET state = :s, pages_done = :d, pages_total = :t, updated_at = now(),"
            " lease_until = now() + make_interval(secs => :l) WHERE id = :i AND claim = :c"
        ),
        {"s": state, "d": done, "t": total, "l": params.LEASE_S, "i": row.id, "c": claim},
    )
    await conn.execute(text("UPDATE ingest_hosts SET seen_at = now(), busy = 'claim'"))
    return True


async def fail(conn: AsyncConnection, upload_id: Any, claim: str, code: str) -> bool:
    row = await under_claim(conn, upload_id, claim)
    if row is None:
        return False
    await conn.execute(
        text(
            "UPDATE uploads SET state = 'failed', code = :k, claim = NULL, lease_until = NULL,"
            " updated_at = now() WHERE id = :i AND claim = :c"
        ),
        {"k": code, "i": row.id, "c": claim},
    )
    log.info("upload_failed id=%s code=%s attempts=%d", row.id, code, row.attempts)
    return True
