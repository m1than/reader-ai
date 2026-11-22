"""Course import, gate G1 (SRV-PIPELINE FR-PIPELINE-1).

A plan shaped like pilot/bashir/course.yaml (as JSON): `course` (an id string, or {id, title,
material_sha256}), `material` {sha256, title?} when `course` is a string, and `lessons` (or
`stages[].lessons`) with `id`, `title` and `pages.pdf` [first, last]. One `course_plans` row per lesson,
upserted by (owner, course id, lesson id); the lesson uuid never changes on a later import. The POST is the
owner's G1 approval.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from reader.lessons import store
from reader.pipeline.errors import PipelineError
from reader.pipeline.source import material_imported

SHA256: Final = re.compile(r"[0-9a-f]{64}")
TITLE_MAX: Final = 200


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


async def import_plan(conn: AsyncConnection, owner_id: uuid.UUID, raw: Any) -> dict[str, Any]:
    plan = parse_plan(raw)
    if not await material_imported(conn, owner_id, plan.material_sha256):
        raise PipelineError("no_material")
    known = {
        r[0]: (r[1], r[2], r[3])
        for r in await conn.execute(
            text(
                "SELECT plan_lesson_id, lesson_id, pdf_first, pdf_last FROM course_plans"
                " WHERE owner_id = :o AND plan_course_id = :c"
            ),
            {"o": owner_id, "c": plan.course_id},
        )
    }
    course_id: uuid.UUID | None = None
    if known:
        first_lesson = next(iter(known.values()))[0]
        course_id = (
            await conn.execute(text("SELECT course_id FROM lessons WHERE id = :l"), {"l": first_lesson})
        ).scalar_one()
    else:
        course_id = await store.create_course(conn, owner_id, plan.title, plan.material_sha256)
    out = []
    for ordinal, lesson in enumerate(plan.lessons, start=1):
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
            lesson_id = await store.ensure_lesson(conn, owner_id, course_id, ordinal, lesson.title)
            await conn.execute(
                text(
                    "INSERT INTO course_plans (owner_id, plan_course_id, plan_lesson_id, lesson_id,"
                    " material_sha256, pdf_first, pdf_last) VALUES (:o, :c, :p, :l, :m, :f, :t)"
                ),
                {
                    "o": owner_id,
                    "c": plan.course_id,
                    "p": lesson.id,
                    "l": lesson_id,
                    "m": plan.material_sha256,
                    "f": lesson.first,
                    "t": lesson.last,
                },
            )
        out.append({"plan_lesson_id": lesson.id, "lesson_id": str(lesson_id)})
    return {"course_id": str(course_id), "lessons": out}
