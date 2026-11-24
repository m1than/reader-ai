"""Shared helpers for the management-layer tests (SRV-LESSON FR-LESSON-20..31, INV-LESSON-20..23).

@trace FR-LESSON-20

A world with the owner ivan and two learners: anna, and olga, who owns courses of her own (the "second owner" of
INV-LESSON-20: SRV-AUTH allows one `owner` role, ownership of a course is per row); course and lesson builders through
SRV-LESSON's own
functions; open runs as compile_runs rows; a per-table content digest to prove "zero rows changed".
"""

from __future__ import annotations

import json
import textwrap
import uuid
from pathlib import Path
from typing import Any

from .conftest import F01, Pg, World, need, scalar

CONFIG = """\
version: 1
instance: { url: "https://localhost", data_dir: ./data }
users:
  - { handle: ivan, role: owner }
  - { handle: olga, role: learner }
  - { handle: anna, role: learner }
sharing: {}
"""
MATERIAL = "ab" * 32
# (method, path) of every route of the amendment in reader.lessons.routes (all `auth: session`).
MANAGE_ROUTES = frozenset(
    {
        ("GET", "/api/courses"),
        ("POST", "/api/courses/order"),
        ("POST", "/api/courses/{course_id}"),
        ("POST", "/api/courses/{course_id}/archive"),
        ("POST", "/api/courses/{course_id}/unarchive"),
        ("GET", "/api/courses/{course_id}/impact"),
        ("DELETE", "/api/courses/{course_id}"),
        ("POST", "/api/courses/{course_id}/restore"),
        ("GET", "/api/courses/{course_id}/lessons"),
        ("POST", "/api/courses/{course_id}/lessons/order"),
        ("POST", "/api/lessons/{lesson_id}"),
        ("POST", "/api/lessons/{lesson_id}/skip"),
        ("POST", "/api/lessons/{lesson_id}/unskip"),
        ("DELETE", "/api/lessons/{lesson_id}"),
        ("POST", "/api/lessons/{lesson_id}/restore"),
        ("POST", "/api/lessons/{lesson_id}/progress"),
        ("POST", "/api/lessons/{lesson_id}/revert"),
        ("GET", "/api/lessons/{lesson_id}/notes"),
        ("POST", "/api/lessons/{lesson_id}/notes"),
        ("POST", "/api/lessons/{lesson_id}/notes/{note_id}"),
        ("DELETE", "/api/lessons/{lesson_id}/notes/{note_id}"),
        ("POST", "/api/lessons/{lesson_id}/bookmarks/{cell_id}"),
        ("DELETE", "/api/lessons/{lesson_id}/bookmarks/{cell_id}"),
        ("GET", "/api/bookmarks"),
    }
)
# Tables that change on every request for reasons outside this amendment (sessions, auth bookkeeping, the queue).
VOLATILE = ("sessions", "auth_failures", "auth_challenges", "procrastinate_", "alembic_version")


async def material_tombstone_column(engine: Any) -> None:
    """SRV-INGEST FR-INGEST-31's `materials.deleted_at` (a parallel slice's migration); a no-op once merged."""
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE materials ADD COLUMN IF NOT EXISTS deleted_at timestamptz NULL"))


async def manage_world(pg: Pg, tmp: Path) -> World:
    from sqlalchemy import text

    from reader.auth.policy import settings_from_config
    from reader.auth.store import prepare
    from reader.config import Secret, load_config
    from reader.db import DbTarget, make_engine, migrate

    path = tmp / "reader.yaml"
    path.write_text(textwrap.dedent(CONFIG), encoding="utf-8")
    settings = settings_from_config(load_config(path))
    engine = make_engine(DbTarget(pg.host, pg.port, pg.user, pg.database, Secret(pg.password)))
    await migrate(engine)
    await prepare(engine, settings)
    await material_tombstone_column(engine)
    async with engine.connect() as conn:
        users = {r[0]: r[1] for r in await conn.execute(text("SELECT handle, id FROM users"))}
    blobs = tmp / "blobs"
    blobs.mkdir()
    return World(engine, settings, blobs, users)


async def make_course(
    w: World, handle: str, title: str, lessons: int = 0, material: str | None = None
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    store = need("reader.lessons.store")
    async with w.engine.begin() as conn:
        course = await store.create_course(conn, w.users[handle], title, material)
        ids = [
            await store.ensure_lesson(conn, w.users[handle], course, n, f"Lesson {n}")
            for n in range(1, lessons + 1)
        ]
    return course, ids


async def execute(w: World, sql: str, **params: Any) -> Any:
    from sqlalchemy import text

    async with w.engine.begin() as conn:
        result = await conn.execute(text(sql), params)
        return result.all() if "RETURNING" in sql else None


async def open_run(w: World, handle: str, lesson: uuid.UUID) -> uuid.UUID:
    rows = await execute(
        w,
        "INSERT INTO compile_runs (owner_id, lesson_id, cap_usd, task_budgets) VALUES (:o, :l, 8.00, '{}'::jsonb)"
        " RETURNING id",
        o=w.users[handle],
        l=lesson,
    )
    run: uuid.UUID = rows[0][0]
    return run


async def close_run(w: World, run: uuid.UUID) -> None:
    await execute(w, "UPDATE compile_runs SET finished_at = now(), cancelled_at = now() WHERE id = :r", r=run)


async def plan_row(w: World, handle: str, plan_course: str, plan_lesson: str, lesson: uuid.UUID) -> None:
    await execute(
        w,
        "INSERT INTO course_plans (owner_id, plan_course_id, plan_lesson_id, lesson_id, material_sha256,"
        " pdf_first, pdf_last) VALUES (:o, :c, :p, :l, :m, 1, 1)",
        o=w.users[handle],
        c=plan_course,
        p=plan_lesson,
        l=lesson,
        m=MATERIAL,
    )


async def positions(w: World, handle: str) -> list[tuple[uuid.UUID, int]]:
    from sqlalchemy import text

    async with w.engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT id, position FROM courses WHERE owner_id = :o AND position IS NOT NULL ORDER BY position"),
            {"o": w.users[handle]},
        )
        return [(r[0], int(r[1])) for r in rows]


async def contiguous(w: World, handle: str) -> bool:
    return [p for _, p in await positions(w, handle)] == list(range(1, len(await positions(w, handle)) + 1))


async def audit_rows(w: World) -> int:
    return int(await scalar(w.engine, "SELECT count(*) FROM audit_log"))


async def snapshot(engine: Any, skip: tuple[str, ...] = ()) -> dict[str, str]:
    """A digest of every table's rows (content, not only counts), outside the volatile tables."""
    from sqlalchemy import text

    async with engine.connect() as conn:
        names = [
            r[0]
            for r in await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
            if not r[0].startswith(VOLATILE) and r[0] not in skip
        ]
        out = {}
        for name in sorted(names):
            out[name] = (
                await conn.execute(
                    text(f'SELECT md5(coalesce(string_agg(t::text, \'|\' ORDER BY t::text), \'\')) FROM "{name}" t')
                )
            ).scalar_one()
        return out


def variant(n: int, notes: tuple[str, ...] = ()) -> bytes:
    """F01 with a changed first cell, and extra `notes` cells with the given ids."""
    doc = json.loads(F01)
    doc["cells"][0]["source"] = [f"variant {n}"]
    template = next(c for c in doc["cells"] if c["id"] == "f01-notes")
    for cell_id in notes:
        extra = json.loads(json.dumps(template))
        extra["id"] = cell_id
        doc["cells"].append(extra)
    return json.dumps(doc).encode()
