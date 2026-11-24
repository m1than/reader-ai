"""A deleted lesson, or a lesson of a deleted course, takes no reader writes (SRV-LESSON INV-LESSON-20). [db]

@trace INV-LESSON-20 FR-LESSON-29 FR-LESSON-31
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import Pg, client, write
from .manage_support import make_course, manage_world, snapshot

pytestmark = pytest.mark.anyio
WRITES = (
    ("POST", "progress", {"done": True}),
    ("POST", "notes", {"body": "A note"}),
    ("POST", "bookmarks/f01-notes", {}),
)


async def test_progress_note_and_bookmark_on_a_deleted_lesson_are_not_found(pg: Pg, tmp_path: Path) -> None:
    w = await manage_world(pg, tmp_path)
    course, (lesson,) = await make_course(w, "ivan", "Course", lessons=1)
    await write(w, "ivan", lesson)  # the F01 revision has the cell `f01-notes`
    async with client(w) as c:
        for gone, undo in (
            (("DELETE", f"/api/lessons/{lesson}", {}), ("POST", f"/api/lessons/{lesson}/restore", {})),
            (("DELETE", f"/api/courses/{course}", {"confirm": "Course"}), ("POST", f"/api/courses/{course}/restore", {})),
        ):
            assert (await c.request(gone[0], gone[1], json=gone[2])).status_code == 200
            before = await snapshot(w.engine)
            for method, tail, body in WRITES:
                r = await c.request(method, f"/api/lessons/{lesson}/{tail}", json=body)
                assert (r.status_code, r.json()) == (404, {"error": "not_found"}), (gone[1], tail)
            assert await snapshot(w.engine) == before
            assert (await c.request(undo[0], undo[1], json=undo[2])).status_code == 200
        # restored, the same writes apply: the 404s above came from the tombstones
        for method, tail, body in WRITES:
            r = await c.request(method, f"/api/lessons/{lesson}/{tail}", json=body)
            assert r.status_code in (200, 201), (tail, r.text)
    await w.engine.dispose()
