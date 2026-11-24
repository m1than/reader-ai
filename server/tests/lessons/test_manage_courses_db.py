"""Courses: positions, list, edit, order, archive, delete and restore (SRV-LESSON amendment V/manage). [db]

@trace FR-LESSON-20 FR-LESSON-21 FR-LESSON-22 FR-LESSON-23 FR-LESSON-24 FR-LESSON-25 INV-LESSON-21 INV-LESSON-22
"""

from __future__ import annotations

import asyncio
import json
import unicodedata
import uuid
from pathlib import Path
from typing import Any

import pytest

from .conftest import Pg, client, need, scalar, write
from .manage_support import (
    MATERIAL,
    audit_rows,
    close_run,
    contiguous,
    execute,
    make_course,
    manage_world,
    open_run,
    positions,
    snapshot,
)

pytestmark = pytest.mark.anyio


async def test_migration_ranks_existing_courses_and_new_courses_go_first(pg: Pg, tmp_path: Path) -> None:
    from alembic import command
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from reader.db import alembic_config, migrate

    w = await manage_world(pg, tmp_path)

    def down(connection: Any) -> None:
        config = alembic_config()
        config.attributes["connection"] = connection
        command.downgrade(config, "lessons@-1")

    async with w.engine.connect() as conn:
        await conn.run_sync(down)
        await conn.commit()
    ivan = w.users["ivan"]
    async with w.engine.begin() as conn:
        for title, age in (("Old", 3), ("Mid", 2), ("New", 1)):
            await conn.execute(
                text(
                    "INSERT INTO courses (owner_id, title, created_at)"
                    " VALUES (:o, :t, now() - make_interval(days => :a))"
                ),
                {"o": ivan, "t": title, "a": age},
            )
    await migrate(w.engine)
    async with w.engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT title, position, version FROM courses WHERE owner_id = :o ORDER BY position"),
                {"o": ivan},
            )
        ).all()
    assert [(r[0], r[1], r[2]) for r in rows] == [("New", 1, 1), ("Mid", 2, 1), ("Old", 3, 1)]
    created, _ = await make_course(w, "ivan", "Created later")
    assert (await positions(w, "ivan"))[0] == (created, 1)
    assert await contiguous(w, "ivan")
    with pytest.raises(IntegrityError, match="check"):
        async with w.engine.begin() as conn:
            await conn.execute(text("UPDATE courses SET archived_at = now() WHERE id = :c"), {"c": created})
    await w.engine.dispose()


async def test_create_and_order_together_keep_positions_contiguous(pg: Pg, tmp_path: Path) -> None:
    w = await manage_world(pg, tmp_path)
    await make_course(w, "ivan", "Seed A")
    await make_course(w, "ivan", "Seed B")
    async with client(w) as c:
        for i in range(50):
            current = [str(x) for x, _ in await positions(w, "ivan")]
            ordered, _ = await asyncio.gather(
                c.post("/api/courses/order", json={"course_ids": current[::-1]}),
                make_course(w, "ivan", f"Raced {i}"),
            )
            assert ordered.status_code in (200, 409)
            assert await contiguous(w, "ivan"), i
    assert len(await positions(w, "ivan")) == 52
    await w.engine.dispose()


async def test_list_by_state_ordering_and_deleted_lessons(pg: Pg, tmp_path: Path) -> None:
    w = await manage_world(pg, tmp_path)
    a, _ = await make_course(w, "ivan", "A", lessons=1)
    b, b_lessons = await make_course(w, "ivan", "B", lessons=3, material=MATERIAL)
    c_id, _ = await make_course(w, "ivan", "C")
    await make_course(w, "olga", "Olga's course")
    await execute(w, "INSERT INTO materials (owner_id, sha256, config_id, page_count) VALUES (:o, :s, 'book-b', 10)",
                  o=w.users["ivan"], s=MATERIAL)
    async with client(w) as c:
        r = await c.get("/api/courses")
        assert r.status_code == 200, r.text
        listed = r.json()["courses"]
        assert [x["course_id"] for x in listed] == [str(c_id), str(b), str(a)]
        assert set(listed[0]) == {
            "course_id", "title", "description", "position", "state", "archived_at", "deleted_at", "version",
            "lesson_count", "material_id", "plan",
        }
        by_id = {x["course_id"]: x for x in listed}
        assert by_id[str(b)]["material_id"] == "book-b"
        assert by_id[str(b)]["lesson_count"] == 3
        assert by_id[str(a)]["material_id"] is None and by_id[str(a)]["plan"] is None
        assert [x["position"] for x in listed] == [1, 2, 3] and {x["state"] for x in listed} == {"active"}
        assert (await c.post(f"/api/courses/{a}/archive", json={})).status_code == 200
        assert (await c.post(f"/api/courses/{c_id}/archive", json={})).status_code == 200
        archived = (await c.get("/api/courses?state=archived")).json()["courses"]
        assert [x["course_id"] for x in archived] == [str(c_id), str(a)]  # newest archive first
        assert all(x["position"] is None and x["state"] == "archived" for x in archived)
        r = await c.request("DELETE", f"/api/courses/{b}", json={"confirm": "B"})
        assert r.status_code == 200, r.text
        deleted = (await c.get("/api/courses?state=deleted")).json()["courses"]
        assert [x["course_id"] for x in deleted] == [str(b)] and deleted[0]["state"] == "deleted"
        assert (await c.get("/api/courses")).json()["courses"] == []
        assert (await c.get("/api/courses?state=bogus")).status_code == 400
        # the deleted-lessons list of a course that is not deleted
        assert (await c.post(f"/api/courses/{b}/restore", json={})).status_code == 200
        for lesson in b_lessons[:2]:
            assert (await c.request("DELETE", f"/api/lessons/{lesson}", json={})).status_code == 200
        gone = (await c.get(f"/api/courses/{b}/lessons?state=deleted")).json()["lessons"]
        assert [x["lesson_id"] for x in gone] == [str(b_lessons[1]), str(b_lessons[0])]
        assert set(gone[0]) == {"lesson_id", "title", "ordinal", "deleted_at"}
    async with client(w, "olga") as c:
        assert [x["title"] for x in (await c.get("/api/courses")).json()["courses"]] == ["Olga's course"]
        assert (await c.get(f"/api/courses/{b}/lessons?state=deleted")).status_code == 404
    async with client(w, "anna") as c:
        assert (await c.get("/api/courses")).json() == {"courses": []}
    await w.engine.dispose()


async def test_edit_course_fields_refusals_and_noop(pg: Pg, tmp_path: Path) -> None:
    w = await manage_world(pg, tmp_path)
    course, _ = await make_course(w, "ivan", "Algebra")
    await make_course(w, "ivan", "Taken")
    async with client(w) as c:
        r = await c.post(f"/api/courses/{course}", json={"version": 1, "title": "  Linear algebra  "})
        assert r.status_code == 200, r.text
        assert (r.json()["title"], r.json()["version"]) == ("Linear algebra", 2)
        r = await c.post(f"/api/courses/{course}", json={"version": 2, "description": " Notes "})
        assert (r.json()["description"], r.json()["version"]) == ("Notes", 3)
        r = await c.post(f"/api/courses/{course}", json={"version": 3, "description": ""})
        assert (r.json()["description"], r.json()["version"]) == (None, 4)
        before = await audit_rows(w)
        # no-op first, whatever the version says
        noop = await c.post(f"/api/courses/{course}", json={"version": 1, "title": "Linear algebra"})
        assert (noop.status_code, noop.json()["version"]) == (200, 4)
        assert await audit_rows(w) == before
        stale = await c.post(f"/api/courses/{course}", json={"version": 1, "title": "Other"})
        assert stale.status_code == 409
        assert stale.json()["error"] == "stale" and stale.json()["course"]["version"] == 4
        for body, code in (
            ({"version": 4, "title": " "}, "title_invalid"),
            ({"version": 4, "title": "x" * 201}, "title_invalid"),
            ({"version": 4, "title": "bad\x07bell"}, "title_invalid"),
            ({"version": 4, "description": "d" * 2001}, "description_invalid"),
            ({"version": 4}, "invalid_body"),  # neither field
            ({"title": "New"}, "invalid_body"),  # no version
        ):
            r = await c.post(f"/api/courses/{course}", json=body)
            assert (r.status_code, r.json()["error"]) == (400, code), body
        taken = await c.post(f"/api/courses/{course}", json={"version": 4, "title": "Taken"})
        assert (taken.status_code, taken.json()["error"]) == (409, "title_taken")
        nfc = await c.post(f"/api/courses/{course}", json={"version": 4, "title": unicodedata.normalize("NFD", "Ёлка")})
        assert nfc.json()["title"] == unicodedata.normalize("NFC", "Ёлка")
        assert await audit_rows(w) == before + 1
        assert await scalar(w.engine, "SELECT count(*) FROM audit_log WHERE action = 'course.rename'") == 2
        assert await scalar(w.engine, "SELECT count(*) FROM audit_log WHERE action = 'course.describe'") == 2
        # a deleted course is 404 before any validation
        await c.request("DELETE", f"/api/courses/{course}", json={"confirm": "Ёлка"})
        assert (await c.post(f"/api/courses/{course}", json={"version": 5, "title": ""})).status_code == 404
    await w.engine.dispose()


async def test_concurrent_edits_one_wins_and_a_repeat_is_200(pg: Pg, tmp_path: Path) -> None:
    w = await manage_world(pg, tmp_path)
    course, _ = await make_course(w, "ivan", "Race")
    async with client(w) as c:
        r1, r2 = await asyncio.gather(
            c.post(f"/api/courses/{course}", json={"version": 1, "title": "Left"}),
            c.post(f"/api/courses/{course}", json={"version": 1, "title": "Right"}),
        )
        assert sorted([r1.status_code, r2.status_code]) == [200, 409]
        same1, same2 = await asyncio.gather(
            c.post(f"/api/courses/{course}", json={"version": 2, "description": "same"}),
            c.post(f"/api/courses/{course}", json={"version": 2, "description": "same"}),
        )
        assert (same1.status_code, same2.status_code) == (200, 200)
        assert same1.json() == same2.json()
    await w.engine.dispose()


async def test_order_courses(pg: Pg, tmp_path: Path) -> None:
    w = await manage_world(pg, tmp_path)
    a, _ = await make_course(w, "ivan", "A")
    b, _ = await make_course(w, "ivan", "B")
    cc, _ = await make_course(w, "ivan", "C")
    async with client(w) as c:
        r = await c.post("/api/courses/order", json={"course_ids": [str(a), str(b), str(cc)]})
        assert (r.status_code, r.json()) == (200, {"course_ids": [str(a), str(b), str(cc)]})
        assert [x for x, _ in await positions(w, "ivan")] == [a, b, cc]
        before = await audit_rows(w)
        assert (await c.post("/api/courses/order", json={"course_ids": [str(a), str(b), str(cc)]})).status_code == 200
        assert await audit_rows(w) == before
        for ids in ([str(a), str(b)], [str(a), str(b), str(cc), str(uuid.uuid4())], [str(a), str(a), str(b)]):
            r = await c.post("/api/courses/order", json={"course_ids": ids})
            assert r.status_code == 409
            assert r.json() == {"error": "order_mismatch", "course_ids": [str(a), str(b), str(cc)]}
        assert (await c.post("/api/courses/order", json={"course_ids": "x"})).status_code == 400
    await w.engine.dispose()


async def test_archive_and_unarchive(pg: Pg, tmp_path: Path) -> None:
    w = await manage_world(pg, tmp_path)
    a, a_lessons = await make_course(w, "ivan", "A", lessons=2)
    b, _ = await make_course(w, "ivan", "B")
    cc, _ = await make_course(w, "ivan", "C")
    assert [x for x, _ in await positions(w, "ivan")] == [cc, b, a]
    async with client(w) as c:
        run = await open_run(w, "ivan", a_lessons[1])
        r = await c.post(f"/api/courses/{a}/archive", json={})
        assert (r.status_code, r.json()["error"]) == (409, "compile_running")
        await close_run(w, run)
        # archive B (position 2), then Undo puts it back at 2
        first = await c.post(f"/api/courses/{b}/archive", json={})
        assert first.status_code == 200 and first.json()["state"] == "archived"
        assert (await c.post(f"/api/courses/{b}/archive", json={})).json() == first.json()
        assert [x for x, _ in await positions(w, "ivan")] == [cc, a]
        back = await c.post(f"/api/courses/{b}/unarchive", json={"at": "previous"})
        assert back.status_code == 200 and back.json()["position"] == 2
        assert [x for x, _ in await positions(w, "ivan")] == [cc, b, a]
        assert (await c.post(f"/api/courses/{b}/unarchive", json={"at": "previous"})).json() == back.json()
        # archive A, unarchive "first"
        await c.post(f"/api/courses/{a}/archive", json={})
        assert (await c.post(f"/api/courses/{a}/unarchive", json={})).json()["position"] == 1
        assert [x for x, _ in await positions(w, "ivan")] == [a, cc, b]
        assert (await c.post(f"/api/courses/{a}/unarchive", json={"at": "middle"})).status_code == 400
        # an archived course's lessons still open by link
        await c.post(f"/api/courses/{a}/archive", json={})
        assert (await c.get(f"/api/lessons/{a_lessons[0]}")).status_code == 200
        assert str(a_lessons[0]) not in {x["lesson_id"] for x in (await c.get("/api/lessons")).json()["lessons"]}
        listed = (await c.get("/api/lessons?archived=1")).json()["lessons"]
        assert str(a_lessons[0]) in {x["lesson_id"] for x in listed}
        # unarchive of a deleted course: 404, nothing written
        await c.request("DELETE", f"/api/courses/{a}", json={"confirm": "A"})
        before = await snapshot(w.engine)
        assert (await c.post(f"/api/courses/{a}/unarchive", json={})).status_code == 404
        assert await snapshot(w.engine) == before
    assert await scalar(w.engine, "SELECT count(*) FROM audit_log WHERE action = 'course.archive'") == 3
    await w.engine.dispose()


async def test_impact_delete_and_restore(pg: Pg, tmp_path: Path) -> None:
    w = await manage_world(pg, tmp_path)
    access = need("reader.lessons.access")
    title = "Линейная алгебра и геометрия: полный курс"  # 41 characters
    assert len(title) == 41
    course, lessons = await make_course(w, "ivan", title, lessons=2)
    other, _ = await make_course(w, "ivan", "Other")
    sha, _ = await write(w, "ivan", lessons[0])
    run = await open_run(w, "ivan", lessons[0])
    await execute(
        w,
        "INSERT INTO agent_spend (owner_id, run_id, lesson_id, stage, task, attempt, kind, model, usd)"
        " VALUES (:o, :r, :l, 'draft', 'draft', 1, 'reserve', 'm', 1)",
        o=w.users["ivan"], r=run, l=lessons[0],
    )
    async with client(w) as c:
        impact = await c.get(f"/api/courses/{course}/impact")
        assert impact.json() == {
            "lessons": 2, "revisions": 1, "tutor_questions": 0, "compile_runs": 1, "spend_rows": 1, "open_runs": 1,
        }
        confirm = unicodedata.normalize("NFD", title[:40]) + "  "
        r = await c.request("DELETE", f"/api/courses/{course}", json={"confirm": confirm})
        assert (r.status_code, r.json()["error"]) == (409, "compile_running")
        await close_run(w, run)
        r = await c.request("DELETE", f"/api/courses/{course}", json={"confirm": title[:39]})
        assert (r.status_code, r.json()["error"]) == (400, "confirm_mismatch")
        before = await snapshot(w.engine, skip=("courses", "audit_log"))
        deleted = await c.request("DELETE", f"/api/courses/{course}", json={"confirm": confirm})
        assert deleted.status_code == 200, deleted.text
        body = deleted.json()
        assert body["open_runs"] == 0 and body["lessons"] == 2 and body["deleted_at"]
        assert (await c.request("DELETE", f"/api/courses/{course}", json={"confirm": confirm})).json() == body
        assert [x for x, _ in await positions(w, "ivan")] == [other]
        # every read of its lessons is 404
        assert (await c.get(f"/api/lessons/{lessons[0]}")).status_code == 404
        assert (await c.get(f"/api/lessons/{lessons[0]}/revisions/{sha}")).status_code == 404
        assert str(lessons[0]) not in {x["lesson_id"] for x in (await c.get("/api/lessons")).json()["lessons"]}
        async with w.engine.connect() as conn:
            assert not await access.can_open(conn, w.users["ivan"], str(lessons[0]))
        assert await snapshot(w.engine, skip=("courses", "audit_log")) == before
        # restore "previous" returns it to its old position (second, under "Other")
        restored = await c.post(f"/api/courses/{course}/restore", json={"at": "previous"})
        assert restored.status_code == 200 and restored.json()["position"] == 2
        assert [x for x, _ in await positions(w, "ivan")] == [other, course]
        assert (await c.post(f"/api/courses/{course}/restore", json={"at": "previous"})).json() == restored.json()
        assert (await c.get(f"/api/lessons/{lessons[0]}")).status_code == 200
        assert await snapshot(w.engine, skip=("courses", "audit_log")) == before
        # archive → delete → restore: archived with no position, then unarchive places it
        assert (await c.post(f"/api/courses/{course}/archive", json={})).status_code == 200
        whole = await c.request("DELETE", f"/api/courses/{course}", json={"confirm": title})  # 41: not the first 40
        assert (whole.status_code, whole.json()["error"]) == (400, "confirm_mismatch")
        assert (await c.request("DELETE", f"/api/courses/{course}", json={"confirm": title[:40]})).status_code == 200
        back = await c.post(f"/api/courses/{course}/restore", json={})
        assert back.status_code == 200
        assert (back.json()["state"], back.json()["position"]) == ("archived", None)
        assert (await c.post(f"/api/courses/{course}/unarchive", json={})).json()["position"] == 1
    assert await scalar(w.engine, "SELECT count(*) FROM audit_log WHERE action = 'course.delete'") == 2
    detail = await scalar(
        w.engine, "SELECT detail::text FROM audit_log WHERE action = 'course.delete' ORDER BY id LIMIT 1"
    )
    assert json.loads(detail) == {
        "lessons": 2, "revisions": 1, "tutor_questions": 0, "compile_runs": 1, "spend_rows": 1, "by_material": False,
    }
    await w.engine.dispose()


async def test_restore_refusals(pg: Pg, tmp_path: Path) -> None:
    w = await manage_world(pg, tmp_path)
    course, _ = await make_course(w, "ivan", "Twin")
    async with client(w) as c:
        await c.request("DELETE", f"/api/courses/{course}", json={"confirm": "Twin"})
        await make_course(w, "ivan", "Twin")  # the title is free while the course is deleted
        r = await c.post(f"/api/courses/{course}/restore", json={})
        assert (r.status_code, r.json()["error"]) == (409, "title_taken")
        assert (await c.post(f"/api/courses/{uuid.uuid4()}/restore", json={})).status_code == 404
    await w.engine.dispose()


# Merge point: `manage.material_is_live` gains `AND deleted_at IS NULL` when SRV-INGEST's `materials.deleted_at`
# (FR-INGEST-31, a parallel slice) is merged; strict, so the merge that switches it on must drop this marker.
@pytest.mark.xfail(strict=True, reason="needs SRV-INGEST materials.deleted_at in material_is_live (merge point)")
async def test_restore_refused_while_the_material_is_deleted(pg: Pg, tmp_path: Path) -> None:
    w = await manage_world(pg, tmp_path)
    sourced, _ = await make_course(w, "ivan", "Sourced", material=MATERIAL)
    await execute(
        w,
        "INSERT INTO materials (owner_id, sha256, config_id, page_count, deleted_at) VALUES (:o, :s, 'm', 1, now())",
        o=w.users["ivan"], s=MATERIAL,
    )
    async with client(w) as c:
        r = await c.post(f"/api/courses/{sourced}/restore", json={})
        assert (r.status_code, r.json()["error"]) == (409, "material_deleted")
        await execute(w, "UPDATE materials SET deleted_at = NULL WHERE sha256 = :s", s=MATERIAL)
        assert (await c.post(f"/api/courses/{sourced}/restore", json={})).status_code == 200
    await w.engine.dispose()


async def test_tombstones_only(pg: Pg, tmp_path: Path) -> None:
    w = await manage_world(pg, tmp_path)
    course, lessons = await make_course(w, "ivan", "Kept", lessons=1)
    await write(w, "ivan", lessons[0])
    counts = (
        "SELECT (SELECT count(*) FROM lessons), (SELECT count(*) FROM lesson_revisions),"
        " (SELECT count(*) FROM lesson_revision_stages), (SELECT count(*) FROM agent_spend)"
    )
    from sqlalchemy import text

    async def all_counts() -> tuple[Any, ...]:
        async with w.engine.connect() as conn:
            return tuple((await conn.execute(text(counts))).one())

    before = await all_counts()
    blobs_before = sorted(p.name for p in w.blobs.rglob("*") if p.is_file())
    async with client(w) as c:
        await c.request("DELETE", f"/api/lessons/{lessons[0]}", json={})
        await c.post(f"/api/lessons/{lessons[0]}/restore", json={})
        await c.request("DELETE", f"/api/courses/{course}", json={"confirm": "Kept"})
        await c.post(f"/api/courses/{course}/restore", json={})
    assert await all_counts() == before
    assert sorted(p.name for p in w.blobs.rglob("*") if p.is_file()) == blobs_before
    triggers = await scalar(
        w.engine,
        "SELECT count(*) FROM pg_trigger WHERE tgname IN ('lesson_revisions_immutable',"
        " 'lesson_revision_stages_immutable', 'lesson_revisions_no_truncate', 'lesson_revision_stages_no_truncate')",
    )
    assert triggers == 4
    await w.engine.dispose()
