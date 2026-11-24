"""SRV-OBS phase 2 [db], code review round 1 fixes (ADR-0038): the label mirror's trace resolution and
handler (FR-OBS-24 a, c: the label is for the lesson's current revision; the trace is the revise, else draft,
stage job of the compile run that produced that revision, one row per label), the bench handler (FR-OBS-23:
tracing off, a re-run reads the same stored row), and the routes' owner scoping, exact keys and refusals
(FR-OBS-10, -23, -24, -25).

@trace FR-OBS-23 FR-OBS-24 FR-OBS-25 INV-OBS-2
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from auth_helpers import ORIGIN, bootstrap, bootstrap_token, client, ready_engine, scalar, settings_for
from conftest import Pg
from manage_helpers import course_for
from softauthn import SoftAuthenticator
from sqlalchemy import text

pytestmark = pytest.mark.anyio
REPO = Path(__file__).resolve().parents[3]
if str(REPO / "server" / "tests") not in sys.path:  # `obs.conftest` (obs_setup) when this file runs alone
    sys.path.insert(0, str(REPO / "server" / "tests"))
V1 = (REPO / "spikes/embeddings-local/questions.yaml").read_text(encoding="utf-8")
PHASE2 = REPO / "spec/fixtures/SRV-OBS/phase2.yaml"
# One owner per instance (FR-AUTH-1): another user's data (live-check's) stands for "not the caller's".
USERS = (("ivan", "owner"), ("live-check", "learner"))


async def _sessions(pg: Pg, tmp: Path) -> tuple[Any, Any, dict[str, str]]:
    settings = settings_for(tmp, users=USERS)
    engine = await ready_engine(pg, settings)
    cookies: dict[str, str] = {}
    for handle, _ in USERS:
        async with client(settings, engine) as c:
            response, _ = await bootstrap(
                c, await bootstrap_token(engine, settings, handle), SoftAuthenticator.random()
            )
            assert response.status_code == 200, response.text
            cookies[handle] = c.cookies.get("__Host-reader_session") or ""
    return settings, engine, cookies


def _as(app: Any, cookie: str | None, origin: str = ORIGIN) -> httpx.AsyncClient:
    jar = {"__Host-reader_session": cookie} if cookie else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN, cookies=jar,
                             headers={"Origin": origin})


def _result(rank: int | None = 1, chunk_chars: int = 1200) -> dict[str, Any]:
    from obs.conftest import load_yaml

    ids = [q["id"] for q in load_yaml(REPO / "spikes/embeddings-local/questions.yaml")["questions"]]
    excluded = set(load_yaml(PHASE2)["bench_sets"]["search-bench-v1"]["exclude"])
    hit = rank is not None
    per = [{"id": i, "rank": rank, "hit": hit, "top_pages": [1]} for i in ids if i not in excluded]
    return {"kind": "search", "model": "BAAI/bge-m3", "chunk_chars": chunk_chars, "overlap": 200,
            "questions": len(per), "per_question": per}


async def _second_revision(engine: Any, owner: Any, lesson: Any, tmp: Path) -> str:
    from reader.lessons import store

    doc = json.loads((Path(store.__file__).parent / "fixtures" / "F01.ipynb").read_bytes())
    doc["cells"][0]["source"] = ["a second revision"]
    data = json.dumps(doc).encode()
    async with engine.begin() as conn:
        sha, _ = await store.write_revision(conn, tmp / "blobs", owner, lesson, data, "fixture", {})
    return str(sha)


async def _run(conn: Any, owner: Any, lesson: Any, final: str | None, at: str) -> str:
    row = (await conn.execute(text(
        "INSERT INTO compile_runs (owner_id, lesson_id, cap_usd, task_budgets, final_sha256, created_at)"
        " VALUES (:o, :l, 8, '{}'::jsonb, :f, :at) RETURNING id::text"),
        {"o": owner, "l": lesson, "f": final, "at": datetime.fromisoformat(at)})).one()
    return str(row[0])


async def _stage_key(conn: Any, run: str, stage: str, job_id: int, at: str) -> None:
    args = {"run_id": run, "stage": stage, "step": job_id}
    await conn.execute(
        text("INSERT INTO job_keys (kind, key, args, job_id, created_at)"
             " VALUES ('pipeline.stage', :key, CAST(:a AS jsonb), :j, :at)"),
        {"key": f"k{job_id}", "a": json.dumps(args), "j": job_id, "at": datetime.fromisoformat(at)},
    )


async def _label_row(conn: Any, owner: Any, lesson: Any, sha: str) -> Any:
    from reader.obs import labels

    label_id = uuid.uuid4()
    await conn.execute(
        text("INSERT INTO owner_labels"
             " (id, owner_id, target, target_id, lesson_revision_sha256, ok, revision)"
             " VALUES (:id, :o, 'lesson', :l, :s, true, 1)"),
        {"id": label_id, "o": owner, "l": lesson, "s": sha},
    )
    return (await conn.execute(text(labels.ROW_SQL), {"id": label_id})).one()


async def test_a_label_is_for_the_current_revision_and_finds_its_run(pg: Pg, tmp_path: Path) -> None:
    from reader.api.app import create_app
    from reader.obs import labels

    settings, engine, cookies = await _sessions(pg, tmp_path)
    try:
        ivan = await scalar(engine, "SELECT id FROM users WHERE handle = 'ivan'")
        other = await scalar(engine, "SELECT id FROM users WHERE handle = 'live-check'")
        _, lesson, first = await course_for(engine, ivan, tmp_path)
        second = await _second_revision(engine, ivan, lesson, tmp_path)
        app = create_app(settings, engine, run_cleanup_task=False)
        async with _as(app, cookies["ivan"]) as c:
            r = await c.post("/api/obs/labels", json={"target": "lesson", "id": str(lesson), "ok": True})
            assert r.status_code == 200 and set(r.json()) == {"revision", "mirror"}
        # FR-OBS-24 (a): the current revision (the newest stage record), not published_sha256 (NULL here)
        assert await scalar(engine, "SELECT lesson_revision_sha256 FROM owner_labels") == second

        _, other_lesson, _ = await course_for(engine, other, tmp_path)
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM owner_labels"))
            old = await _run(conn, ivan, lesson, first, "2026-11-20T10:00:00Z")
            await _stage_key(conn, old, "revise", 11, "2026-11-20T10:05:00Z")
            produced = await _run(conn, ivan, lesson, second, "2026-11-21T10:00:00Z")
            await _stage_key(conn, produced, "draft", 21, "2026-11-21T10:01:00Z")
            await _stage_key(conn, produced, "revise", 22, "2026-11-21T10:02:00Z")
            await _stage_key(conn, produced, "revise", 23, "2026-11-21T10:03:00Z")  # a resumed revise
            await _stage_key(conn, produced, "fact_check", 24, "2026-11-21T10:04:00Z")
            later = await _run(conn, ivan, lesson, first, "2026-11-22T10:00:00Z")  # newer, other revision
            await _stage_key(conn, later, "revise", 31, "2026-11-22T10:05:00Z")
            await _run(conn, ivan, lesson, None, "2026-11-23T10:00:00Z")  # an unfinished run
            drafted = await _run(conn, ivan, lesson, "d" * 64, "2026-11-19T10:00:00Z")
            await _stage_key(conn, drafted, "draft", 41, "2026-11-19T10:01:00Z")  # converged: no revise
            foreign = await _run(conn, other, other_lesson, second, "2026-11-24T10:00:00Z")
            await _stage_key(conn, foreign, "revise", 51, "2026-11-24T10:01:00Z")

            # L2 (that revision's run), L5 (one row), L8 (the newest key)
            got = await labels.labelled_job(conn, await _label_row(conn, ivan, lesson, second))
            assert (got.job_id, got.key) == (23, "k23")
            got = await labels.labelled_job(conn, await _label_row(conn, ivan, lesson, first))
            assert got.job_id == 31  # the newest run that produced `first`
            got = await labels.labelled_job(conn, await _label_row(conn, ivan, lesson, "d" * 64))
            assert got.job_id == 41  # L3: the draft stage when the run has no revise
            assert await labels.labelled_job(conn, await _label_row(conn, ivan, lesson, "e" * 64)) is None
            assert await labels.labelled_job(conn, await _label_row(conn, ivan, lesson, "")) is None
    finally:
        await engine.dispose()


async def test_the_label_handler_off_stale_and_saved(pg: Pg, tmp_path: Path, monkeypatch: Any) -> None:
    from obs.conftest import obs_setup
    from reader.obs import labels

    _, engine, _ = await _sessions(pg, tmp_path)
    try:
        ivan = await scalar(engine, "SELECT id FROM users WHERE handle = 'ivan'")
        _, lesson, sha = await course_for(engine, ivan, tmp_path)
        async with engine.begin() as conn:
            run = await _run(conn, ivan, lesson, sha, "2026-11-21T10:00:00Z")
            await _stage_key(conn, run, "revise", 7, "2026-11-21T10:02:00Z")
            await _label_row(conn, ivan, lesson, sha)
            await conn.execute(text("UPDATE owner_labels SET revision = 2"))
        label_id = str(await scalar(engine, "SELECT id FROM owner_labels"))
        calls: list[dict[str, Any]] = []

        async def mirror(label: Any, **kw: Any) -> str | None:
            calls.append({"revision": label.revision, **kw})
            return None if kw["job_revision"] < label.revision else "a-7"

        monkeypatch.setattr(labels, "mirror", mirror)

        async def handle(revision: int, attempts: int = 0) -> None:
            async with engine.begin() as conn:
                args = labels.LabelArgs(label_id=label_id, revision=revision)
                await labels.handle(args, conn, SimpleNamespace(attempts=attempts))

        with obs_setup("worker", enabled=False):
            await handle(2)  # tracing off: no request, nothing saved
        assert calls == [] and await scalar(engine, "SELECT assessment_id FROM owner_labels") is None
        with obs_setup("worker"):
            await handle(1)  # a stale job: the mirror makes no request
            assert await scalar(engine, "SELECT assessment_id FROM owner_labels") is None
            await handle(2)
            await handle(2, attempts=2)  # a retry repeats the same lookup; the last attempt is flagged
        assert await scalar(engine, "SELECT assessment_id FROM owner_labels") == "a-7"
        assert [(c["job_revision"], c["last"]) for c in calls] == [(1, False), (2, False), (2, True)]
        assert {c["trace_ref"] for c in calls} == {labels.trace_ref(7, "k7")}
    finally:
        await engine.dispose()


async def test_the_bench_handler_off_then_stored_row(pg: Pg, tmp_path: Path, monkeypatch: Any) -> None:
    from obs.conftest import obs_setup
    from reader.obs import bench

    _, engine, _ = await _sessions(pg, tmp_path)
    try:
        ivan = await scalar(engine, "SELECT id FROM users WHERE handle = 'ivan'")
        async with engine.begin() as conn:
            upload_id = await bench.record(conn, ivan, bench.validate(V1, _result()))
        assert upload_id is not None
        rows: list[dict[str, Any]] = []

        async def write(client: Any, uid: str, row: Any) -> str:
            rows.append(dict(row))
            return "r-1"

        monkeypatch.setattr(bench, "write", write)

        async def handle(uid: str) -> None:
            async with engine.begin() as conn:
                await bench.handle(bench.BenchArgs(upload_id=uid), conn, SimpleNamespace(attempts=0))

        with obs_setup("worker", enabled=False):
            await handle(str(upload_id))  # tracing off
        assert rows == []
        with obs_setup("worker"):
            await handle(str(upload_id))
            await handle(str(upload_id))
            await handle(str(uuid.uuid4()))  # an unknown upload ends without a request
        assert len(rows) == 2 and rows[0] == rows[1]  # every value comes from the stored row
        assert rows[0]["scores"]["hit_at_1"] == 1.0
        assert rows[0]["params"] == {"model": "BAAI/bge-m3", "chunk_chars": 1200, "overlap": 200}
        assert isinstance(rows[0]["created_at"], datetime) and rows[0]["created_at"].tzinfo is not None
    finally:
        await engine.dispose()


async def test_routes_scope_by_owner_and_refuse_by_origin_and_session(pg: Pg, tmp_path: Path) -> None:
    from obs.conftest import obs_setup
    from reader.api.app import create_app
    from reader.obs import bench

    settings, engine, cookies = await _sessions(pg, tmp_path)
    try:
        ivan = await scalar(engine, "SELECT id FROM users WHERE handle = 'ivan'")
        other = await scalar(engine, "SELECT id FROM users WHERE handle = 'live-check'")
        _, lesson, _ = await course_for(engine, ivan, tmp_path)
        _, foreign, _ = await course_for(engine, other, tmp_path)
        label = {"target": "lesson", "id": str(lesson), "ok": True}
        upload = {"set": V1, "result": _result()}
        for rank in (1, None):  # another user's regressed bench series
            async with engine.begin() as conn:
                theirs = bench.validate(V1, _result(rank, chunk_chars=1300))
                assert await bench.record(conn, other, theirs) is not None
        app = create_app(settings, engine, run_cleanup_task=False)
        async with _as(app, None) as c:
            for path, body in (("/api/obs/labels", label), ("/api/obs/bench", upload)):
                assert (await c.post(path, json=body)).status_code == 401, path
        async with _as(app, cookies["ivan"], origin="https://evil.example") as c:
            for path, body in (("/api/obs/labels", label), ("/api/obs/bench", upload)):
                assert (await c.post(path, json=body)).status_code == 403, path
        async with _as(app, cookies["ivan"]) as c:  # another user's lesson reads as a missing one
            r = await c.post("/api/obs/labels", json={**label, "id": str(foreign)})
            assert (r.status_code, r.json()) == (404, {"error": "not_found"})
            before = (await c.get("/api/obs/summary")).json()
        assert not any(a["code"] == "bench_regressed" for a in before["alerts"])  # another user's rows
        with obs_setup("api"):
            async with _as(app, cookies["ivan"]) as c:
                r = await c.post("/api/obs/labels", json=label)
                assert (r.status_code, r.json()) == (200, {"revision": 1, "mirror": "queued"})
                r = await c.post("/api/obs/bench", json=upload)
                assert r.status_code == 202 and set(r.json()) == {"upload_id"}
                r = await c.post("/api/obs/bench", json={"set": V1, "result": _result(None)})
                assert r.status_code == 202
                mine = (await c.get("/api/obs/summary")).json()
        job = json.loads(await scalar(engine, "SELECT args::text FROM job_keys WHERE kind = 'obs.label'"))
        assert set(job) == {"label_id", "revision"} and job["revision"] == 1
        regressions = [a["subject"] for a in mine["alerts"] if a["code"] == "bench_regressed"]
        assert regressions == ["search/search-bench-v1/BAAI/bge-m3"]  # the caller's series only
        for a in mine["alerts"]:
            assert set(a) == {"code", "subject", "value", "limit"}
    finally:
        await engine.dispose()
