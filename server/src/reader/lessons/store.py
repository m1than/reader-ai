"""Courses, lessons and revisions (SRV-LESSON FR-LESSON-4..6).

Every function takes an `AsyncConnection` inside the caller's transaction and never commits.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from reader import blobs
from reader.config import Secret
from reader.lessons.schema import STAGES, LessonWriteError, notes_ids, validate

__all__ = ["LessonWriteError", "create_course", "ensure_lesson", "write_revision"]

TITLE_MAX_CHARS: Final = 200
PROVENANCE_MAX_BYTES: Final = 16384
PROVENANCE_KEYS: Final = frozenset(
    {"input_sha256", "prompt_version", "model", "cost_usd", "duration_s", "job_key", "source"}
)
_SHA256: Final = re.compile(r"[0-9a-f]{64}")


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
    return value is None or isinstance(value, (str, int, bool))


def _provenance_json(stage: str, provenance: object) -> str:
    """FR-LESSON-5; the compact JSON stored with the stage record."""
    if not isinstance(stage, str) or stage not in STAGES:
        raise LessonWriteError("bad_provenance")
    if not isinstance(provenance, dict) or not set(provenance) <= PROVENANCE_KEYS:
        raise LessonWriteError("bad_provenance")
    if not all(_scalar(v) for v in provenance.values()):
        raise LessonWriteError("bad_provenance")
    compact = json.dumps(provenance, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if len(compact.encode("utf-8")) > PROVENANCE_MAX_BYTES:
        raise LessonWriteError("bad_provenance")
    return compact


def _uuid(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


async def create_course(
    conn: AsyncConnection, owner_id: uuid.UUID, title: str, material_sha256: str | None
) -> uuid.UUID:
    if material_sha256 is not None and _SHA256.fullmatch(material_sha256) is None:
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


async def _newest_notes(conn: AsyncConnection, blobs_root: Any, lesson_id: uuid.UUID) -> set[str]:
    newest = (
        await conn.execute(
            text("SELECT sha256 FROM lesson_revisions WHERE lesson_id = :l ORDER BY seq DESC LIMIT 1"),
            {"l": lesson_id},
        )
    ).scalar()
    if newest is None:
        return set()
    return notes_ids(json.loads(blobs.get(blobs_root, newest)))


async def write_revision(
    conn: AsyncConnection,
    blobs_root: Any,
    owner_id: uuid.UUID,
    lesson_id: uuid.UUID | str,
    data: bytes,
    stage: str,
    provenance: dict[str, Any],
) -> tuple[str, bool]:
    """FR-LESSON-4: validate, store, and record the stage; returns (sha256, created)."""
    if _contains_secret(provenance):
        raise TypeError("a Secret cannot be stored in lesson provenance")
    lesson = _uuid(lesson_id)
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
    exists = (
        await conn.execute(
            text("SELECT 1 FROM lesson_revisions WHERE lesson_id = :l AND sha256 = :s"),
            {"l": lesson, "s": validated.sha256},
        )
    ).scalar() is not None
    if not exists:
        previous = await _newest_notes(conn, blobs_root, lesson)
        if not previous <= notes_ids(validated.notebook):
            raise LessonWriteError("notes_cell_removed")
    blobs.put(blobs_root, validated.canonical)
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
