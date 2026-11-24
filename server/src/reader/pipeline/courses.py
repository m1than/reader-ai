"""Course import, gate G1 (SRV-PIPELINE FR-PIPELINE-1, FR-PIPELINE-14).

A plan shaped like pilot/bashir/course.yaml (as JSON): `course` (an id string, or {id, title,
material_sha256}), `material` {sha256, title?} when `course` is a string, and `lessons` (or
`stages[].lessons`) with `id`, `title` and `pages.pdf` [first, last]. One `course_plans` row per lesson,
upserted by (owner, course id, lesson id); the lesson uuid never changes on a later import. The POST is the
owner's G1 approval.

FR-PIPELINE-14: every import runs under a per-course advisory lock; SRV-PLAN's study-plan import (origin
`study_plan`) also removes unstarted lessons absent from the plan and renumbers the course in plan order, or,
for a course whose lessons the owner reordered (`order_source = 'manual'`), by SRV-PLAN FR-PLAN-21's rule.
FR-PIPELINE-22: an import onto a deleted course is refused before any write.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from reader.lessons import store
from reader.lessons.schema import LessonWriteError
from reader.pipeline.errors import CourseDeleted, PipelineError
from reader.pipeline.source import material_imported
from reader.plan import order

SHA256: Final = re.compile(r"[0-9a-f]{64}")
TITLE_MAX: Final = 200
# pg_advisory_xact_lock(namespace, hashtext(owner:course)): one writer per plan course ("RCPC"); taken before
# FR-PIPELINE-11's per-lesson lock everywhere.
COURSE_LOCK_NAMESPACE: Final = 0x52435043


@dataclass(frozen=True)
class PlanLesson:
    id: str
    title: str
    first: int
    last: int


@dataclass(frozen=True)
class Plan:
    course_id: str
    title: str
    material_sha256: str
    lessons: list[PlanLesson]


@dataclass(frozen=True)
class ImportResult:
    course_id: uuid.UUID
    lessons: list[tuple[str, uuid.UUID, int]]  # (plan lesson id, lesson id, ordinal) in plan order
    created: int
    removed: int


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PipelineError("plan_invalid")
    return value.strip()[:TITLE_MAX]


def parse_plan(raw: Any) -> Plan:
    if not isinstance(raw, dict):
        raise PipelineError("plan_invalid")
    course = raw.get("course")
    if isinstance(course, dict):
        course_id, title, sha = course.get("id"), course.get("title"), course.get("material_sha256")
    else:
        given = raw.get("material")
        material: dict[str, Any] = given if isinstance(given, dict) else {}
        course_id, title, sha = course, material.get("title") or course, material.get("sha256")
    if not isinstance(sha, str) or not SHA256.fullmatch(sha):
        raise PipelineError("plan_invalid")
    items = raw.get("lessons")
    if items is None and isinstance(raw.get("stages"), list):
        items = [
            lesson
            for stage in raw["stages"]
            if isinstance(stage, dict)
            for lesson in stage.get("lessons", [])
        ]
    if not isinstance(items, list) or not items:
        raise PipelineError("plan_invalid")
    lessons: list[PlanLesson] = []
    for item in items:
        pages = item.get("pages", {}).get("pdf") if isinstance(item, dict) else None
        if not (isinstance(pages, list) and len(pages) == 2 and all(type(p) is int for p in pages)):
            raise PipelineError("plan_invalid")
        if not 1 <= pages[0] <= pages[1]:
            raise PipelineError("plan_invalid")
        lessons.append(PlanLesson(_text(item.get("id")), _text(item.get("title")), pages[0], pages[1]))
    if len({lesson.id for lesson in lessons}) != len(lessons):
        raise PipelineError("plan_invalid")
    return Plan(_text(course_id), _text(title), sha, lessons)


async def lock_course(conn: AsyncConnection, owner_id: uuid.UUID, plan_course_id: str) -> None:
    """The per-course lock, held to the end of the caller's transaction (FR-PIPELINE-14)."""
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:k))"),
        {"ns": COURSE_LOCK_NAMESPACE, "k": f"{owner_id}:{plan_course_id}"},
    )


async def _remove_unstarted(
    conn: AsyncConnection,
    owner_id: uuid.UUID,
    plan: Plan,
    course_id: uuid.UUID,
    known: dict[str, tuple[uuid.UUID, int, int]],
) -> int:
    """Origin `study_plan`: absent lessons go when they have not started; started ones never move."""
    wanted = {lesson.id: (lesson.first, lesson.last) for lesson in plan.lessons}
    started = await store.started_lessons(conn, owner_id, course_id)
    removed = 0
    for plan_lesson_id, (lesson_id, first, last) in known.items():
        kept = plan_lesson_id in wanted
        if lesson_id in started and (not kept or wanted[plan_lesson_id] != (first, last)):
            raise PipelineError("started_lesson_dropped")
        if kept:
            continue
        await conn.execute(
            text(
                "DELETE FROM course_plans WHERE owner_id = :o AND plan_course_id = :c AND plan_lesson_id = :p"
            ),
            {"o": owner_id, "c": plan.course_id, "p": plan_lesson_id},
        )
        try:
            await store.delete_unstarted_lesson(conn, owner_id, lesson_id)  # re-checks "not started"
        except LessonWriteError:
            raise PipelineError("started_lesson_dropped") from None
        removed += 1
    return removed


async def _final_order(
    conn: AsyncConnection,
    course_id: uuid.UUID,
    ids: list[tuple[str, uuid.UUID]],
    known: dict[str, tuple[uuid.UUID, int, int]],
) -> list[uuid.UUID]:
    """FR-PIPELINE-14's renumbering: plan order, or SRV-PLAN FR-PLAN-21's placement for a manual order."""
    plan_order = [lesson_id for _, lesson_id in ids]
    source = (
        await conn.execute(text("SELECT order_source FROM courses WHERE id = :c"), {"c": course_id})
    ).scalar_one()
    if source != "manual":
        return plan_order
    rows = (
        await conn.execute(
            text("SELECT id, deleted_at IS NOT NULL FROM lessons WHERE course_id = :c ORDER BY ordinal"),
            {"c": course_id},
        )
    ).all()
    kept = {lesson_id for lesson_id, _, _ in known.values()}
    return order.placement([r[0] for r in rows], plan_order, kept, {r[0] for r in rows if r[1]})


async def import_course_plan(
    conn: AsyncConnection, owner_id: uuid.UUID, plan: Plan, *, origin: str
) -> ImportResult:
    """FR-PIPELINE-1's import as a function; the caller holds `lock_course` for this course."""
    if not await material_imported(conn, owner_id, plan.material_sha256):
        raise PipelineError("no_material")
    rows = (
        await conn.execute(
            text(
                "SELECT plan_lesson_id, lesson_id, pdf_first, pdf_last, origin FROM course_plans"
                " WHERE owner_id = :o AND plan_course_id = :c"
            ),
            {"o": owner_id, "c": plan.course_id},
        )
    ).all()
    if any(r[4] != origin for r in rows):
        raise PipelineError("origin_conflict")
    known: dict[str, tuple[uuid.UUID, int, int]] = {r[0]: (r[1], r[2], r[3]) for r in rows}
    if known:
        first_lesson = next(iter(known.values()))[0]
        course_id: uuid.UUID = (
            await conn.execute(text("SELECT course_id FROM lessons WHERE id = :l"), {"l": first_lesson})
        ).scalar_one()
        deleted = await conn.execute(
            text("SELECT 1 FROM courses WHERE id = :c AND deleted_at IS NOT NULL"), {"c": course_id}
        )
        if deleted.first() is not None:  # FR-PIPELINE-22: restore the course first; nothing is written
            raise CourseDeleted(course_id)
    else:
        course_id = await store.create_course(conn, owner_id, plan.title, plan.material_sha256)
    removed = 0
    if origin == "study_plan":
        removed = await _remove_unstarted(conn, owner_id, plan, course_id, known)
        wanted = {lesson.id for lesson in plan.lessons}
        known = {k: v for k, v in known.items() if k in wanted}
    created = 0
    ids: list[tuple[str, uuid.UUID]] = []
    # S5: lessons map by plan lesson id; a new one takes the next free ordinal, never an existing lesson's.
    next_ordinal = int(
        (
            await conn.execute(
                text("SELECT coalesce(max(ordinal), 0) FROM lessons WHERE course_id = :c"), {"c": course_id}
            )
        ).scalar_one()
    )
    for lesson in plan.lessons:
        if lesson.id in known:
            lesson_id, first, last = known[lesson.id]
            if (first, last) != (lesson.first, lesson.last):
                await conn.execute(
                    text(
                        "UPDATE course_plans SET pdf_first = :f, pdf_last = :t, g1_at = now()"
                        " WHERE owner_id = :o AND plan_course_id = :c AND plan_lesson_id = :p"
                    ),
                    {"f": lesson.first, "t": lesson.last, "o": owner_id, "c": plan.course_id, "p": lesson.id},
                )
        else:
            next_ordinal += 1
            created += 1
            lesson_id = await store.ensure_lesson(conn, owner_id, course_id, next_ordinal, lesson.title)
            await conn.execute(
                text(
                    "INSERT INTO course_plans (owner_id, plan_course_id, plan_lesson_id, lesson_id,"
                    " material_sha256, pdf_first, pdf_last, origin) VALUES (:o, :c, :p, :l, :m, :f, :t, :g)"
                ),
                {
                    "o": owner_id,
                    "c": plan.course_id,
                    "p": lesson.id,
                    "l": lesson_id,
                    "m": plan.material_sha256,
                    "f": lesson.first,
                    "t": lesson.last,
                    "g": origin,
                },
            )
        ids.append((lesson.id, lesson_id))
    if origin == "study_plan":
        final = await _final_order(conn, course_id, ids, known)
        await store.reorder_lessons(conn, owner_id, course_id, final)
    ordinals = {
        r[0]: int(r[1])
        for r in await conn.execute(
            text("SELECT id, ordinal FROM lessons WHERE course_id = :c"), {"c": course_id}
        )
    }
    return ImportResult(course_id, [(p, lid, ordinals[lid]) for p, lid in ids], created, removed)


async def import_plan(conn: AsyncConnection, owner_id: uuid.UUID, raw: Any) -> dict[str, Any]:
    """The manifest route's import (FR-PIPELINE-1): behaviour unchanged, now under the course lock."""
    plan = parse_plan(raw)
    await lock_course(conn, owner_id, plan.course_id)
    result = await import_course_plan(conn, owner_id, plan, origin="manifest")
    return {
        "course_id": str(result.course_id),
        "lessons": [{"plan_lesson_id": p, "lesson_id": str(lid)} for p, lid, _ in result.lessons],
    }
