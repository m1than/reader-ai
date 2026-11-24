"""Lessons: rename, order, skip, delete, done, revert, notes and bookmarks (SRV-LESSON amendment V/manage). [db]

@trace FR-LESSON-26 FR-LESSON-27 FR-LESSON-28 FR-LESSON-29 FR-LESSON-30 FR-LESSON-31
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from .conftest import F01, Pg, client, need, scalar, write
from .manage_support import audit_rows, close_run, execute, make_course, manage_world, open_run, variant

pytestmark = pytest.mark.anyio
F01_SHA = hashlib.sha256(F01).hexdigest()


async def test_rename_lesson(pg: Pg, tmp_path: Path) -> None:
    w = await manage_world(pg, tmp_path)
    _, (lesson,) = await make_course(w, "ivan", "Course", lessons=1)
    async with client(w) as c:
        r = await c.post(f"/api/lessons/{lesson}", json={"version": 1, "title": " Proofs "})
        assert r.status_code == 200, r.text
        assert (r.json()["title"], r.json()["version"], r.json()["lesson_id"]) == ("Proofs", 2, str(lesson))
        assert {"course_id", "ordinal", "latest_sha256", "skipped", "done", "course_position"} <= set(r.json())
        assert (await c.post(f"/api/lessons/{lesson}", json={"version": 1, "title": "Proofs"})).json() == r.json()
        stale = await c.post(f"/api/lessons/{lesson}", json={"version": 1, "title": "Other"})
        assert (stale.status_code, stale.json()["error"]) == (409, "stale")
        bad = await c.post(f"/api/lessons/{lesson}", json={"version": 2, "title": ""})
        assert (bad.status_code, bad.json()["error"]) == (400, "title_invalid")
        run = await open_run(w, "ivan", lesson)
        busy = await c.post(f"/api/lessons/{lesson}", json={"version": 2, "title": "Other"})
        assert (busy.status_code, busy.json()["error"]) == (409, "compile_running")
        await close_run(w, run)
        assert (await c.post(f"/api/lessons/{lesson}", json={"version": 2, "title": "Other"})).status_code == 200
    assert await scalar(w.engine, "SELECT count(*) FROM audit_log WHERE action = 'lesson.rename'") == 2
    await w.engine.dispose()


async def test_order_lessons_keeps_deleted_after_visible(pg: Pg, tmp_path: Path) -> None:
    w = await manage_world(pg, tmp_path)
    course, (a, b, cc, d) = await make_course(w, "ivan", "Course", lessons=4)
    async with client(w) as c:
        assert (await c.request("DELETE", f"/api/lessons/{b}", json={})).status_code == 200
        r = await c.post(f"/api/courses/{course}/lessons/order", json={"lesson_ids": [str(d), str(cc), str(a)]})
        assert (r.status_code, r.json()) == (200, {"lesson_ids": [str(d), str(cc), str(a)]})
        ordinals = {
            x: await scalar(w.engine, "SELECT ordinal FROM lessons WHERE id = :l", l=x) for x in (a, b, cc, d)
        }
        assert sorted(ordinals, key=ordinals.__getitem__) == [d, cc, a, b]
        assert await scalar(w.engine, "SELECT order_source FROM courses WHERE id = :c", c=course) == "manual"
        before = await audit_rows(w)
        again = await c.post(f"/api/courses/{course}/lessons/order", json={"lesson_ids": [str(d), str(cc), str(a)]})
        assert again.json() == r.json() and await audit_rows(w) == before
        for ids in ([str(d), str(cc)], [str(d), str(cc), str(a), str(b)]):
            mismatch = await c.post(f"/api/courses/{course}/lessons/order", json={"lesson_ids": ids})
            assert mismatch.status_code == 409
            assert mismatch.json() == {"error": "order_mismatch", "lesson_ids": [str(d), str(cc), str(a)]}
    other, _ = await make_course(w, "ivan", "Untouched", lessons=2)
    async with client(w) as c:
        ids = [x["lesson_id"] for x in (await c.get("/api/lessons")).json()["lessons"] if x["course_id"] == str(other)]
        await c.post(f"/api/courses/{other}/lessons/order", json={"lesson_ids": ids})  # the current order: no-op
    assert await scalar(w.engine, "SELECT order_source FROM courses WHERE id = :c", c=other) == "plan"
    await w.engine.dispose()


async def test_skip_delete_restore(pg: Pg, tmp_path: Path) -> None:
    w = await manage_world(pg, tmp_path)
    course, (lesson, other) = await make_course(w, "ivan", "Course", lessons=2)
    async with client(w) as c:
        run = await open_run(w, "ivan", lesson)
        for method, path in (("POST", "skip"), ("DELETE", "")):
            url = f"/api/lessons/{lesson}" + (f"/{path}" if path else "")
            r = await c.request(method, url, json={})
            assert (r.status_code, r.json()["error"]) == (409, "compile_running")
        await close_run(w, run)
        for path in ("skip", "unskip"):
            first = await c.post(f"/api/lessons/{lesson}/{path}", json={})
            assert first.status_code == 200
            assert (await c.post(f"/api/lessons/{lesson}/{path}", json={})).json() == first.json()
        skipped = await c.post(f"/api/lessons/{lesson}/skip", json={})
        assert skipped.json()["skipped_at"] is not None
        listed = {x["lesson_id"]: x for x in (await c.get("/api/lessons")).json()["lessons"]}
        assert listed[str(lesson)]["skipped"] is True and listed[str(other)]["skipped"] is False
        deleted = await c.request("DELETE", f"/api/lessons/{lesson}", json={})
        assert deleted.status_code == 200 and deleted.json()["deleted_at"] is not None
        assert (await c.request("DELETE", f"/api/lessons/{lesson}", json={})).json() == deleted.json()
        assert (await c.get(f"/api/lessons/{lesson}")).status_code == 404
        assert (await c.post(f"/api/lessons/{lesson}/skip", json={})).status_code == 404
        # its ordinal is kept (a gap); restore while the course is deleted is 404
        assert await scalar(w.engine, "SELECT ordinal FROM lessons WHERE id = :l", l=lesson) == 1
        await c.request("DELETE", f"/api/courses/{course}", json={"confirm": "Course"})
        assert (await c.post(f"/api/lessons/{lesson}/restore", json={})).status_code == 404
        await c.post(f"/api/courses/{course}/restore", json={})
        restored = await c.post(f"/api/lessons/{lesson}/restore", json={})
        assert restored.status_code == 200 and restored.json()["deleted_at"] is None
        assert (await c.post(f"/api/lessons/{lesson}/restore", json={})).json() == restored.json()
        assert (await c.get(f"/api/lessons/{lesson}")).status_code == 200
    for action, n in (("lesson.skip", 2), ("lesson.unskip", 1), ("lesson.delete", 1), ("lesson.restore", 1)):
        assert await scalar(w.engine, "SELECT count(*) FROM audit_log WHERE action = :a", a=action) == n, action
    await w.engine.dispose()


async def test_done(pg: Pg, tmp_path: Path) -> None:
    w = await manage_world(pg, tmp_path)
    _, (lesson,) = await make_course(w, "ivan", "Course", lessons=1)
    async with client(w) as c:
        first = await c.post(f"/api/lessons/{lesson}/progress", json={"done": True})
        assert first.status_code == 200 and first.json()["done"] is True and first.json()["done_at"]
        assert (await c.post(f"/api/lessons/{lesson}/progress", json={"done": True})).json() == first.json()
        listed = (await c.get("/api/lessons")).json()["lessons"]
        assert listed[0]["done"] is True
        off = await c.post(f"/api/lessons/{lesson}/progress", json={"done": False})
        assert off.json() == {"done": False, "done_at": None}
        assert (await c.post(f"/api/lessons/{lesson}/progress", json={"done": False})).json() == off.json()
        assert (await c.post(f"/api/lessons/{lesson}/progress", json={"done": "yes"})).status_code == 400
    assert await scalar(w.engine, "SELECT count(*) FROM lesson_progress") == 0
    await w.engine.dispose()


async def test_revert(pg: Pg, tmp_path: Path) -> None:
    w = await manage_world(pg, tmp_path)
    _, (lesson,) = await make_course(w, "ivan", "Course", lessons=1)
    first, _ = await write(w, "ivan", lesson)
    second, _ = await write(w, "ivan", lesson, variant(1))
    async with client(w) as c:
        r = await c.post(f"/api/lessons/{lesson}/revert", json={"sha256": first})
        assert (r.status_code, r.json()) == (200, {"applied": True, "latest_sha256": first})
        noop = await c.post(f"/api/lessons/{lesson}/revert", json={"sha256": first})
        assert (noop.status_code, noop.json()["applied"]) == (200, False)
        from sqlalchemy import text

        async with w.engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT stage, sha256, provenance::text FROM lesson_revision_stages WHERE lesson_id = :l"
                         " ORDER BY id"),
                    {"l": lesson},
                )
            ).all()
        assert [(x[0], x[1]) for x in rows] == [("draft", first), ("draft", second), ("revert", first)]
        assert json.loads(rows[2][2]) == {"input_sha256": second}
        back = await c.post(f"/api/lessons/{lesson}/revert", json={"sha256": second})
        assert back.json() == {"applied": True, "latest_sha256": second}
        unknown = await c.post(f"/api/lessons/{lesson}/revert", json={"sha256": "0" * 64})
        assert (unknown.status_code, unknown.json()["error"]) == (404, "unknown_revision")
        run = await open_run(w, "ivan", lesson)
        busy = await c.post(f"/api/lessons/{lesson}/revert", json={"sha256": first})
        assert (busy.status_code, busy.json()["error"]) == (409, "compile_running")
        await close_run(w, run)
        # a revert that would drop a notes cell id of the current revision
        third, _ = await write(w, "ivan", lesson, variant(2, notes=("my-notes",)))
        dropped = await c.post(f"/api/lessons/{lesson}/revert", json={"sha256": first})
        assert (dropped.status_code, dropped.json()["error"]) == (400, "notes_cell_removed")
        assert await scalar(
            w.engine, "SELECT sha256 FROM lesson_revision_stages WHERE lesson_id = :l ORDER BY id DESC LIMIT 1", l=lesson
        ) == third
    assert await scalar(w.engine, "SELECT count(*) FROM audit_log WHERE action = 'lesson.revert'") == 2
    await w.engine.dispose()


async def test_notes_and_bookmarks(pg: Pg, tmp_path: Path) -> None:
    w = await manage_world(pg, tmp_path)
    _, (lesson, bare) = await make_course(w, "ivan", "Course", lessons=2)
    await write(w, "ivan", lesson)
    async with client(w) as c:
        n1 = await c.post(f"/api/lessons/{lesson}/notes", json={"body": "  first  ", "cell_id": "f01-notes"})
        assert n1.status_code == 201, n1.text
        note = n1.json()
        assert (note["body"], note["cell_id"], note["lesson_id"]) == ("first", "f01-notes", str(lesson))
        n2 = await c.post(f"/api/lessons/{lesson}/notes", json={"body": "second"})
        assert n2.status_code == 201 and n2.json()["note_id"] != note["note_id"]
        listed = (await c.get(f"/api/lessons/{lesson}/notes")).json()["notes"]
        assert [x["body"] for x in listed] == ["second", "first"]
        edited = await c.post(f"/api/lessons/{lesson}/notes/{note['note_id']}", json={"body": "changed"})
        assert edited.status_code == 200 and edited.json()["body"] == "changed"
        for body, code in (({"body": " "}, "note_invalid"), ({"body": "x" * 10001}, "note_invalid"),
                           ({"body": "x", "cell_id": "nope"}, "cell_unknown")):
            r = await c.post(f"/api/lessons/{lesson}/notes", json=body)
            assert (r.status_code, r.json()["error"]) == (400, code), body
        r = await c.post(f"/api/lessons/{bare}/notes", json={"body": "x", "cell_id": "f01-notes"})
        assert (r.status_code, r.json()["error"]) == (400, "cell_unknown")  # no revision: no cells
        assert (await c.post(f"/api/lessons/{lesson}/notes/{uuid.uuid4()}", json={"body": "x"})).status_code == 404
        gone = await c.request("DELETE", f"/api/lessons/{lesson}/notes/{note['note_id']}")
        assert gone.status_code == 200
        assert (await c.request("DELETE", f"/api/lessons/{lesson}/notes/{note['note_id']}")).json() == gone.json()
        assert [x["body"] for x in (await c.get(f"/api/lessons/{lesson}/notes")).json()["notes"]] == ["second"]
        # the notes cap: 500 per (user, lesson)
        await execute(
            w,
            "INSERT INTO lesson_notes (owner_id, user_id, lesson_id, body)"
            " SELECT :o, :o, :l, 'n' FROM generate_series(1, 499)",
            o=w.users["ivan"], l=lesson,
        )
        capped = await c.post(f"/api/lessons/{lesson}/notes", json={"body": "one too many"})
        assert (capped.status_code, capped.json()["error"]) == (409, "limit")
        # bookmarks
        b1 = await c.post(f"/api/lessons/{lesson}/bookmarks/f01-notes", json={})
        assert b1.status_code == 200
        assert (await c.post(f"/api/lessons/{lesson}/bookmarks/f01-notes", json={})).json() == b1.json()
        r = await c.post(f"/api/lessons/{lesson}/bookmarks/nope", json={})
        assert (r.status_code, r.json()["error"]) == (400, "cell_unknown")
        marks = (await c.get("/api/bookmarks")).json()["bookmarks"]
        assert [(x["lesson_id"], x["cell_id"], x["stale"]) for x in marks] == [(str(lesson), "f01-notes", False)]
        # the cell leaves the current revision: the bookmark is stale
        doc = json.loads(F01)
        doc["cells"] = [x for x in doc["cells"] if x["id"] != "f01-explain"]
        await c.post(f"/api/lessons/{lesson}/bookmarks/f01-explain", json={})
        store = need("reader.lessons.store")
        async with w.engine.begin() as conn:
            await store.write_revision(
                conn, w.blobs, w.users["ivan"], lesson, json.dumps(doc).encode(), "draft", {}
            )
        marks = {x["cell_id"]: x["stale"] for x in (await c.get("/api/bookmarks")).json()["bookmarks"]}
        assert marks == {"f01-notes": False, "f01-explain": True}
        removed = await c.request("DELETE", f"/api/lessons/{lesson}/bookmarks/f01-explain")
        assert removed.status_code == 200
        assert (await c.request("DELETE", f"/api/lessons/{lesson}/bookmarks/f01-explain")).json() == removed.json()
        # the bookmark cap: 200 per (user, lesson)
        await execute(
            w,
            "INSERT INTO lesson_bookmarks (owner_id, user_id, lesson_id, cell_id)"
            " SELECT :o, :o, :l, 'c' || g FROM generate_series(1, 199) g",
            o=w.users["ivan"], l=lesson,
        )
        capped = await c.post(f"/api/lessons/{lesson}/bookmarks/f01-lab", json={})
        assert (capped.status_code, capped.json()["error"]) == (409, "limit")
        # a deleted lesson's bookmarks leave the list
        await c.request("DELETE", f"/api/lessons/{lesson}", json={})
        assert (await c.get("/api/bookmarks")).json() == {"bookmarks": []}
    await w.engine.dispose()
