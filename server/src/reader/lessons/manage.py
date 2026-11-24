"""The management layer over courses and lessons (SRV-LESSON FR-LESSON-20..31, INV-LESSON-20..23).

Every function takes an `AsyncConnection` inside the caller's transaction, never commits, and raises
`ManageError(code)` before any write it refuses (the caller's transaction then rolls back). Each applied
change records its audit row (SRV-EVENTS FR-EVENTS-30) in the same transaction; a no-op writes nothing.

Deletes are tombstones (`deleted_at`): nothing here updates or deletes lesson revisions, stage records or
spend rows.

Lock order (INV-LESSON-23), the one `create_run` follows: the plan-course locks (`lock_course`, "RCPC") of
every affected course in ascending `plan_course_id`, then the run locks ("RCMP") of every affected lesson in
ascending lesson id, then the owner's position lock ("RCPO", `store.lock_positions`); only then reads of run
state and tombstones, and writes.

Interface for SRV-INGEST FR-INGEST-31 (the material cascade):
  - `delete_courses(conn, owner_id, course_ids, *, by_material, actor_id)` soft-deletes several courses in one
    transaction under the full lock order taken over the whole set before any check; an open run of any of
    their lessons raises `ManageError("compile_running")` before anything is written; returns one impact body
    per course.
  - `restore_courses_by_material(conn, owner_id, material_id, actor_id)` restores the courses that material's
    cascade deleted (FR-LESSON-25's rule with "first"); call it after clearing the material's `deleted_at`.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import anyio.to_thread
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from reader import blobs
from reader.jobs import audit
from reader.lessons import store
from reader.lessons.schema import LessonWriteError
from reader.pipeline.courses import lock_course
from reader.pipeline.runs import LOCK_NAMESPACE as RUN_LOCK_NAMESPACE

TITLE_MAX: Final = 200
DESCRIPTION_MAX: Final = 2000
CONFIRM_CHARS: Final = 40
NOTE_MAX: Final = 10000
NOTES_PER_LESSON: Final = 500
BOOKMARKS_PER_LESSON: Final = 200
# pg_advisory_xact_lock(namespace, hashtext(user:lesson)): one writer of a reader's notes and bookmarks
# ("RCNB"), so the per-lesson caps hold under concurrent requests.
READER_LOCK_NAMESPACE: Final = 0x52434E42
CELL_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
STATES: Final = ("active", "archived", "deleted")


class ManageError(Exception):
    """A refused request: `code` from SRV-LESSON `route_errors`, its HTTP status, extra body keys."""

    STATUS: Final[Mapping[str, int]] = {
        "not_found": 404,
        "unknown_revision": 404,
        "invalid_body": 400,
        "title_invalid": 400,
        "description_invalid": 400,
        "confirm_mismatch": 400,
        "note_invalid": 400,
        "cell_unknown": 400,
        "notes_cell_removed": 400,
        "blob_unavailable": 500,
    }

    def __init__(self, code: str, **extra: Any) -> None:
        super().__init__(code)
        self.code = code
        self.status = self.STATUS.get(code, 409)
        self.extra = extra


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


# --- locks ---


async def lock_plans(conn: AsyncConnection, owner_id: uuid.UUID, course_ids: Sequence[uuid.UUID]) -> None:
    """SRV-PIPELINE's `lock_course` for every plan course of these courses, in ascending plan course id."""
    plans = (
        await conn.execute(
            text(
                "SELECT DISTINCT p.plan_course_id FROM course_plans p JOIN lessons l ON l.id = p.lesson_id"
                " WHERE p.owner_id = :o AND l.course_id = ANY(CAST(:c AS uuid[]))"
            ),
            {"o": owner_id, "c": list(course_ids)},
        )
    ).scalars()
    for plan_course in sorted(plans):
        await lock_course(conn, owner_id, plan_course)


async def lock_courses(
    conn: AsyncConnection, owner_id: uuid.UUID, course_ids: Sequence[uuid.UUID], *, positions: bool
) -> list[uuid.UUID]:
    """INV-LESSON-23 over a set of courses: plan-course locks, then every lesson's run lock, then (with
    `positions`) the owner's position lock. Returns the lessons of those courses (deleted ones included)."""
    await lock_plans(conn, owner_id, course_ids)
    lessons = list(
        (
            await conn.execute(
                text("SELECT id FROM lessons WHERE owner_id = :o AND course_id = ANY(CAST(:c AS uuid[]))"),
                {"o": owner_id, "c": list(course_ids)},
            )
        ).scalars()
    )
    await lock_lessons(conn, lessons)
    if positions:
        await store.lock_positions(conn, owner_id)
    return lessons


async def lock_lessons(conn: AsyncConnection, lesson_ids: Sequence[uuid.UUID]) -> None:
    """The run locks of SRV-PIPELINE FR-PIPELINE-11, in ascending lesson id order."""
    for lesson in sorted(set(lesson_ids)):
        await conn.execute(
            text("SELECT pg_advisory_xact_lock(:ns, hashtext(:l))"),
            {"ns": RUN_LOCK_NAMESPACE, "l": str(lesson)},
        )


async def lock_positions(conn: AsyncConnection, owner_id: uuid.UUID) -> None:
    """The owner's position lock "RCPO" (FR-LESSON-20), last in INV-LESSON-23's order."""
    await store.lock_positions(conn, owner_id)


async def _refuse_open_runs(conn: AsyncConnection, lesson_ids: Sequence[uuid.UUID]) -> None:
    """An open run (finished_at and cancelled_at null: running, stalled, stage_failed) refuses the change."""
    found = (
        await conn.execute(
            text(
                "SELECT 1 FROM compile_runs WHERE lesson_id = ANY(CAST(:l AS uuid[]))"
                " AND finished_at IS NULL AND cancelled_at IS NULL LIMIT 1"
            ),
            {"l": list(lesson_ids)},
        )
    ).first()
    if found is not None:
        raise ManageError("compile_running")


# --- field rules ---


def clean_title(value: Any) -> str:
    """FR-LESSON-22: trimmed, NFC, 1..200 characters, no Cc characters."""
    if not isinstance(value, str):
        raise ManageError("title_invalid")
    title = unicodedata.normalize("NFC", value).strip()
    if not 1 <= len(title) <= TITLE_MAX or any(unicodedata.category(ch) == "Cc" for ch in title):
        raise ManageError("title_invalid")
    return title


def clean_description(value: Any) -> str | None:
    if not isinstance(value, str):
        raise ManageError("description_invalid")
    description = unicodedata.normalize("NFC", value).strip()
    if len(description) > DESCRIPTION_MAX:
        raise ManageError("description_invalid")
    return description or None


def _version(body: Mapping[str, Any]) -> int:
    value = body.get("version")
    if type(value) is not int:
        raise ManageError("invalid_body")
    return value


def _ids(value: Any) -> list[uuid.UUID] | None:
    """A JSON list of canonical uuid strings, or None when it is not one."""
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        return None
    try:
        return [uuid.UUID(x) for x in value]
    except ValueError:
        return None


def _at(body: Mapping[str, Any]) -> str:
    at = body.get("at", "first")
    if at not in ("previous", "first"):
        raise ManageError("invalid_body")
    return str(at)


# --- courses ---

_COURSE: Final = (
    "SELECT c.id, c.title, c.description, c.position, c.archived_at, c.deleted_at, c.version,"
    " (SELECT count(*) FROM lessons l WHERE l.course_id = c.id AND l.deleted_at IS NULL),"
    " (SELECT m.config_id FROM materials m"
    "  WHERE m.owner_id = c.owner_id AND m.sha256 = c.material_sha256),"
    " sp.total_weeks, sp.minutes_per_day, sp.start_date"
    " FROM courses c LEFT JOIN study_plans sp ON sp.course_id = c.id WHERE c.owner_id = :o"
)


def _course_json(row: Any) -> dict[str, Any]:
    state = "deleted" if row[5] is not None else "archived" if row[4] is not None else "active"
    plan = None
    if row[9] is not None:
        finish = row[11] + timedelta(days=7 * int(row[9]) - 1)  # the deadline keeps the first start date
        plan = {
            "total_weeks": row[9],
            "minutes_per_day": row[10],
            "start_date": row[11].isoformat(),
            "finish_by": finish.isoformat(),
        }
    return {
        "course_id": str(row[0]),
        "title": row[1],
        "description": row[2],
        "position": row[3],
        "state": state,
        "archived_at": _iso(row[4]),
        "deleted_at": _iso(row[5]),
        "version": row[6],
        "lesson_count": row[7],
        "material_id": row[8],
        "plan": plan,
    }


async def course_body(
    conn: AsyncConnection, owner_id: uuid.UUID, course_id: uuid.UUID
) -> dict[str, Any] | None:
    """FR-LESSON-21's row of one course in any state, or None when it is not the owner's."""
    row = (await conn.execute(text(_COURSE + " AND c.id = :c"), {"o": owner_id, "c": course_id})).first()
    return None if row is None else _course_json(row)


_STATE_SQL: Final = {
    "active": " AND c.position IS NOT NULL ORDER BY c.position, c.created_at",
    "archived": " AND c.archived_at IS NOT NULL AND c.deleted_at IS NULL"
    " ORDER BY c.archived_at DESC, c.created_at",
    "deleted": " AND c.deleted_at IS NOT NULL ORDER BY c.deleted_at DESC, c.created_at",
}


async def list_courses(conn: AsyncConnection, owner_id: uuid.UUID, state: str) -> list[dict[str, Any]]:
    """FR-LESSON-21: the owner's courses in one state, active by position, the others newest first."""
    rows = await conn.execute(text(_COURSE + _STATE_SQL[state]), {"o": owner_id})
    return [_course_json(r) for r in rows]


async def _course_for_update(conn: AsyncConnection, owner_id: uuid.UUID, course_id: uuid.UUID) -> Any:
    row = (
        await conn.execute(
            text(
                "SELECT title, description, version, deleted_at, archived_at, material_sha256, position,"
                " last_position FROM courses WHERE id = :c AND owner_id = :o FOR UPDATE"
            ),
            {"c": course_id, "o": owner_id},
        )
    ).first()
    if row is None:
        raise ManageError("not_found")
    return row


async def _current_course(
    conn: AsyncConnection, owner_id: uuid.UUID, course_id: uuid.UUID
) -> dict[str, Any]:
    body = await course_body(conn, owner_id, course_id)
    if body is None:
        raise ManageError("not_found")
    return body


async def _title_taken(conn: AsyncConnection, owner_id: uuid.UUID, course_id: uuid.UUID, title: str) -> bool:
    found = await conn.execute(
        text(
            "SELECT 1 FROM courses WHERE owner_id = :o AND id <> :c AND title = :t AND deleted_at IS NULL"
            " LIMIT 1"
        ),
        {"o": owner_id, "c": course_id, "t": title},
    )
    return found.first() is not None


async def edit_course(
    conn: AsyncConnection, owner_id: uuid.UUID, course_id: uuid.UUID, body: Mapping[str, Any]
) -> dict[str, Any]:
    """FR-LESSON-22: 404, field rules, the no-op, `stale`, `title_taken`, in that order."""
    row = await _course_for_update(conn, owner_id, course_id)
    if row.deleted_at is not None:
        raise ManageError("not_found")
    given: dict[str, Any] = {}
    if "title" in body:
        given["title"] = clean_title(body["title"])
    if "description" in body:
        given["description"] = clean_description(body["description"])
    if not given:
        raise ManageError("invalid_body")
    version = _version(body)
    changed = {k: v for k, v in given.items() if v != getattr(row, k)}
    if not changed:
        return await _current_course(conn, owner_id, course_id)
    if version != row.version:
        raise ManageError("stale", course=await _current_course(conn, owner_id, course_id))
    unsourced = row.material_sha256 is None
    if "title" in changed and unsourced and await _title_taken(conn, owner_id, course_id, changed["title"]):
        raise ManageError("title_taken")
    sets = ", ".join(f"{k} = :{k}" for k in changed)
    try:
        async with conn.begin_nested():
            await conn.execute(
                text(f"UPDATE courses SET {sets}, version = version + 1 WHERE id = :c"),  # noqa: S608
                {**changed, "c": course_id},
            )
    except IntegrityError:
        raise ManageError("title_taken") from None
    for key, action in (("title", "course.rename"), ("description", "course.describe")):
        if key in changed:
            await audit.record(conn, owner_id, action, str(course_id), {"version": row.version + 1})
    return await _current_course(conn, owner_id, course_id)


async def _active_order(conn: AsyncConnection, owner_id: uuid.UUID) -> list[uuid.UUID]:
    rows = await conn.execute(
        text("SELECT id FROM courses WHERE owner_id = :o AND position IS NOT NULL ORDER BY position"),
        {"o": owner_id},
    )
    return list(rows.scalars())


async def _renumber(conn: AsyncConnection, owner_id: uuid.UUID, order: Sequence[uuid.UUID]) -> None:
    """Positions 1..n in `order` (the owner's positioned courses), in two statements: first every position
    past the current maximum, then the final values (the partial unique index is not deferrable)."""
    await conn.execute(
        text(
            "UPDATE courses SET position = position"
            " + (SELECT max(position) FROM courses WHERE owner_id = :o)"
            " WHERE owner_id = :o AND position IS NOT NULL"
        ),
        {"o": owner_id},
    )
    await conn.execute(
        text(
            "UPDATE courses SET position = v.n"
            " FROM unnest(CAST(:ids AS uuid[]), CAST(:ns AS int[])) AS v(id, n)"
            " WHERE courses.id = v.id AND courses.owner_id = :o"
        ),
        {"ids": list(order), "ns": list(range(1, len(order) + 1)), "o": owner_id},
    )


async def order_courses(
    conn: AsyncConnection, owner_id: uuid.UUID, body: Mapping[str, Any]
) -> dict[str, Any]:
    """FR-LESSON-23: exactly the active courses, else `order_mismatch` with the current list."""
    ids = _ids(body.get("course_ids"))
    if ids is None:
        raise ManageError("invalid_body")
    await store.lock_positions(conn, owner_id)
    current = await _active_order(conn, owner_id)
    if len(ids) != len(set(ids)) or set(ids) != set(current):
        raise ManageError("order_mismatch", course_ids=[str(x) for x in current])
    if ids != current:
        await _renumber(conn, owner_id, ids)
        await audit.record(conn, owner_id, "course.order", str(owner_id), {"count": len(ids)})
    return {"course_ids": [str(x) for x in ids]}


async def _vacate(
    conn: AsyncConnection,
    owner_id: uuid.UUID,
    course_ids: Sequence[uuid.UUID],
    sets: str,
    params: Mapping[str, Any] | None = None,
) -> None:
    """Archive or delete: `sets`, last_position := position, position := null; then close the gaps."""
    await conn.execute(
        text(
            f"UPDATE courses SET {sets}, last_position = coalesce(position, last_position),"  # noqa: S608
            " position = NULL WHERE owner_id = :o AND id = ANY(CAST(:c AS uuid[]))"
        ),
        {**(params or {}), "o": owner_id, "c": list(course_ids)},
    )
    await _renumber(conn, owner_id, await _active_order(conn, owner_id))


async def archive_course(conn: AsyncConnection, owner_id: uuid.UUID, course_id: uuid.UUID) -> dict[str, Any]:
    """FR-LESSON-24: under the header's locks; refused while a lesson has an open run."""
    lessons = await lock_courses(conn, owner_id, [course_id], positions=True)
    row = await _course_for_update(conn, owner_id, course_id)
    if row.deleted_at is not None:
        raise ManageError("not_found")
    if row.archived_at is None:
        await _refuse_open_runs(conn, lessons)
        await _vacate(conn, owner_id, [course_id], "archived_at = now()")
        await audit.record(conn, owner_id, "course.archive", str(course_id), {})
    return await _current_course(conn, owner_id, course_id)


async def unarchive_course(
    conn: AsyncConnection, owner_id: uuid.UUID, course_id: uuid.UUID, body: Mapping[str, Any]
) -> dict[str, Any]:
    at = _at(body)
    await store.lock_positions(conn, owner_id)
    row = await _course_for_update(conn, owner_id, course_id)
    if row.deleted_at is not None:
        raise ManageError("not_found")
    if row.archived_at is not None:
        await store.place_at(conn, owner_id, course_id, (row.last_position or 1) if at == "previous" else 1)
        await audit.record(conn, owner_id, "course.unarchive", str(course_id), {})
    return await _current_course(conn, owner_id, course_id)


async def impact(conn: AsyncConnection, owner_id: uuid.UUID, course_id: uuid.UUID) -> dict[str, int]:
    """FR-LESSON-25: counts over the course (read-only)."""
    row = (
        await conn.execute(
            text(
                "WITH ls AS (SELECT id FROM lessons WHERE course_id = :c AND owner_id = :o) SELECT"
                " (SELECT count(*) FROM ls),"
                " (SELECT count(*) FROM lesson_revisions WHERE lesson_id IN (SELECT id FROM ls)),"
                " (SELECT count(*) FROM tutor_questions WHERE lesson_id IN (SELECT id FROM ls)),"
                " (SELECT count(*) FROM compile_runs WHERE lesson_id IN (SELECT id FROM ls)),"
                " (SELECT count(*) FROM agent_spend WHERE lesson_id IN (SELECT id FROM ls)),"
                " (SELECT count(*) FROM compile_runs WHERE lesson_id IN (SELECT id FROM ls)"
                "  AND finished_at IS NULL AND cancelled_at IS NULL)"
            ),
            {"c": course_id, "o": owner_id},
        )
    ).one()
    keys = ("lessons", "revisions", "tutor_questions", "compile_runs", "spend_rows", "open_runs")
    return {k: int(v) for k, v in zip(keys, row, strict=True)}


async def course_impact(conn: AsyncConnection, owner_id: uuid.UUID, course_id: uuid.UUID) -> dict[str, int]:
    await _current_course(conn, owner_id, course_id)
    return await impact(conn, owner_id, course_id)


def confirms(confirm: Any, title: str) -> bool:
    """FR-LESSON-25: the target is the title NFC-normalised, cut to 40 code points, then trimmed; `confirm` is
    NFC-normalised and trimmed (SRV-INGEST FR-INGEST-31 and WEB-MANAGE FR-MANAGE-2 use the same rule). An
    empty target matches nothing, the empty `confirm` included."""
    if not isinstance(confirm, str):
        return False
    expected = unicodedata.normalize("NFC", title)[:CONFIRM_CHARS].strip()
    return expected != "" and unicodedata.normalize("NFC", confirm).strip() == expected


async def delete_courses(
    conn: AsyncConnection,
    owner_id: uuid.UUID,
    course_ids: Sequence[uuid.UUID],
    *,
    by_material: str | None,
    actor_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Soft-delete the owner's courses in one transaction (FR-LESSON-25; SRV-INGEST FR-INGEST-31's cascade).

    Takes INV-LESSON-23's locks over the whole set (plan-course locks ascending, every lesson's run lock
    ascending, then "RCPO") before any check. Raises `ManageError("not_found")` if any id is not the owner's
    course and `ManageError("compile_running")` if any lesson of a course not yet deleted has an open run,
    both before any write. Courses already deleted are left as they are. Each course deleted here gets
    `deleted_at` = now, `deleted_by_material` = `by_material` (the material id, or None for a course's own
    delete), `last_position`, a null position, and one `course.delete` audit row with its counts and
    `by_material: bool`. Returns, in input order, `{course_id, deleted_at, lessons, revisions,
    tutor_questions, compile_runs, spend_rows, open_runs}`.
    """
    wanted = list(dict.fromkeys(course_ids))
    found = (
        await conn.execute(
            text("SELECT id FROM courses WHERE owner_id = :o AND id = ANY(CAST(:c AS uuid[]))"),
            {"o": owner_id, "c": wanted},
        )
    ).scalars()
    if set(found) != set(wanted):
        raise ManageError("not_found")
    await lock_courses(conn, owner_id, wanted, positions=True)
    pending = list(
        (
            await conn.execute(
                text(
                    "SELECT id FROM courses WHERE owner_id = :o AND id = ANY(CAST(:c AS uuid[]))"
                    " AND deleted_at IS NULL FOR UPDATE"
                ),
                {"o": owner_id, "c": wanted},
            )
        ).scalars()
    )
    if pending:
        lessons = (
            await conn.execute(
                text("SELECT id FROM lessons WHERE course_id = ANY(CAST(:c AS uuid[]))"), {"c": pending}
            )
        ).scalars()
        await _refuse_open_runs(conn, list(lessons))
        counts = {c: await impact(conn, owner_id, c) for c in pending}
        sets = "deleted_at = now(), deleted_by_material = :m"
        await _vacate(conn, owner_id, pending, sets, {"m": by_material})
        for course in pending:
            detail = {k: v for k, v in counts[course].items() if k != "open_runs"}
            detail["by_material"] = by_material is not None
            await audit.record(conn, actor_id, "course.delete", str(course), detail)
    out = []
    for course in wanted:
        deleted_at = (
            await conn.execute(text("SELECT deleted_at FROM courses WHERE id = :c"), {"c": course})
        ).scalar_one()
        counted = await impact(conn, owner_id, course)
        out.append({"course_id": str(course), **counted, "deleted_at": _iso(deleted_at)})
    return out


async def delete_course(
    conn: AsyncConnection, owner_id: uuid.UUID, course_id: uuid.UUID, body: Mapping[str, Any]
) -> dict[str, Any]:
    """`DELETE /api/courses/{id}`: the confirm text, then `delete_courses` with one id."""
    row = await _current_course(conn, owner_id, course_id)
    if not confirms(body.get("confirm"), row["title"]):
        raise ManageError("confirm_mismatch")
    (result,) = await delete_courses(conn, owner_id, [course_id], by_material=None, actor_id=owner_id)
    return {k: v for k, v in result.items() if k != "course_id"}


async def material_is_live(conn: AsyncConnection, owner_id: uuid.UUID, sha256: str | None) -> bool:
    """FR-LESSON-25's `material_deleted` check: the course's material row, read FOR SHARE so a concurrent
    material delete cannot interleave (no write skew); a deleted material (SRV-INGEST FR-INGEST-31) is not
    live."""
    if sha256 is None:
        return True
    found = await conn.execute(
        text("SELECT 1 FROM materials WHERE owner_id = :o AND sha256 = :s AND deleted_at IS NULL FOR SHARE"),
        {"o": owner_id, "s": sha256},
    )
    return found.first() is not None


async def _restore(
    conn: AsyncConnection,
    owner_id: uuid.UUID,
    actor_id: uuid.UUID,
    course_id: uuid.UUID,
    row: Any,
    at: str,
    *,
    by_material: bool,
) -> None:
    """FR-LESSON-25's rule: an archived course returns archived (no position); any other is placed."""
    await conn.execute(text("UPDATE courses SET deleted_by_material = NULL WHERE id = :c"), {"c": course_id})
    if row.archived_at is not None:
        await conn.execute(text("UPDATE courses SET deleted_at = NULL WHERE id = :c"), {"c": course_id})
    else:
        await store.place_at(conn, owner_id, course_id, (row.last_position or 1) if at == "previous" else 1)
    await audit.record(conn, actor_id, "course.restore", str(course_id), {"by_material": by_material})


async def restore_course(
    conn: AsyncConnection, owner_id: uuid.UUID, course_id: uuid.UUID, body: Mapping[str, Any]
) -> dict[str, Any]:
    at = _at(body)
    await store.lock_positions(conn, owner_id)
    row = await _course_for_update(conn, owner_id, course_id)
    if row.deleted_at is not None:
        if not await material_is_live(conn, owner_id, row.material_sha256):
            raise ManageError("material_deleted")
        if row.material_sha256 is None and await _title_taken(conn, owner_id, course_id, row.title):
            raise ManageError("title_taken")
        await _restore(conn, owner_id, owner_id, course_id, row, at, by_material=False)
    return await _current_course(conn, owner_id, course_id)


async def restore_courses_by_material(
    conn: AsyncConnection, owner_id: uuid.UUID, material_id: str, actor_id: uuid.UUID
) -> list[uuid.UUID]:
    """SRV-INGEST FR-INGEST-31's cascade restore: every course that material's cascade deleted comes back by
    FR-LESSON-25's rule with "first" (archived ones stay archived), keeping their relative order; one
    `course.restore` audit row each (`by_material: true`). Call it after clearing the material's `deleted_at`;
    returns the restored course ids."""
    await store.lock_positions(conn, owner_id)
    rows = (
        await conn.execute(
            text(
                "SELECT id, archived_at, last_position FROM courses WHERE owner_id = :o"
                " AND deleted_by_material = :m AND deleted_at IS NOT NULL"
                " ORDER BY last_position DESC NULLS FIRST, id FOR UPDATE"
            ),
            {"o": owner_id, "m": material_id},
        )
    ).all()
    for row in rows:
        await _restore(conn, owner_id, actor_id, row.id, row, "first", by_material=True)
    return [row.id for row in rows]


async def deleted_lessons(conn: AsyncConnection, owner_id: uuid.UUID, course_id: uuid.UUID) -> dict[str, Any]:
    """FR-LESSON-21: a course's deleted lessons, newest deletion first (the course must not be deleted)."""
    course = await _current_course(conn, owner_id, course_id)
    if course["state"] == "deleted":
        raise ManageError("not_found")
    rows = await conn.execute(
        text(
            "SELECT id, title, ordinal, deleted_at FROM lessons WHERE course_id = :c"
            " AND deleted_at IS NOT NULL ORDER BY deleted_at DESC, id"
        ),
        {"c": course_id},
    )
    return {
        "lessons": [
            {"lesson_id": str(r[0]), "title": r[1], "ordinal": r[2], "deleted_at": _iso(r[3])} for r in rows
        ]
    }


# --- lessons ---

# FR-LESSON-7's row (with the amendment's additive fields); `:u` is the reader, the course owner in stage 1.
LESSON_ROW: Final = (
    "SELECT l.id, l.course_id, c.title, l.ordinal, l.title, l.updated_at,"
    " (SELECT s.sha256 FROM lesson_revision_stages s WHERE s.lesson_id = l.id ORDER BY s.id DESC LIMIT 1),"
    " l.skipped_at IS NOT NULL,"
    " EXISTS (SELECT 1 FROM lesson_progress p WHERE p.user_id = :u AND p.lesson_id = l.id),"
    " c.position, l.version"
    " FROM lessons l JOIN courses c ON c.id = l.course_id"
    " WHERE c.owner_id = :u AND l.deleted_at IS NULL AND c.deleted_at IS NULL"
)


def lesson_json(row: Any) -> dict[str, Any]:
    return {
        "lesson_id": str(row[0]),
        "course_id": str(row[1]),
        "course_title": row[2],
        "ordinal": row[3],
        "title": row[4],
        "updated_at": _iso(row[5]),
        "latest_sha256": row[6],
        "skipped": row[7],
        "done": row[8],
        "course_position": row[9],
        "version": row[10],
    }


async def lesson_body(conn: AsyncConnection, user_id: uuid.UUID, lesson_id: uuid.UUID) -> dict[str, Any]:
    row = (await conn.execute(text(LESSON_ROW + " AND l.id = :l"), {"u": user_id, "l": lesson_id})).first()
    if row is None:
        raise ManageError("not_found")
    return lesson_json(row)


async def _lesson_for_update(conn: AsyncConnection, owner_id: uuid.UUID, lesson_id: uuid.UUID) -> Any:
    """The owner's lesson row, locked; a lesson of a deleted course is not found."""
    row = (
        await conn.execute(
            text(
                "SELECT l.course_id, l.title, l.version, l.skipped_at, l.deleted_at FROM lessons l"
                " JOIN courses c ON c.id = l.course_id"
                " WHERE l.id = :l AND l.owner_id = :o AND c.deleted_at IS NULL FOR UPDATE OF l"
            ),
            {"l": lesson_id, "o": owner_id},
        )
    ).first()
    if row is None:
        raise ManageError("not_found")
    return row


async def _lock_lesson(conn: AsyncConnection, owner_id: uuid.UUID, lesson_id: uuid.UUID) -> Any:
    """INV-LESSON-23 for one lesson: its course's plan lock, its run lock, then the row."""
    course = (
        await conn.execute(
            text("SELECT course_id FROM lessons WHERE id = :l AND owner_id = :o"),
            {"l": lesson_id, "o": owner_id},
        )
    ).scalar()
    if course is None:
        raise ManageError("not_found")
    await lock_plans(conn, owner_id, [course])
    await lock_lessons(conn, [lesson_id])
    return await _lesson_for_update(conn, owner_id, lesson_id)


def _lesson_state(lesson_id: uuid.UUID, skipped_at: Any, deleted_at: Any) -> dict[str, Any]:
    return {"lesson_id": str(lesson_id), "skipped_at": _iso(skipped_at), "deleted_at": _iso(deleted_at)}


async def rename_lesson(
    conn: AsyncConnection, owner_id: uuid.UUID, lesson_id: uuid.UUID, body: Mapping[str, Any]
) -> dict[str, Any]:
    """FR-LESSON-26: the title rule, the no-op, `stale`, then `compile_running` under the run lock."""
    title = clean_title(body.get("title"))
    version = _version(body)
    row = await _lock_lesson(conn, owner_id, lesson_id)
    if row.deleted_at is not None:
        raise ManageError("not_found")
    if title == row.title:
        return await lesson_body(conn, owner_id, lesson_id)
    if version != row.version:
        raise ManageError("stale", lesson=await lesson_body(conn, owner_id, lesson_id))
    await _refuse_open_runs(conn, [lesson_id])
    await conn.execute(
        text("UPDATE lessons SET title = :t, version = version + 1 WHERE id = :l"),
        {"t": title, "l": lesson_id},
    )
    await audit.record(conn, owner_id, "lesson.rename", str(lesson_id), {"version": row.version + 1})
    return await lesson_body(conn, owner_id, lesson_id)


async def set_lesson_flag(
    conn: AsyncConnection, owner_id: uuid.UUID, lesson_id: uuid.UUID, action: str
) -> dict[str, Any]:
    """FR-LESSON-28: skip, unskip, delete and restore, each idempotent; skip and delete refuse an open run."""
    column, value = {
        "skip": ("skipped_at", True),
        "unskip": ("skipped_at", False),
        "delete": ("deleted_at", True),
        "restore": ("deleted_at", False),
    }[action]
    row = await _lock_lesson(conn, owner_id, lesson_id)
    if column == "skipped_at" and row.deleted_at is not None:
        raise ManageError("not_found")
    if (getattr(row, column) is not None) != value:
        if value:
            await _refuse_open_runs(conn, [lesson_id])
        await conn.execute(
            text(f"UPDATE lessons SET {column} = {'now()' if value else 'NULL'} WHERE id = :l"),  # noqa: S608
            {"l": lesson_id},
        )
        await audit.record(conn, owner_id, f"lesson.{action}", str(lesson_id), {})
    state = (
        await conn.execute(text("SELECT skipped_at, deleted_at FROM lessons WHERE id = :l"), {"l": lesson_id})
    ).one()
    return _lesson_state(lesson_id, state[0], state[1])


async def order_lessons(
    conn: AsyncConnection, owner_id: uuid.UUID, course_id: uuid.UUID, body: Mapping[str, Any]
) -> dict[str, Any]:
    """FR-LESSON-27: exactly the visible lessons; deleted ones keep their slots after them."""
    ids = _ids(body.get("lesson_ids"))
    if ids is None:
        raise ManageError("invalid_body")
    await lock_plans(conn, owner_id, [course_id])  # no interleaving with a re-plan's renumbering
    course = await _course_for_update(conn, owner_id, course_id)
    if course.deleted_at is not None:
        raise ManageError("not_found")
    rows = (
        await conn.execute(
            text("SELECT id, deleted_at IS NOT NULL FROM lessons WHERE course_id = :c ORDER BY ordinal"),
            {"c": course_id},
        )
    ).all()
    visible = [r[0] for r in rows if not r[1]]
    if len(ids) != len(set(ids)) or set(ids) != set(visible):
        raise ManageError("order_mismatch", lesson_ids=[str(x) for x in visible])
    if ids != visible:
        await store.reorder_lessons(conn, owner_id, course_id, ids + [r[0] for r in rows if r[1]])
        await conn.execute(text("UPDATE courses SET order_source = 'manual' WHERE id = :c"), {"c": course_id})
        await audit.record(conn, owner_id, "lesson.order", str(course_id), {"count": len(ids)})
    return {"lesson_ids": [str(x) for x in ids]}


async def _readable(conn: AsyncConnection, user_id: uuid.UUID, lesson_id: uuid.UUID) -> uuid.UUID:
    """FR-LESSON-29/-31's access: a lesson the reader can open (SRV-LESSON `can_open`); its owner id."""
    owner: uuid.UUID | None = (
        await conn.execute(
            text(
                "SELECT l.owner_id FROM lessons l JOIN courses c ON c.id = l.course_id WHERE l.id = :l"
                " AND c.owner_id = :u AND l.deleted_at IS NULL AND c.deleted_at IS NULL"
            ),
            {"l": lesson_id, "u": user_id},
        )
    ).scalar()
    if owner is None:
        raise ManageError("not_found")
    return owner


async def set_progress(
    conn: AsyncConnection, user_id: uuid.UUID, lesson_id: uuid.UUID, body: Mapping[str, Any]
) -> dict[str, Any]:
    """FR-LESSON-29: done keeps its first `done_at`; not done deletes the row."""
    done = body.get("done")
    if not isinstance(done, bool):
        raise ManageError("invalid_body")
    owner = await _readable(conn, user_id, lesson_id)
    if done:
        await conn.execute(
            text(
                "INSERT INTO lesson_progress (owner_id, user_id, lesson_id) VALUES (:o, :u, :l)"
                " ON CONFLICT (user_id, lesson_id) DO NOTHING"
            ),
            {"o": owner, "u": user_id, "l": lesson_id},
        )
        done_at = (
            await conn.execute(
                text("SELECT done_at FROM lesson_progress WHERE user_id = :u AND lesson_id = :l"),
                {"u": user_id, "l": lesson_id},
            )
        ).scalar_one()
        return {"done": True, "done_at": _iso(done_at)}
    await conn.execute(
        text("DELETE FROM lesson_progress WHERE user_id = :u AND lesson_id = :l"),
        {"u": user_id, "l": lesson_id},
    )
    return {"done": False, "done_at": None}


async def revert_lesson(
    conn: AsyncConnection,
    blobs_root: Path,
    owner_id: uuid.UUID,
    lesson_id: uuid.UUID,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    """FR-LESSON-30: write an earlier revision again through FR-LESSON-4 with stage `revert`."""
    sha256 = body.get("sha256")
    if not isinstance(sha256, str) or not blobs.is_sha256(sha256):
        raise ManageError("unknown_revision")
    row = await _lock_lesson(conn, owner_id, lesson_id)
    if row.deleted_at is not None:
        raise ManageError("not_found")
    known = await conn.execute(
        text("SELECT 1 FROM lesson_revisions WHERE lesson_id = :l AND sha256 = :s"),
        {"l": lesson_id, "s": sha256},
    )
    if known.first() is None:
        raise ManageError("unknown_revision")
    current = await store.current_sha256(conn, lesson_id)
    if sha256 == current:
        return {"applied": False, "latest_sha256": current}
    await _refuse_open_runs(conn, [lesson_id])
    try:
        data = await anyio.to_thread.run_sync(blobs.get, blobs_root, sha256)
        await store.write_revision(
            conn, blobs_root, owner_id, lesson_id, data, "revert", {"input_sha256": current}
        )
    except (blobs.BlobMissing, blobs.BlobCorrupt):
        raise ManageError("blob_unavailable") from None
    except LessonWriteError as e:
        raise ManageError(e.code) from None
    await audit.record(
        conn, owner_id, "lesson.revert", str(lesson_id), {"from_sha256": current, "to_sha256": sha256}
    )
    return {"applied": True, "latest_sha256": sha256}


# --- notes and bookmarks ---


async def _cells(conn: AsyncConnection, blobs_root: Path, lesson_id: uuid.UUID) -> frozenset[str]:
    """The cell ids of the lesson's current revision (none without a revision)."""
    current = await store.current_sha256(conn, lesson_id)
    if current is None:
        return frozenset()
    try:
        notebook = json.loads(await anyio.to_thread.run_sync(blobs.get, blobs_root, current))
    except (blobs.BlobMissing, blobs.BlobCorrupt):
        raise ManageError("blob_unavailable") from None
    return frozenset(str(c.get("id")) for c in notebook.get("cells", []))


async def _lock_reader(conn: AsyncConnection, user_id: uuid.UUID, lesson_id: uuid.UUID) -> None:
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:k))"),
        {"ns": READER_LOCK_NAMESPACE, "k": f"{user_id}:{lesson_id}"},
    )


def _note_json(row: Any) -> dict[str, Any]:
    return {
        "note_id": str(row[0]),
        "lesson_id": str(row[1]),
        "cell_id": row[2],
        "body": row[3],
        "created_at": _iso(row[4]),
        "updated_at": _iso(row[5]),
    }


_NOTE: Final = "SELECT id, lesson_id, cell_id, body, created_at, updated_at FROM lesson_notes"


def _note_body(value: Any) -> str:
    if not isinstance(value, str):
        raise ManageError("note_invalid")
    body = value.strip()
    if not 1 <= len(body) <= NOTE_MAX:
        raise ManageError("note_invalid")
    return body


async def list_notes(conn: AsyncConnection, user_id: uuid.UUID, lesson_id: uuid.UUID) -> dict[str, Any]:
    await _readable(conn, user_id, lesson_id)
    rows = await conn.execute(
        text(_NOTE + " WHERE user_id = :u AND lesson_id = :l ORDER BY created_at DESC, id"),
        {"u": user_id, "l": lesson_id},
    )
    return {"notes": [_note_json(r) for r in rows]}


async def add_note(
    conn: AsyncConnection, blobs_root: Path, user_id: uuid.UUID, lesson_id: uuid.UUID, body: Mapping[str, Any]
) -> dict[str, Any]:
    owner = await _readable(conn, user_id, lesson_id)
    text_body = _note_body(body.get("body"))
    cell_id = body.get("cell_id")
    if cell_id is not None and (
        not isinstance(cell_id, str) or cell_id not in await _cells(conn, blobs_root, lesson_id)
    ):
        raise ManageError("cell_unknown")
    await _lock_reader(conn, user_id, lesson_id)
    count = (
        await conn.execute(
            text("SELECT count(*) FROM lesson_notes WHERE user_id = :u AND lesson_id = :l"),
            {"u": user_id, "l": lesson_id},
        )
    ).scalar_one()
    if count >= NOTES_PER_LESSON:
        raise ManageError("limit")
    row = (
        await conn.execute(
            text(
                "INSERT INTO lesson_notes (owner_id, user_id, lesson_id, cell_id, body)"
                " VALUES (:o, :u, :l, :c, :b)"
                " RETURNING id, lesson_id, cell_id, body, created_at, updated_at"
            ),
            {"o": owner, "u": user_id, "l": lesson_id, "c": cell_id, "b": text_body},
        )
    ).one()
    return _note_json(row)


async def edit_note(
    conn: AsyncConnection,
    user_id: uuid.UUID,
    lesson_id: uuid.UUID,
    note_id: uuid.UUID,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    await _readable(conn, user_id, lesson_id)
    text_body = _note_body(body.get("body"))
    await conn.execute(
        text(
            "UPDATE lesson_notes SET body = :b, updated_at = clock_timestamp()"
            " WHERE id = :n AND user_id = :u AND lesson_id = :l AND body <> :b"
        ),
        {"b": text_body, "n": note_id, "u": user_id, "l": lesson_id},
    )
    row = (
        await conn.execute(
            text(_NOTE + " WHERE id = :n AND user_id = :u AND lesson_id = :l"),
            {"n": note_id, "u": user_id, "l": lesson_id},
        )
    ).first()
    if row is None:
        raise ManageError("not_found")
    return _note_json(row)


async def delete_note(
    conn: AsyncConnection, user_id: uuid.UUID, lesson_id: uuid.UUID, note_id: uuid.UUID
) -> dict[str, Any]:
    """A hard delete of the reader's own note; a repeat (or no note of the reader) changes nothing."""
    await _readable(conn, user_id, lesson_id)
    await conn.execute(
        text("DELETE FROM lesson_notes WHERE id = :n AND user_id = :u AND lesson_id = :l"),
        {"n": note_id, "u": user_id, "l": lesson_id},
    )
    return {"note_id": str(note_id), "deleted": True}


async def add_bookmark(
    conn: AsyncConnection, blobs_root: Path, user_id: uuid.UUID, lesson_id: uuid.UUID, cell_id: str
) -> dict[str, Any]:
    owner = await _readable(conn, user_id, lesson_id)
    if not CELL_ID.fullmatch(cell_id) or cell_id not in await _cells(conn, blobs_root, lesson_id):
        raise ManageError("cell_unknown")
    await _lock_reader(conn, user_id, lesson_id)
    existing = (
        await conn.execute(
            text(
                "SELECT created_at FROM lesson_bookmarks"
                " WHERE user_id = :u AND lesson_id = :l AND cell_id = :c"
            ),
            {"u": user_id, "l": lesson_id, "c": cell_id},
        )
    ).scalar()
    if existing is None:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM lesson_bookmarks WHERE user_id = :u AND lesson_id = :l"),
                {"u": user_id, "l": lesson_id},
            )
        ).scalar_one()
        if count >= BOOKMARKS_PER_LESSON:
            raise ManageError("limit")
        existing = (
            await conn.execute(
                text(
                    "INSERT INTO lesson_bookmarks (owner_id, user_id, lesson_id, cell_id)"
                    " VALUES (:o, :u, :l, :c)"
                    " RETURNING created_at"
                ),
                {"o": owner, "u": user_id, "l": lesson_id, "c": cell_id},
            )
        ).scalar_one()
    return {"lesson_id": str(lesson_id), "cell_id": cell_id, "created_at": _iso(existing)}


async def remove_bookmark(
    conn: AsyncConnection, user_id: uuid.UUID, lesson_id: uuid.UUID, cell_id: str
) -> dict[str, Any]:
    await _readable(conn, user_id, lesson_id)
    await conn.execute(
        text("DELETE FROM lesson_bookmarks WHERE user_id = :u AND lesson_id = :l AND cell_id = :c"),
        {"u": user_id, "l": lesson_id, "c": cell_id},
    )
    return {"lesson_id": str(lesson_id), "cell_id": cell_id, "deleted": True}


async def list_bookmarks(conn: AsyncConnection, blobs_root: Path, user_id: uuid.UUID) -> dict[str, Any]:
    """The reader's bookmarks on lessons that are not deleted; `stale` once the cell left the current
    revision."""
    rows = (
        await conn.execute(
            text(
                "SELECT b.lesson_id, c.id, l.title, b.cell_id, b.created_at FROM lesson_bookmarks b"
                " JOIN lessons l ON l.id = b.lesson_id JOIN courses c ON c.id = l.course_id"
                " WHERE b.user_id = :u AND c.owner_id = :u AND l.deleted_at IS NULL AND c.deleted_at IS NULL"
                " ORDER BY b.created_at DESC, b.lesson_id, b.cell_id"
            ),
            {"u": user_id},
        )
    ).all()
    cells: dict[uuid.UUID, frozenset[str]] = {}
    for row in rows:
        if row[0] not in cells:
            cells[row[0]] = await _cells(conn, blobs_root, row[0])
    return {
        "bookmarks": [
            {
                "lesson_id": str(r[0]),
                "course_id": str(r[1]),
                "lesson_title": r[2],
                "cell_id": r[3],
                "created_at": _iso(r[4]),
                "stale": r[3] not in cells[r[0]],
            }
            for r in rows
        ]
    }
