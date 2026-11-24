"""SRV-TUTOR manage amendment [db]: clearing history (FR-TUTOR-20) and deleted lessons (FR-TUTOR-21).

@trace FR-TUTOR-20 FR-TUTOR-21
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from .conftest import (
    FakeJobs,
    FakeProvider,
    Pg,
    client,
    deps,
    execute,
    insert_question,
    question_row,
    rows,
    run_handler,
    scalar,
    world,
)

pytestmark = pytest.mark.anyio


async def _answered(w: Any, handle: str, text: str) -> uuid.UUID:
    qid = await insert_question(w, handle, question=text)
    await execute(
        w.engine,
        "UPDATE tutor_questions SET state = 'answered', answer = '{\"blocks\": []}', grounded = true,"
        " model = 'claude-sonnet-5-5', finished_at = now() WHERE id = :i",
        i=qid,
    )
    await execute(
        w.engine,
        "INSERT INTO agent_spend (owner_id, run_id, stage, task, attempt, kind, model, usd)"
        " VALUES (:o, :r, 'tutor', 'tutor', 1, 'reserve', 'claude-sonnet-5-5', 0.05)",
        o=w.users[handle], r=qid,
    )
    return qid


async def _snapshot(w: Any) -> Any:
    return await rows(
        w.engine,
        "SELECT id, owner_id, question, answer, state, model, error_code, created_at, finished_at, cleared_at"
        " FROM tutor_questions ORDER BY id",
    )


async def _spend(w: Any) -> Any:
    return await rows(w.engine, "SELECT id, run_id, usd FROM agent_spend ORDER BY id")


async def test_clear_history_removes_the_text_and_keeps_the_rest(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    lesson = w.lessons["ivan"]
    first = await _answered(w, "ivan", "First secret-ish question?")
    second = await _answered(w, "ivan", "Second question?")
    theirs = await _answered(w, "anna", "Anna's question?")
    spend = await _spend(w)
    before = {r[0]: r for r in await _snapshot(w)}
    async with client(w, "ivan", jobs=FakeJobs()) as c:
        r = await c.post(f"/api/lessons/{lesson}/questions/clear", json={})
        assert (r.status_code, r.json()) == (200, {"cleared": 2})
        again = await c.post(f"/api/lessons/{lesson}/questions/clear", json={})
        assert (again.status_code, again.json()) == (200, {"cleared": 0})
        listed = (await c.get(f"/api/lessons/{lesson}/questions")).json()["questions"]
        assert listed == []
        for qid in (first, second):
            got = await c.get(f"/api/lessons/{lesson}/questions/{qid}")
            assert (got.status_code, got.json()) == (404, {"error": "not_found"})
    after = {r[0]: r for r in await _snapshot(w)}
    for qid in (first, second):
        b, a = before[qid], after[qid]
        assert (a[2], a[3], a[9] is not None) == ("", None, True)  # question, answer, cleared_at
        assert a[4:9] == b[4:9]  # state, model, error_code, created_at, finished_at stay
    assert after[theirs] == before[theirs]
    assert await _spend(w) == spend
    assert await rows(w.engine, "SELECT action, object_id, detail FROM audit_log") == [
        ("tutor.clear", str(lesson), {"count": 2}),
    ]
    await w.engine.dispose()


async def test_clear_one_question(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    lesson = w.lessons["ivan"]
    one = await _answered(w, "ivan", "One?")
    other = await _answered(w, "ivan", "Other?")
    theirs = await _answered(w, "anna", "Anna's?")
    async with client(w, "ivan", jobs=FakeJobs()) as c:
        r = await c.request("DELETE", f"/api/lessons/{lesson}/questions/{one}", json={})
        assert (r.status_code, r.json()) == (200, {"cleared": 1})
        for target in (one, theirs, uuid.uuid4(), "not-a-uuid"):  # cleared, another user's, unknown, malformed
            r = await c.request("DELETE", f"/api/lessons/{lesson}/questions/{target}", json={})
            assert (r.status_code, r.json()) == (404, {"error": "not_found"}), target
        listed = (await c.get(f"/api/lessons/{lesson}/questions")).json()["questions"]
        assert [q["question_id"] for q in listed] == [str(other)]
    assert await scalar(w.engine, "SELECT question FROM tutor_questions WHERE id = :i", i=theirs) == "Anna's?"
    await w.engine.dispose()


async def test_a_queued_question_makes_clear_busy(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    lesson = w.lessons["ivan"]
    await _answered(w, "ivan", "Done?")
    queued = await insert_question(w, "ivan", question="Waiting?")
    before = await _snapshot(w)
    async with client(w, "ivan", jobs=FakeJobs(statuses={})) as c:
        for method, path in [
            ("POST", f"/api/lessons/{lesson}/questions/clear"),
            ("DELETE", f"/api/lessons/{lesson}/questions/{queued}"),
        ]:
            r = await c.request(method, path, json={})
            assert (r.status_code, r.json()) == (409, {"error": "tutor_busy"}), path
    assert await _snapshot(w) == before
    assert await scalar(w.engine, "SELECT count(*) FROM audit_log") == 0
    await w.engine.dispose()


async def test_other_readers_get_404_and_change_nothing(pg: Pg, tmp_path: Path) -> None:
    w = await world(pg, tmp_path)
    lesson = w.lessons["ivan"]
    qid = await _answered(w, "ivan", "Mine?")
    before = await _snapshot(w)
    async with client(w, "anna", jobs=FakeJobs()) as c:
        r = await c.post(f"/api/lessons/{lesson}/questions/clear", json={})
        assert (r.status_code, r.json()) == (404, {"error": "not_found"})
        r = await c.request("DELETE", f"/api/lessons/{lesson}/questions/{qid}", json={})
        assert (r.status_code, r.json()) == (404, {"error": "not_found"})
    async with client(w, None, jobs=FakeJobs()) as c:
        assert (await c.post(f"/api/lessons/{lesson}/questions/clear", json={})).status_code == 401
    assert await _snapshot(w) == before
    await w.engine.dispose()


async def test_the_question_check_allows_empty_text_only_when_cleared(pg: Pg, tmp_path: Path) -> None:
    from sqlalchemy.exc import IntegrityError

    w = await world(pg, tmp_path)
    qid = await _answered(w, "ivan", "Text?")
    with pytest.raises(IntegrityError):
        await execute(w.engine, "UPDATE tutor_questions SET question = '' WHERE id = :i", i=qid)
    with pytest.raises(IntegrityError):
        await execute(w.engine, "UPDATE tutor_questions SET cleared_at = now() WHERE id = :i", i=qid)
    await execute(w.engine, "UPDATE tutor_questions SET question = '', cleared_at = now() WHERE id = :i", i=qid)
    await w.engine.dispose()


# FR-TUTOR-21 — deleted lessons answer 404 through can_open; a queued question of one expires ----------------


async def test_every_route_answers_404_when_the_lesson_cannot_be_opened(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import reader.tutor.routes as routes

    w = await world(pg, tmp_path)
    lesson = w.lessons["ivan"]
    qid = await _answered(w, "ivan", "Before the delete?")

    async def deleted(conn: Any, user_id: Any, lesson_id: str) -> bool:  # SRV-LESSON's tombstone read
        return False

    monkeypatch.setattr(routes, "can_open", deleted)
    before = await _snapshot(w)
    async with client(w, "ivan", jobs=FakeJobs()) as c:
        for method, path, body in [
            ("POST", f"/api/lessons/{lesson}/ask", {"question": "Now?"}),
            ("GET", f"/api/lessons/{lesson}/questions", None),
            ("GET", f"/api/lessons/{lesson}/questions/{qid}", None),
            ("POST", f"/api/lessons/{lesson}/questions/clear", {}),
            ("DELETE", f"/api/lessons/{lesson}/questions/{qid}", {}),
        ]:
            r = await c.request(method, path, json=body)
            assert (r.status_code, r.json()) == (404, {"error": "not_found"}), (method, path)
    assert await _snapshot(w) == before
    await w.engine.dispose()


async def test_a_queued_question_of_a_deleted_lesson_expires_without_a_model_call(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import reader.tutor.jobs as jobs

    w = await world(pg, tmp_path)
    qid = await insert_question(w, "ivan", question="Queued before the delete?")

    async def deleted(conn: Any, user_id: Any, lesson_id: str) -> bool:
        return False

    monkeypatch.setattr(jobs, "can_open", deleted)
    provider = FakeProvider()
    await run_handler(w, qid, deps(w, provider))
    assert (await question_row(w, qid))[0::3] == ("expired", "expired")
    assert provider.calls == []
    assert await scalar(w.engine, "SELECT count(*) FROM agent_spend WHERE run_id = :q", q=qid) == 0
    await w.engine.dispose()
