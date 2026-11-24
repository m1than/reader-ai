"""SRV-PLAN's own reader over SRV-INGEST's tables, and the `study_plans` rows (FR-PLAN-6, -9, -11).

Reads only the owner's rows. Titles are read for the response and never logged.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from reader.lessons import store as lessons
from reader.plan.planner import Fixed


async def page_chars(
    conn: AsyncConnection, owner_id: uuid.UUID, material_sha256: str
) -> tuple[uuid.UUID, tuple[int, ...], tuple[int, ...]] | None:
    """(ingest id, span characters per page, spans per page) for pages 1..page_count; None if not imported."""
    row = (
        await conn.execute(
            text(
                "SELECT i.id, m.page_count FROM materials m JOIN ingests i ON i.material_id = m.id"
                # FR-PLAN-20: a deleted material is not planned (404).
                " WHERE m.owner_id = :o AND m.sha256 = :s AND m.deleted_at IS NULL"
            ),
            {"o": owner_id, "s": material_sha256},
        )
    ).first()
    if row is None:
        return None
    ingest_id, count = row[0], int(row[1])
    chars = [0] * count
    spans = [0] * count
    for page, n, k in await conn.execute(
        text(
            "SELECT page, sum(char_length(text)), count(*) FROM material_spans"
            " WHERE ingest_id = :i AND owner_id = :o GROUP BY page"
        ),
        {"i": ingest_id, "o": owner_id},
    ):
        if 1 <= page <= count:
            chars[page - 1], spans[page - 1] = int(n), int(k)
    return ingest_id, tuple(chars), tuple(spans)


async def lesson_chars(conn: AsyncConnection, owner_id: uuid.UUID, lesson_id: uuid.UUID) -> int | None:
    """FR-PLAN-13: span characters of the lesson's pages, from the owner's own ingest of its material.

    None for a lesson without a `course_plans` row of this owner or a material without an ingest.
    """
    row = (
        await conn.execute(
            text(
                "SELECT i.id, p.pdf_first, p.pdf_last FROM course_plans p"
                " JOIN materials m ON m.owner_id = p.owner_id AND m.sha256 = p.material_sha256"
                " JOIN ingests i ON i.material_id = m.id AND i.owner_id = p.owner_id"
                " JOIN lessons l ON l.id = p.lesson_id JOIN courses c ON c.id = l.course_id"
                " WHERE p.owner_id = :o AND p.lesson_id = :l"
                " AND l.deleted_at IS NULL AND c.deleted_at IS NULL"  # SRV-LESSON INV-LESSON-22
            ),
            {"o": owner_id, "l": lesson_id},
        )
    ).first()
    if row is None:
        return None
    total = (
        await conn.execute(
            text(
                "SELECT coalesce(sum(char_length(text)), 0) FROM material_spans"
                " WHERE ingest_id = :i AND owner_id = :o AND page BETWEEN :f AND :t"
            ),
            {"i": row[0], "o": owner_id, "f": row[1], "t": row[2]},
        )
    ).scalar()
    return int(total or 0)


async def outline_entries(
    conn: AsyncConnection, owner_id: uuid.UUID, ingest_id: uuid.UUID
) -> tuple[tuple[int, int | None, str], ...]:
    rows = await conn.execute(
        text(
            "SELECT level, page, title FROM material_outline"
            " WHERE ingest_id = :i AND owner_id = :o ORDER BY seq"
        ),
        {"i": ingest_id, "o": owner_id},
    )
    return tuple((int(r[0]), None if r[1] is None else int(r[1]), str(r[2])) for r in rows)


@dataclass(frozen=True)
class Stored:
    course_id: uuid.UUID
    total_weeks: int
    minutes_per_day: int
    rate_centi: int
    start_date: dt.date
    plan_sha256: str


async def stored(conn: AsyncConnection, owner_id: uuid.UUID, plan_course_id: str) -> Stored | None:
    row = (
        await conn.execute(
            text(
                "SELECT course_id, total_weeks, minutes_per_day, rate_centi, start_date, plan_sha256"
                " FROM study_plans WHERE owner_id = :o AND plan_course_id = :c"
            ),
            {"o": owner_id, "c": plan_course_id},
        )
    ).first()
    return None if row is None else Stored(*row)


@dataclass(frozen=True)
class CourseState:
    course_id: uuid.UUID
    plan_course_id: str
    material_sha256: str
    deleted: bool
    archived: bool


async def course_state(
    conn: AsyncConnection,
    owner_id: uuid.UUID,
    *,
    plan_course_id: str | None = None,
    course_id: uuid.UUID | None = None,
) -> CourseState | None:
    """FR-PLAN-20: the owner's study-plan course (by plan id or course id), deleted or archived or neither."""
    row = (
        await conn.execute(
            text(
                "SELECT s.course_id, s.plan_course_id, s.material_sha256, c.deleted_at IS NOT NULL,"
                " c.archived_at IS NOT NULL FROM study_plans s JOIN courses c ON c.id = s.course_id"
                " WHERE s.owner_id = :o AND (s.plan_course_id = :p OR s.course_id = :c)"
            ),
            {"o": owner_id, "p": plan_course_id, "c": course_id},
        )
    ).first()
    return None if row is None else CourseState(*row)


async def first_active(
    conn: AsyncConnection, owner_id: uuid.UUID, lesson_ids: list[uuid.UUID]
) -> uuid.UUID | None:
    """FR-PIPELINE-22: compile-first skips skipped and deleted lessons; the first remaining one by ordinal."""
    value: uuid.UUID | None = (
        await conn.execute(
            text(
                "SELECT id FROM lessons WHERE owner_id = :o AND id = ANY(CAST(:ids AS uuid[]))"
                " AND skipped_at IS NULL AND deleted_at IS NULL ORDER BY ordinal LIMIT 1"
            ),
            {"o": owner_id, "ids": lesson_ids},
        )
    ).scalar()
    return value


async def course_of(conn: AsyncConnection, owner_id: uuid.UUID, plan_course_id: str) -> uuid.UUID | None:
    value: uuid.UUID | None = (
        await conn.execute(
            text(
                "SELECT l.course_id FROM course_plans p JOIN lessons l ON l.id = p.lesson_id"
                " WHERE p.owner_id = :o AND p.plan_course_id = :c LIMIT 1"
            ),
            {"o": owner_id, "c": plan_course_id},
        )
    ).scalar()
    return value


async def fixed(conn: AsyncConnection, owner_id: uuid.UUID, plan_course_id: str) -> tuple[Fixed, ...]:
    """FR-PLAN-6: the started lessons of the plan course, by SRV-LESSON `started_lessons`."""
    course = await course_of(conn, owner_id, plan_course_id)
    if course is None:
        return ()
    started = await lessons.started_lessons(conn, owner_id, course)
    rows = await conn.execute(
        text(
            "SELECT plan_lesson_id, lesson_id, pdf_first, pdf_last FROM course_plans"
            " WHERE owner_id = :o AND plan_course_id = :c ORDER BY pdf_first"
        ),
        {"o": owner_id, "c": plan_course_id},
    )
    return tuple(Fixed(int(r[2]), int(r[3]), str(r[0])) for r in rows if r[1] in started)


async def plan_lessons(
    conn: AsyncConnection, owner_id: uuid.UUID, plan_course_id: str
) -> list[dict[str, Any]]:
    rows = await conn.execute(
        text(
            "SELECT p.plan_lesson_id, p.lesson_id, l.ordinal FROM course_plans p"
            " JOIN lessons l ON l.id = p.lesson_id"
            " WHERE p.owner_id = :o AND p.plan_course_id = :c ORDER BY l.ordinal"
        ),
        {"o": owner_id, "c": plan_course_id},
    )
    return [{"plan_lesson_id": r[0], "lesson_id": str(r[1]), "ordinal": int(r[2])} for r in rows]


async def save(
    conn: AsyncConnection,
    owner_id: uuid.UUID,
    course_id: uuid.UUID,
    plan_course_id: str,
    material_sha256: str,
    ingest_id: uuid.UUID,
    weeks: int,
    minutes: int,
    rate_centi: int,
    start_date: dt.date,
    plan_sha256: str,
) -> None:
    """FR-PLAN-11: one row per course; `start_date` is written by the first confirm only."""
    await conn.execute(
        text(
            "INSERT INTO study_plans (course_id, owner_id, plan_course_id, material_sha256, ingest_id,"
            " total_weeks, minutes_per_day, rate_centi, start_date, plan_sha256)"
            " VALUES (:c, :o, :p, :m, :i, :w, :n, :r, :s, :h)"
            " ON CONFLICT (course_id) DO UPDATE SET ingest_id = excluded.ingest_id,"
            " total_weeks = excluded.total_weeks, minutes_per_day = excluded.minutes_per_day,"
            " rate_centi = excluded.rate_centi, plan_sha256 = excluded.plan_sha256, confirmed_at = now()"
        ),
        {
            "c": course_id,
            "o": owner_id,
            "p": plan_course_id,
            "m": material_sha256,
            "i": ingest_id,
            "w": weeks,
            "n": minutes,
            "r": rate_centi,
            "s": start_date,
            "h": plan_sha256,
        },
    )
