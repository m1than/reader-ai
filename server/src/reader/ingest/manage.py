"""Rename, soft delete and restore of imported materials (SRV-INGEST FR-INGEST-30..32).

Only a material with a `materials` row can be managed. Delete is a tombstone (`deleted_at`): no row of
materials, ingests, spans, outline or uploads and no file under the blobs root is removed. The course cascade
and its restore go through SRV-LESSON's `reader.lessons.manage` (loaded only when a course is affected), under
the lock order of SRV-LESSON INV-LESSON-23: plan-course locks, lesson run locks, then the position lock.
"""

from __future__ import annotations

import importlib
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from reader.ingest import uploads
from reader.ingest.jobs import KIND
from reader.jobs.queue import job_status_of

TITLE_MAX: Final = 200
CONFIRM_CHARS: Final = 40  # SRV-LESSON FR-LESSON-25's confirm rule
# pg_advisory_xact_lock(namespace, hashtext(owner)): the owner's course positions ("RCPO", FR-LESSON-20).
POSITION_LOCK_NAMESPACE: Final = 0x5243504F
_LOCK_ROUNDS: Final = 3  # re-reads of the affected set after locking (a course created meanwhile)

# Busy checks other modules register on import, (conn, owner_id, materials.id) → busy: SRV-FRESH adds its
# running report (FR-FRESH-20). The ingest package never names their tables (SRV-FRESH's quarantine).
BusyCheck = Callable[[AsyncConnection, uuid.UUID, uuid.UUID], Awaitable[bool]]
BUSY_CHECKS: list[BusyCheck] = []


class Refused(Exception):
    """A refusal with its HTTP status and JSON body; raised inside the request's transaction (rolled back)."""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__(body.get("error"))
        self.status, self.body = status, body


def refused(status: int, code: str) -> Refused:
    return Refused(status, {"error": code})


@dataclass(frozen=True)
class Material:
    id: uuid.UUID
    sha256: str
    config_id: str
    title: str | None
    deleted_at: datetime | None
    version: int


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def clean_title(value: Any) -> str | None:
    """FR-INGEST-30: null clears the override; a string is NFC, trimmed, 1..200 characters, no Cc."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise refused(400, "title_invalid")
    title = _nfc(value)
    if not 1 <= len(title) <= TITLE_MAX or any(unicodedata.category(ch) == "Cc" for ch in title):
        raise refused(400, "title_invalid")
    return title


def confirm_matches(confirm: Any, display: str) -> bool:
    """FR-INGEST-31: the target is the display title NFC-normalised, cut to 40 code points, then trimmed;
    `confirm` is NFC-normalised and trimmed (SRV-LESSON FR-LESSON-25's rule, character for character). An
    empty target matches nothing, the empty `confirm` included."""
    target = unicodedata.normalize("NFC", display)[:CONFIRM_CHARS].strip()
    return target != "" and isinstance(confirm, str) and _nfc(confirm) == target


def default_title(material_id: str, filename: str | None) -> str:
    """FR-INGEST-23: the filename without a trailing `.pdf`, else the material id."""
    if filename is None:
        return material_id
    return filename[:-4] if filename.lower().endswith(".pdf") else filename


_ROW: Final = (
    "SELECT id, sha256, config_id, title, deleted_at, version FROM materials"
    " WHERE owner_id = :o AND sha256 = :s"
)


async def row_of(conn: AsyncConnection, owner: uuid.UUID, sha: str, *, lock: bool = False) -> Material | None:
    """The owner's row of the material; `lock` holds it FOR UPDATE to the end of the transaction."""
    sql = _ROW + " FOR UPDATE" if lock else _ROW
    row = (await conn.execute(text(sql), {"o": owner, "s": sha})).first()
    return None if row is None else Material(row[0], row[1], row[2], row[3], row[4], int(row[5]))


async def filename_of(conn: AsyncConnection, owner: uuid.UUID, sha: str) -> str | None:
    sql = "SELECT filename FROM uploads WHERE owner_id = :o AND sha256 = :s"
    value = (await conn.execute(text(sql), {"o": owner, "s": sha})).scalar()
    return None if value is None else str(value)


async def display_title(conn: AsyncConnection, owner: uuid.UUID, material_id: str, row: Material) -> str:
    if row.title is not None:
        return row.title
    uploaded = material_id.startswith("up-")  # a reader.yaml material's default is its id, never a filename
    return default_title(material_id, await filename_of(conn, owner, row.sha256) if uploaded else None)


# ---- courses and impact ----


def _courses(rows: Sequence[Any]) -> list[dict[str, Any]]:
    return [{"course_id": str(r[0]), "title": r[1]} for r in rows]


async def courses_using(conn: AsyncConnection, owner: uuid.UUID, sha: str) -> list[dict[str, Any]]:
    """Courses that are not deleted (archived included) whose material is this one."""
    rows = await conn.execute(
        text(
            "SELECT id, title FROM courses"
            " WHERE owner_id = :o AND material_sha256 = :s AND deleted_at IS NULL ORDER BY created_at, id"
        ),
        {"o": owner, "s": sha},
    )
    return _courses(rows.all())


async def courses_cascaded(conn: AsyncConnection, owner: uuid.UUID, material_id: str) -> list[dict[str, Any]]:
    rows = await conn.execute(
        text(
            "SELECT id, title FROM courses WHERE owner_id = :o AND deleted_by_material = :m"
            " ORDER BY created_at, id"
        ),
        {"o": owner, "m": material_id},
    )
    return _courses(rows.all())


async def lesson_count(conn: AsyncConnection, owner: uuid.UUID, courses: Sequence[dict[str, Any]]) -> int:
    ids = [uuid.UUID(c["course_id"]) for c in courses]
    sql = "SELECT count(*) FROM lessons WHERE owner_id = :o AND course_id = ANY(:c) AND deleted_at IS NULL"
    return int((await conn.execute(text(sql), {"o": owner, "c": ids})).scalar_one())


async def uploads_open(conn: AsyncConnection, owner: uuid.UUID, sha: str) -> int:
    """Uploads of these bytes still on their way to the import (FR-INGEST-18's open states, no import key)."""
    rows = await conn.execute(
        text("SELECT state FROM uploads WHERE owner_id = :o AND sha256 = :s"), {"o": owner, "s": sha}
    )
    states = [r[0] for r in rows]
    if not any(state in uploads.OPEN for state in states):
        return 0
    return 0 if await uploads.key_exists(conn, sha) else sum(state in uploads.OPEN for state in states)


async def impact(conn: AsyncConnection, owner: uuid.UUID, sha: str) -> dict[str, Any]:
    courses = await courses_using(conn, owner, sha)
    return {
        "courses": courses,
        "lessons": await lesson_count(conn, owner, courses),
        "uploads_open": await uploads_open(conn, owner, sha),
    }


async def busy(conn: AsyncConnection, owner: uuid.UUID, row: Material) -> bool:
    """FR-INGEST-31: an open upload, a running import or embed key, or a running freshness report."""
    if await uploads_open(conn, owner, row.sha256):
        return True
    if await job_status_of(conn, KIND, await uploads.import_key(conn, row.sha256)) == "running":
        return True
    embed = await uploads.embed_key(conn, row.sha256)
    if embed is not None and await job_status_of(conn, uploads.EMBED_KIND, embed) == "running":
        return True
    for check in BUSY_CHECKS:
        if await check(conn, owner, row.id):
            return True
    return False


# ---- locks (SRV-LESSON INV-LESSON-23's order over the whole set) ----


@dataclass(frozen=True)
class Affected:
    """What a material request may write: the plan courses and lessons of its courses."""

    plan_courses: frozenset[str]
    lessons: frozenset[uuid.UUID]


async def affected(conn: AsyncConnection, owner: uuid.UUID, sha: str, material_id: str) -> Affected:
    """The courses using the material or deleted by its cascade, read as their plan courses and lessons."""
    using = await courses_using(conn, owner, sha)
    cascaded = await courses_cascaded(conn, owner, material_id)
    course_ids = sorted({uuid.UUID(c["course_id"]) for c in (*using, *cascaded)})
    plans = await conn.execute(
        text(
            "SELECT DISTINCT cp.plan_course_id FROM course_plans cp JOIN lessons l ON l.id = cp.lesson_id"
            " WHERE cp.owner_id = :o AND l.course_id = ANY(:c)"
        ),
        {"o": owner, "c": course_ids},
    )
    lessons = await conn.execute(
        text("SELECT id FROM lessons WHERE owner_id = :o AND course_id = ANY(:c)"),
        {"o": owner, "c": course_ids},
    )
    return Affected(frozenset(str(r[0]) for r in plans), frozenset(r[0] for r in lessons))


def lock_sequence(found: Affected) -> list[tuple[str, str]]:
    """INV-LESSON-23's global order: ("plan", plan_course_id) ascending, then ("lesson", lesson id) ascending
    (the position lock comes last, once, in `lock_affected`)."""
    plans = [("plan", p) for p in sorted(found.plan_courses)]
    return plans + [("lesson", str(lesson)) for lesson in sorted(found.lessons)]


async def _advisory(conn: AsyncConnection, namespace: int, key: str) -> None:
    await conn.execute(text("SELECT pg_advisory_xact_lock(:ns, hashtext(:k))"), {"ns": namespace, "k": key})


async def _take(conn: AsyncConnection, owner: uuid.UUID, kind: str, key: str) -> None:
    from reader.pipeline.courses import lock_course
    from reader.pipeline.runs import LOCK_NAMESPACE

    if kind == "plan":
        await lock_course(conn, owner, key)
    else:
        await _advisory(conn, LOCK_NAMESPACE, key)


async def lock_affected(conn: AsyncConnection, owner: uuid.UUID, sha: str, material_id: str) -> None:
    """FR-INGEST-31: course locks, lesson locks, then the position lock, over every course this request may
    write. After each walk the affected set is read again. What is guaranteed: a set that stays stable
    between two reads has been walked whole in the global order; a set that changed is walked whole again in
    that order (a lock already held is granted again at once; nothing is released, so a lock taken in an
    earlier walk may precede one that sorts before it); a set that still changes after `_LOCK_ROUNDS` walks
    refuses `material_busy`."""
    held: Affected | None = None
    for _ in range(_LOCK_ROUNDS):
        found = await affected(conn, owner, sha, material_id)
        if found == held:
            break
        for kind, key in lock_sequence(found):
            await _take(conn, owner, kind, key)
        held = found
    else:
        if await affected(conn, owner, sha, material_id) != held:
            raise refused(409, "material_busy")
    await _advisory(conn, POSITION_LOCK_NAMESPACE, str(owner))


def lessons_manage() -> Any:
    """SRV-LESSON's course delete and restore (FR-LESSON-25), loaded only when a course is affected."""
    return importlib.import_module("reader.lessons.manage")


async def delete_courses(
    conn: AsyncConnection, owner: uuid.UUID, courses: Sequence[dict[str, Any]], material_id: str
) -> None:
    """The cascade: SRV-LESSON soft-deletes them all or refuses before any write (409 with its code)."""
    ids = [uuid.UUID(c["course_id"]) for c in courses]
    try:
        await lessons_manage().delete_courses(conn, owner, ids, by_material=material_id, actor_id=owner)
    except Exception as refusal:  # SRV-LESSON's ManageError carries `.code` (compile_running)
        if type(refusal).__name__ != "ManageError":
            raise
        raise refused(409, str(getattr(refusal, "code", "refused"))) from None


async def restore_courses(conn: AsyncConnection, owner: uuid.UUID, material_id: str) -> list[str]:
    if not await courses_cascaded(conn, owner, material_id):
        return []
    restored = await lessons_manage().restore_courses_by_material(conn, owner, material_id, owner)
    return [str(course_id) for course_id in restored]
