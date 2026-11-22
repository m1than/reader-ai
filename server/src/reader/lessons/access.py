"""Who may open a lesson (SRV-LESSON FR-LESSON-11, FR-LESSON-12). Stage 1: the course owner only."""

from __future__ import annotations

import re
import uuid
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SHA256: Final = re.compile(r"[0-9a-f]{64}")


def lesson_uuid(value: str) -> uuid.UUID | None:
    """The lesson id of a lowercase canonical UUID string, else None (checked before any query)."""
    return uuid.UUID(value) if isinstance(value, str) and _UUID.fullmatch(value) else None


def is_sha256(value: str) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


async def can_open(conn: AsyncConnection, user_id: uuid.UUID, lesson_id: str) -> bool:
    lesson = lesson_uuid(lesson_id)
    if lesson is None:
        return False
    row = await conn.execute(
        text(
            "SELECT 1 FROM lessons l JOIN courses c ON c.id = l.course_id"
            " WHERE l.id = :l AND c.owner_id = :u"
        ),
        {"l": lesson, "u": user_id},
    )
    return row.scalar() is not None
