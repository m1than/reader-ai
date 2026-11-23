"""Courses, lessons and revisions (SRV-LESSON FR-LESSON-4..6).

Every function takes an `AsyncConnection` inside the caller's transaction and never commits. Blob IO runs in a
worker thread. A lesson's current revision is the sha256 of its most recent stage record (highest id).
"""

from __future__ import annotations

import json
import math
import uuid
from pathlib import Path
from typing import Any, Final

import anyio.to_thread
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from reader import blobs
from reader.config import Secret
from reader.lessons.access import lesson_uuid
from reader.lessons.schema import STAGES, LessonWriteError, notes_ids, validate

__all__ = [
    "LessonWriteError",
    "create_course",
    "current_sha256",
    "delete_unstarted_lesson",
    "ensure_lesson",
    "reorder_lessons",
    "started_lessons",
    "write_revision",
]

# FR-LESSON-15: the referring (table, column) pairs come from the catalog, so a later table counts by itself.
# `course_plans` is the plan's own row, not a sign that the lesson started.
_REFERENCES: Final = text(
    "SELECT c.conrelid::regclass::text, a.attname FROM pg_constraint c"
    " CROSS JOIN LATERAL unnest(c.conkey, c.confkey) AS k(src, dst)"
    " JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.src"
    " JOIN pg_attribute b ON b.attrelid = c.confrelid AND b.attnum = k.dst"
    " WHERE c.contype = 'f' AND c.confrelid = 'lessons'::regclass AND b.attname = 'id'"
    " AND c.conrelid::regclass::text <> 'course_plans' ORDER BY 1, 2"
)


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def started_lessons(
    conn: AsyncConnection, owner_id: uuid.UUID, course_id: uuid.UUID
) -> frozenset[uuid.UUID]:
    """FR-LESSON-15: the course's lessons that any row other than their `course_plans` row refers to."""
    refs = (await conn.execute(_REFERENCES)).all()
    if not refs:
        return frozenset()
    # Names come from the catalog, never from input: tables as regclass text, columns quoted here.
    exists = " OR ".join(
        f"EXISTS (SELECT 1 FROM {table} t WHERE t.{_ident(col)} = l.id)"  # noqa: S608
        for table, col in refs
    )
    rows = await conn.execute(
        text(f"SELECT l.id FROM lessons l WHERE l.course_id = :c AND l.owner_id = :o AND ({exists})"),  # noqa: S608
        {"c": course_id, "o": owner_id},
    )
    return frozenset(r[0] for r in rows)


async def delete_unstarted_lesson(conn: AsyncConnection, owner_id: uuid.UUID, lesson_id: uuid.UUID) -> None:
    """FR-LESSON-15: delete the owner's lesson only if it has not started (its plan row is gone)."""
    course = (
        await conn.execute(
            text("SELECT course_id FROM lessons WHERE id = :l AND owner_id = :o"),
            {"l": lesson_id, "o": owner_id},
        )
    ).scalar()
    if course is None:
        raise LessonWriteError("unknown_lesson")
    if lesson_id in await started_lessons(conn, owner_id, course):
        raise LessonWriteError("lesson_started")
    try:
        async with conn.begin_nested():
            await conn.execute(
                text("DELETE FROM lessons WHERE id = :l AND owner_id = :o"), {"l": lesson_id, "o": owner_id}
            )
    except IntegrityError:
        raise LessonWriteError("lesson_started") from None


async def reorder_lessons(
    conn: AsyncConnection, owner_id: uuid.UUID, course_id: uuid.UUID, lesson_ids: list[uuid.UUID]
) -> None:
    """FR-LESSON-15: ordinals 1..n in list order, in two passes (the unique constraint is not deferrable)."""
    current = {
        r[0]: int(r[1])
        for r in await conn.execute(
            text("SELECT id, ordinal FROM lessons WHERE course_id = :c AND owner_id = :o"),
            {"c": course_id, "o": owner_id},
        )
    }
    if set(lesson_ids) != set(current) or len(lesson_ids) != len(current):
        raise LessonWriteError("lessons_mismatch")
    if [current[x] for x in lesson_ids] == list(range(1, len(lesson_ids) + 1)):
        return
    offset = max(current.values(), default=0) + len(lesson_ids)
    await conn.execute(
        text("UPDATE lessons SET ordinal = ordinal + :d WHERE course_id = :c AND owner_id = :o"),
        {"d": offset, "c": course_id, "o": owner_id},
    )
    await conn.execute(
        text(
            "UPDATE lessons SET ordinal = v.n"
            " FROM unnest(CAST(:ids AS uuid[]), CAST(:ns AS int[])) AS v(id, n)"
            " WHERE lessons.id = v.id AND lessons.course_id = :c"
        ),
        {"ids": lesson_ids, "ns": list(range(1, len(lesson_ids) + 1)), "c": course_id},
    )


TITLE_MAX_CHARS: Final = 200
PROVENANCE_MAX_BYTES: Final = 16384
PROVENANCE_KEYS: Final = frozenset(
    {"input_sha256", "prompt_version", "model", "cost_usd", "duration_s", "job_key", "source"}
)


def _title(title: str) -> str:
    stripped = title.strip() if isinstance(title, str) else ""
    if not 1 <= len(stripped) <= TITLE_MAX_CHARS:
        raise ValueError(f"title must be 1..{TITLE_MAX_CHARS} characters after stripping")
    return stripped


def _contains_secret(value: object) -> bool:
    if isinstance(value, Secret):
        return True
    if isinstance(value, dict):
        return any(_contains_secret(k) or _contains_secret(v) for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_secret(v) for v in value)
    return False


def _scalar(value: object) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        return "\x00" not in value  # Postgres jsonb cannot hold U+0000
    return value is None or isinstance(value, (int, bool))


def _provenance_json(stage: str, provenance: object) -> str:
    """FR-LESSON-5; the compact JSON stored with the stage record."""
    if not isinstance(stage, str) or stage not in STAGES:
        raise LessonWriteError("bad_provenance")
    if not isinstance(provenance, dict) or not set(provenance) <= PROVENANCE_KEYS:
        raise LessonWriteError("bad_provenance")
    if not all(_scalar(v) for v in provenance.values()):
        raise LessonWriteError("bad_provenance")
    try:
        compact = json.dumps(provenance, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        size = len(compact.encode("utf-8"))
    except ValueError, UnicodeEncodeError:  # a huge int (str conversion limit), a lone surrogate
        raise LessonWriteError("bad_provenance") from None
    if size > PROVENANCE_MAX_BYTES:
        raise LessonWriteError("bad_provenance")
    return compact


async def create_course(
    conn: AsyncConnection, owner_id: uuid.UUID, title: str, material_sha256: str | None
) -> uuid.UUID:
    if material_sha256 is not None and not blobs.is_sha256(material_sha256):
        raise ValueError("material_sha256 must be 64 lowercase hex characters")
    row = await conn.execute(
        text("INSERT INTO courses (owner_id, title, material_sha256) VALUES (:o, :t, :m) RETURNING id"),
        {"o": owner_id, "t": _title(title), "m": material_sha256},
    )
    course_id: uuid.UUID = row.scalar_one()
    return course_id


async def ensure_lesson(
    conn: AsyncConnection, owner_id: uuid.UUID, course_id: uuid.UUID, ordinal: int, title: str
) -> uuid.UUID:
    if type(ordinal) is not int or ordinal < 1:
        raise ValueError("ordinal must be an integer >= 1")
    clean = _title(title)
    found = await conn.execute(text("SELECT owner_id FROM courses WHERE id = :c"), {"c": course_id})
    owner = found.scalar()
    if owner != owner_id:
        raise LessonWriteError("not_owner")
    inserted = await conn.execute(
        text(
            "INSERT INTO lessons (owner_id, course_id, ordinal, title) VALUES (:o, :c, :n, :t)"
            " ON CONFLICT (course_id, ordinal) DO NOTHING RETURNING id"
        ),
        {"o": owner_id, "c": course_id, "n": ordinal, "t": clean},
    )
    lesson_id: uuid.UUID | None = inserted.scalar()
    if lesson_id is None:
        existing = await conn.execute(
            text("SELECT id FROM lessons WHERE course_id = :c AND ordinal = :n"),
            {"c": course_id, "n": ordinal},
        )
        lesson_id = existing.scalar_one()
    return lesson_id


async def current_sha256(conn: AsyncConnection, lesson_id: uuid.UUID) -> str | None:
    """The lesson's current revision: the sha256 of its most recent stage record (FR-LESSON-4)."""
    row = await conn.execute(
        text("SELECT sha256 FROM lesson_revision_stages WHERE lesson_id = :l ORDER BY id DESC LIMIT 1"),
        {"l": lesson_id},
    )
    sha256: str | None = row.scalar()
    return sha256


async def write_revision(
    conn: AsyncConnection,
    blobs_root: Path,
    owner_id: uuid.UUID,
    lesson_id: uuid.UUID | str,
    data: bytes,
    stage: str,
    provenance: dict[str, Any],
) -> tuple[str, bool]:
    """FR-LESSON-4: validate, store, and record the stage; returns (sha256, created)."""
    if _contains_secret(provenance):
        raise TypeError("a Secret cannot be stored in lesson provenance")
    lesson = lesson_id if isinstance(lesson_id, uuid.UUID) else lesson_uuid(lesson_id)
    locked = None
    if lesson is not None:
        locked = (
            await conn.execute(
                text("SELECT id FROM lessons WHERE id = :l AND owner_id = :o FOR UPDATE"),
                {"l": lesson, "o": owner_id},
            )
        ).scalar()
    if lesson is None or locked is None:
        raise LessonWriteError("unknown_lesson")
    validated = validate(data)
    stage_json = _provenance_json(stage, provenance)
    current = await current_sha256(conn, lesson)
    if current is not None and current != validated.sha256:
        # Fails closed: a missing or corrupt current blob refuses every other write until it is healed
        # (rewriting the current revision itself skips this read and heals the blob).
        previous = json.loads(await anyio.to_thread.run_sync(blobs.get, blobs_root, current))
        if not notes_ids(previous) <= notes_ids(validated.notebook):
            raise LessonWriteError("notes_cell_removed")
    exists = (
        await conn.execute(
            text("SELECT 1 FROM lesson_revisions WHERE lesson_id = :l AND sha256 = :s"),
            {"l": lesson, "s": validated.sha256},
        )
    ).scalar() is not None
    await anyio.to_thread.run_sync(blobs.put, blobs_root, validated.canonical)
    if not exists:
        await conn.execute(
            text(
                "INSERT INTO lesson_revisions (owner_id, lesson_id, sha256, byte_size, cell_count)"
                " VALUES (:o, :l, :s, :b, :c)"
            ),
            {
                "o": owner_id,
                "l": lesson,
                "s": validated.sha256,
                "b": len(validated.canonical),
                "c": validated.cell_count,
            },
        )
    await conn.execute(text("UPDATE lessons SET updated_at = now() WHERE id = :l"), {"l": lesson})
    await conn.execute(
        text(
            "INSERT INTO lesson_revision_stages (owner_id, lesson_id, sha256, stage, provenance)"
            " VALUES (:o, :l, :s, :st, CAST(:p AS jsonb))"
        ),
        {"o": owner_id, "l": lesson, "s": validated.sha256, "st": stage, "p": stage_json},
    )
    return validated.sha256, not exists
