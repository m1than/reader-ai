"""Every management route: access (404 for anyone who does not own or cannot open), Origin, idempotency. [db]

@trace INV-LESSON-20 INV-LESSON-21 FR-LESSON-21
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from .conftest import ORIGIN, Pg, client, need, write
from .manage_support import MANAGE_ROUTES, audit_rows, make_course, manage_world, snapshot

pytestmark = pytest.mark.anyio


def _routes(course: uuid.UUID, lesson: uuid.UUID, note: str, sha: str) -> list[tuple[str, str, Any]]:
    """(method, path, json body) of every route of the amendment that names a course or a lesson."""
    return [
        ("POST", f"/api/courses/{course}", {"version": 1, "title": "Renamed"}),
        ("POST", f"/api/courses/{course}/archive", {}),
        ("POST", f"/api/courses/{course}/unarchive", {}),
        ("GET", f"/api/courses/{course}/impact", None),
        ("DELETE", f"/api/courses/{course}", {"confirm": "Mine"}),
        ("POST", f"/api/courses/{course}/restore", {}),
        ("GET", f"/api/courses/{course}/lessons?state=deleted", None),
        ("POST", f"/api/courses/{course}/lessons/order", {"lesson_ids": [str(lesson)]}),
        ("POST", f"/api/lessons/{lesson}", {"version": 1, "title": "Renamed"}),
        ("POST", f"/api/lessons/{lesson}/skip", {}),
        ("POST", f"/api/lessons/{lesson}/unskip", {}),
        ("DELETE", f"/api/lessons/{lesson}", {}),
        ("POST", f"/api/lessons/{lesson}/restore", {}),
        ("POST", f"/api/lessons/{lesson}/progress", {"done": True}),
        ("POST", f"/api/lessons/{lesson}/revert", {"sha256": sha}),
        ("GET", f"/api/lessons/{lesson}/notes", None),
        ("POST", f"/api/lessons/{lesson}/notes", {"body": "x"}),
        ("POST", f"/api/lessons/{lesson}/notes/{note}", {"body": "y"}),
        ("DELETE", f"/api/lessons/{lesson}/notes/{note}", None),
        ("POST", f"/api/lessons/{lesson}/bookmarks/f01-notes", {}),
        ("DELETE", f"/api/lessons/{lesson}/bookmarks/f01-notes", None),
    ]


async def _send(c: httpx.AsyncClient, method: str, path: str, body: Any, **kw: Any) -> httpx.Response:
    if body is None:
        return await c.request(method, path, **kw)
    return await c.request(method, path, json=body, **kw)


def test_routes_are_declared_with_session_auth() -> None:
    routes = need("reader.lessons.routes")
    declared = {(r.method, r.path): r.auth for r in routes.ROUTES}
    assert MANAGE_ROUTES <= set(declared)
    assert {declared[k] for k in MANAGE_ROUTES} == {"session"}
    # the order route is matched before the course id route
    paths = [(r.method, r.path) for r in routes.ROUTES]
    assert paths.index(("POST", "/api/courses/order")) < paths.index(("POST", "/api/courses/{course_id}"))


async def test_strangers_get_404_and_change_nothing(pg: Pg, tmp_path: Path) -> None:
    w = await manage_world(pg, tmp_path)
    course, (lesson,) = await make_course(w, "ivan", "Mine", lessons=1)
    sha, _ = await write(w, "ivan", lesson)
    async with client(w) as c:
        note = (await c.post(f"/api/lessons/{lesson}/notes", json={"body": "mine"})).json()["note_id"]
    routes = _routes(course, lesson, note, sha)
    for handle in ("anna", "olga"):
        before = await snapshot(w.engine)
        async with client(w, handle) as c:
            for method, path, body in routes:
                r = await _send(c, method, path, body)
                assert (r.status_code, r.json()) == (404, {"error": "not_found"}), (handle, method, path)
            assert (await c.get("/api/courses")).json() == {"courses": []}
            assert (await c.get("/api/bookmarks")).json() == {"bookmarks": []}
            r = await c.post("/api/courses/order", json={"course_ids": [str(course)]})
            assert r.status_code == 409 and r.json()["course_ids"] == []
        assert await snapshot(w.engine) == before, handle
    # a foreign Origin: 403 `origin` on every non-GET route, nothing written
    before = await snapshot(w.engine)
    async with client(w) as c:
        for method, path, body in [*routes, ("POST", "/api/courses/order", {"course_ids": [str(course)]})]:
            if method == "GET":
                continue
            r = await _send(c, method, path, body, headers={"Origin": "https://evil.example"})
            assert (r.status_code, r.json()) == (403, {"error": "origin"}), (method, path)
    assert await snapshot(w.engine) == before
    assert ORIGIN == "https://localhost"
    await w.engine.dispose()


async def test_every_mutation_twice_is_the_same(pg: Pg, tmp_path: Path) -> None:
    w = await manage_world(pg, tmp_path)
    course, (lesson,) = await make_course(w, "ivan", "Mine", lessons=1)
    first_sha, _ = await write(w, "ivan", lesson)
    from .manage_support import variant

    await write(w, "ivan", lesson, variant(1))
    other, _ = await make_course(w, "ivan", "Other")
    async with client(w) as c:
        note = (await c.post(f"/api/lessons/{lesson}/notes", json={"body": "mine"})).json()["note_id"]
        steps: list[tuple[str, str, Any]] = [
            ("POST", f"/api/courses/{course}", {"version": 1, "title": "Renamed"}),
            ("POST", "/api/courses/order", {"course_ids": [str(course), str(other)]}),
            ("POST", f"/api/courses/{course}/archive", {}),
            ("POST", f"/api/courses/{course}/unarchive", {"at": "previous"}),
            ("POST", f"/api/courses/{course}/lessons/order", {"lesson_ids": [str(lesson)]}),
            ("POST", f"/api/lessons/{lesson}", {"version": 1, "title": "Renamed"}),
            ("POST", f"/api/lessons/{lesson}/skip", {}),
            ("POST", f"/api/lessons/{lesson}/unskip", {}),
            ("POST", f"/api/lessons/{lesson}/progress", {"done": True}),
            ("POST", f"/api/lessons/{lesson}/revert", {"sha256": first_sha}),
            ("POST", f"/api/lessons/{lesson}/notes/{note}", {"body": "edited"}),
            ("POST", f"/api/lessons/{lesson}/bookmarks/f01-notes", {}),
            ("DELETE", f"/api/lessons/{lesson}/bookmarks/f01-notes", None),
            ("DELETE", f"/api/lessons/{lesson}/notes/{note}", None),
            ("DELETE", f"/api/lessons/{lesson}", {}),
            ("POST", f"/api/lessons/{lesson}/restore", {}),
            ("DELETE", f"/api/courses/{course}", {"confirm": "Renamed"}),
            ("POST", f"/api/courses/{course}/restore", {"at": "previous"}),
        ]
        for method, path, body in steps:
            first = await _send(c, method, path, body)
            assert first.status_code == 200, (method, path, first.text)
            rows, state = await audit_rows(w), await snapshot(w.engine)
            again = await _send(c, method, path, body)
            if path.endswith("/revert"):  # FR-LESSON-30: the repeat finds the revision current, {applied: false}
                assert (again.status_code, again.json()) == (200, {"applied": False, "latest_sha256": first_sha})
            else:
                assert (again.status_code, again.json()) == (first.status_code, first.json()), (method, path)
            assert await audit_rows(w) == rows, (method, path)
            assert await snapshot(w.engine) == state, (method, path)
    await w.engine.dispose()
