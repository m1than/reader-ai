"""SRV-LESSON read routes and access, against Postgres through SRV-AUTH's app factory.

@trace FR-LESSON-7 FR-LESSON-8 FR-LESSON-9 FR-LESSON-11 FR-LESSON-12 FR-LESSON-14 INV-LESSON-4
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from .conftest import F01, Pg, client, course_and_lesson, need, spec_data, world, write
from .manage_support import MANAGE_ROUTES

pytestmark = pytest.mark.anyio
F01_SHA = hashlib.sha256(F01).hexdigest()


@contextlib.contextmanager
def _statements(engine: Any) -> Iterator[list[str]]:
    """Every SQL statement the engine sends while the block runs (query counter)."""
    from sqlalchemy import event

    seen: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_: Any) -> None:
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


def _variant(n: int) -> bytes:
    doc = json.loads(F01)
    doc["cells"][0]["source"] = [f"variant {n}"]
    return json.dumps(doc).encode()


def _same(a: httpx.Response, b: httpx.Response) -> None:
    """FR-LESSON-12: same status, body, Content-Type and header set (values except Date)."""
    assert (a.status_code, a.content, a.headers.get("content-type")) == (
        b.status_code,
        b.content,
        b.headers.get("content-type"),
    )
    strip = lambda r: {k: v for k, v in r.headers.items() if k.lower() != "date"}  # noqa: E731
    assert strip(a) == strip(b)


def test_routes_are_declared_as_data() -> None:
    routes = need("reader.lessons.routes")
    declared = {(r.method, r.path, r.auth) for r in routes.ROUTES}
    assert declared == {
        ("GET", "/api/lessons", "session"),
        ("GET", "/api/lessons/{lesson_id}", "session"),
        ("GET", "/api/lessons/{lesson_id}/revisions/{sha256}", "session"),
    } | {(method, path, "session") for method, path in MANAGE_ROUTES}  # amendment V/manage (FR-LESSON-20..31)


async def test_list_shows_only_own_lessons_in_order(pg: Pg, tmp_path: Path) -> None:
    store = need("reader.lessons.store")
    w = await world(pg, tmp_path)
    async with w.engine.begin() as conn:
        older = await store.create_course(conn, w.users["ivan"], "Older", None)
    async with w.engine.begin() as conn:
        newer = await store.create_course(conn, w.users["ivan"], "Newer", None)
        l2 = await store.ensure_lesson(conn, w.users["ivan"], newer, 2, "Two")
        l1 = await store.ensure_lesson(conn, w.users["ivan"], newer, 1, "One")
        lo = await store.ensure_lesson(conn, w.users["ivan"], older, 1, "Old one")
        anna_course = await store.create_course(conn, w.users["anna"], "Anna", None)
        await store.ensure_lesson(conn, w.users["anna"], anna_course, 1, "Hers")
    sha, _ = await write(w, "ivan", l1)
    async with client(w, "ivan") as c:
        r = await c.get("/api/lessons")
    assert r.status_code == 200
    lessons = r.json()["lessons"]
    assert [x["lesson_id"] for x in lessons] == [str(l1), str(l2), str(lo)]
    first = lessons[0]
    assert set(first) == {
        "lesson_id",
        "course_id",
        "course_title",
        "ordinal",
        "title",
        "updated_at",
        "latest_sha256",
        "skipped",  # amendment V/manage (FR-LESSON-7's additive fields)
        "done",
        "course_position",
        "version",
    }
    assert (first["course_id"], first["course_title"], first["ordinal"], first["latest_sha256"]) == (
        str(newer),
        "Newer",
        1,
        sha,
    )
    assert lessons[1]["latest_sha256"] is None
    await w.engine.dispose()


async def test_detail_lists_newest_revisions_with_stages(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w)
    limit = spec_data()["bounds"]["revisions_listed"]
    shas = [(await write(w, "ivan", lesson, _variant(i)))[0] for i in range(limit + 1)]
    await write(w, "ivan", lesson, _variant(limit), stage="fact_check")
    async with client(w, "ivan") as c:
        r = await c.get(f"/api/lessons/{lesson}")
    assert r.status_code == 200
    body = r.json()
    assert body["lesson"]["lesson_id"] == str(lesson)
    assert body["lesson"]["latest_sha256"] == shas[-1]
    revisions = body["revisions"]
    assert len(revisions) == limit
    assert [x["sha256"] for x in revisions] == list(reversed(shas))[:limit]
    assert revisions[0]["stages"] == ["draft", "fact_check"]
    assert set(revisions[0]) == {"sha256", "byte_size", "cell_count", "created_at", "stages"}
    await w.engine.dispose()


async def test_revision_bytes_and_headers(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w)
    await write(w, "ivan", lesson)
    url = f"/api/lessons/{lesson}/revisions/{F01_SHA}"
    async with client(w, "ivan") as c:
        r = await c.get(url)
        assert r.status_code == 200
        assert r.content == F01
        assert r.headers["content-type"] == "application/x-ipynb+json"
        assert r.headers["etag"] == f'"{F01_SHA}"'
        assert r.headers["cache-control"] == "private, max-age=31536000, immutable"
        assert r.headers["x-content-type-options"] == "nosniff"
        for value in ("*", f'"{F01_SHA}"', f'W/"{F01_SHA}"', f'"x", W/"{F01_SHA}" , "y"'):
            cached = await c.get(url, headers={"If-None-Match": value})
            assert cached.status_code == 304, value
            assert cached.content == b""
            assert cached.headers["etag"] == f'"{F01_SHA}"'
            assert cached.headers["cache-control"] == "private, max-age=31536000, immutable"
            assert cached.headers["x-content-type-options"] == "nosniff"
        assert (await c.get(url, headers={"If-None-Match": '"other"'})).status_code == 200
        unknown = await c.get(f"/api/lessons/{lesson}/revisions/{'0' * 64}")
        assert (unknown.status_code, unknown.json()) == (404, {"error": "unknown_revision"})
    await w.engine.dispose()


async def test_access_check_comes_before_304(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w, "ivan")
    await write(w, "ivan", lesson)
    async with client(w, "anna") as c:
        r = await c.get(f"/api/lessons/{lesson}/revisions/{F01_SHA}", headers={"If-None-Match": "*"})
    assert (r.status_code, r.json()) == (404, {"error": "not_found"})
    await w.engine.dispose()


@pytest.mark.parametrize("damage", ["delete", "corrupt"])
async def test_unavailable_blob(
    pg: Pg, tmp_path: Path, damage: str, caplog: pytest.LogCaptureFixture
) -> None:
    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w)
    await write(w, "ivan", lesson)
    blob = w.blobs / "sha256" / F01_SHA[:2] / F01_SHA
    if damage == "delete":
        blob.unlink()
    else:
        blob.write_bytes(b"{}")
    with caplog.at_level(logging.INFO):
        async with client(w, "ivan") as c:
            r = await c.get(f"/api/lessons/{lesson}/revisions/{F01_SHA}")
    assert (r.status_code, r.json()) == (500, {"error": "blob_unavailable"})
    lines = [rec.getMessage() for rec in caplog.records if F01_SHA in rec.getMessage()]
    assert lines, "the failure is logged with the sha256"
    assert all("{" not in line for line in lines), "no blob content in the log"
    assert any(str(lesson) in line for line in lines)
    await w.engine.dispose()


async def test_not_found_is_uniform(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w, "ivan")
    sha, _ = await write(w, "ivan", lesson)
    async with client(w, "anna") as c:
        absent = str(uuid.uuid4())
        reference = await c.get(f"/api/lessons/{absent}")
        assert (reference.status_code, reference.json()) == (404, {"error": "not_found"})
        for path in (
            f"/api/lessons/{lesson}",
            f"/api/lessons/{str(lesson).upper()}",
            "/api/lessons/not-a-uuid",
            f"/api/lessons/{str(lesson).replace('-', '')}",
        ):
            _same(await c.get(path), reference)
        rev_reference = await c.get(f"/api/lessons/{absent}/revisions/{sha}")
        for path in (
            f"/api/lessons/{lesson}/revisions/{sha}",
            f"/api/lessons/{absent}/revisions/{sha.upper()}",
            f"/api/lessons/{absent}/revisions/xyz",
        ):
            _same(await c.get(path), rev_reference)
    await w.engine.dispose()


async def test_unauthenticated(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    async with client(w, None) as c:
        for path in ("/api/lessons", f"/api/lessons/{uuid.uuid4()}"):
            r = await c.get(path)
            assert (r.status_code, r.json()) == (401, {"error": "unauthenticated"})
    await w.engine.dispose()


async def test_can_open(pg: Pg, tmp_path: Path) -> None:
    access = need("reader.lessons.access")
    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w, "ivan")
    async with w.engine.connect() as conn:
        assert await access.can_open(conn, w.users["ivan"], str(lesson)) is True
        assert await access.can_open(conn, w.users["anna"], str(lesson)) is False
        assert await access.can_open(conn, w.users["ivan"], str(uuid.uuid4())) is False
        with _statements(w.engine) as seen:
            for bad in (str(lesson).upper(), "x", "", f"{lesson} ", f"{lesson}\n", "1; DROP TABLE lessons"):
                assert await access.can_open(conn, w.users["ivan"], bad) is False
        assert seen == [], "malformed ids are refused without a query"
    await w.engine.dispose()


async def test_malformed_path_ids_issue_no_lesson_query(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w, "ivan")
    async with client(w, "ivan") as c:
        with _statements(w.engine) as seen:
            for path in (
                f"/api/lessons/{str(lesson).upper()}",
                "/api/lessons/not-a-uuid",
                f"/api/lessons/{lesson}/revisions/{F01_SHA.upper()}",
                f"/api/lessons/{lesson}/revisions/xyz",
            ):
                assert (await c.get(path)).status_code == 404
    assert [s for s in seen if "lesson" in s] == [], "only the session lookup may query"
    await w.engine.dispose()


async def test_current_revision_follows_the_latest_stage_record(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w)
    a, _ = await write(w, "ivan", lesson)
    b, _ = await write(w, "ivan", lesson, _variant(1))
    async with client(w, "ivan") as c:
        assert (await c.get(f"/api/lessons/{lesson}")).json()["lesson"]["latest_sha256"] == b
        await write(w, "ivan", lesson, stage="revise")
        assert (await c.get("/api/lessons")).json()["lessons"][0]["latest_sha256"] == a
        assert (await c.get(f"/api/lessons/{lesson}")).json()["lesson"]["latest_sha256"] == a
    await w.engine.dispose()


async def test_get_routes_write_nothing(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    _, lesson = await course_and_lesson(w)
    await write(w, "ivan", lesson)
    from .conftest import scalar

    count = (
        "SELECT (SELECT count(*) FROM lessons) + (SELECT count(*) FROM lesson_revisions)"
        " + (SELECT count(*) FROM lesson_revision_stages)"
    )
    before: Any = await scalar(w.engine, count)
    async with client(w, "ivan") as c:
        await c.get("/api/lessons")
        await c.get(f"/api/lessons/{lesson}")
        await c.get(f"/api/lessons/{lesson}/revisions/{F01_SHA}")
    assert await scalar(w.engine, count) == before
    await w.engine.dispose()
