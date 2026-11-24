"""SRV-OBS phase 2 [db]: the owner-only upload and label routes on real sessions and the paired migration
`obs_0002` — `POST /api/obs/bench` (FR-OBS-23: every status and code, the stored row, the outbox job),
`POST /api/obs/labels` (FR-OBS-24: the record, relabel revisions, `mirror`), the summary's `alerts` and
`stack` (FR-OBS-25, -32), and no canary in any phase 2 row (INV-OBS-2).

@trace FR-OBS-23 FR-OBS-24 FR-OBS-25 FR-OBS-32 INV-OBS-2
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from auth_helpers import ORIGIN, bootstrap, bootstrap_token, client, ready_engine, scalar, settings_for
from conftest import Pg
from softauthn import SoftAuthenticator

pytestmark = pytest.mark.anyio
REPO = Path(__file__).resolve().parents[3]
V1 = (REPO / "spikes/embeddings-local/questions.yaml").read_text(encoding="utf-8")
CANARY = "bookcanary-7f3a9c"


async def _sessions(pg: Pg, tmp: Path) -> tuple[Any, Any, dict[str, str]]:
    settings = settings_for(tmp)
    engine = await ready_engine(pg, settings)
    cookies: dict[str, str] = {}
    for handle in ("ivan", "live-check"):
        async with client(settings, engine) as c:
            response, _ = await bootstrap(
                c, await bootstrap_token(engine, settings, handle), SoftAuthenticator.random()
            )
            assert response.status_code == 200, response.text
            cookies[handle] = c.cookies.get("__Host-reader_session") or ""
    return settings, engine, cookies


def _as(app: Any, cookie: str | None) -> httpx.AsyncClient:
    jar = {"__Host-reader_session": cookie} if cookie else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN, cookies=jar,
                             headers={"Origin": ORIGIN})


def _result(**over: Any) -> dict[str, Any]:
    from obs.conftest import load_yaml

    ids = [q["id"] for q in load_yaml(REPO / "spikes/embeddings-local/questions.yaml")["questions"]]
    sets = load_yaml(REPO / "spec/fixtures/SRV-OBS/phase2.yaml")["bench_sets"]
    excluded = set(sets["search-bench-v1"]["exclude"])
    per = [{"id": i, "rank": 1, "hit": True, "top_pages": [1]} for i in ids if i not in excluded]
    body = {"kind": "search", "model": "BAAI/bge-m3", "chunk_chars": 1200, "overlap": 200,
            "questions": len(per), "per_question": per}
    body.update(over)
    return body


async def test_bench_upload_statuses_and_the_stored_row(pg: Pg, tmp_path: Path) -> None:
    from reader.api.app import create_app

    settings, engine, cookies = await _sessions(pg, tmp_path)
    try:
        app = create_app(settings, engine, run_cleanup_task=False)
        for cookie, status in ((None, 401), (cookies["live-check"], 403)):
            async with _as(app, cookie) as c:
                r = await c.post("/api/obs/bench", json={"set": V1, "result": _result()})
                assert r.status_code == status
        async with _as(app, cookies["ivan"]) as c:
            big = await c.post("/api/obs/bench", json={"set": V1 + " " * 262_144, "result": _result()})
            assert (big.status_code, big.json()) == (413, {"error": "too_large"})
            r = await c.post("/api/obs/bench", json={"set": V1 + "\n# " + CANARY, "result": _result()})
            assert (r.status_code, r.json()) == (400, {"error": "set_not_pinned"})
            r = await c.post("/api/obs/bench", json={"set": V1, "result": _result(note=CANARY)})
            assert (r.status_code, r.json()) == (400, {"error": "bench_invalid"})
            r = await c.post("/api/obs/bench", json={"set": V1, "result": _result(kind="tutor")})
            assert (r.status_code, r.json()) == (400, {"error": "bench_invalid"})
            ok = await c.post("/api/obs/bench", json={"set": V1, "result": _result()})
            assert ok.status_code == 202 and set(ok.json()) == {"upload_id"}
            again = await c.post("/api/obs/bench", json={"set": V1, "result": _result()})
            assert (again.status_code, again.json()) == (409, {"error": "already_recorded"})
        upload = ok.json()["upload_id"]
        row = json.loads(await scalar(engine, "SELECT row_to_json(r)::text FROM obs_bench_results r"))
        assert row["id"] == upload and row["kind"] == "search" and row["set_name"] == "search-bench-v1"
        assert row["scores"]["hit_at_1"] == 1.0 and "recall_at_10" not in row["scores"]
        assert await scalar(engine, "SELECT count(*) FROM obs_bench_results") == 1
        args = await scalar(engine, "SELECT args::text FROM job_keys WHERE kind = 'obs.bench'")
        assert json.loads(args) == {"upload_id": upload}  # the outbox: same transaction as the row
        dump = await scalar(engine, "SELECT coalesce(json_agg(r)::text, '') FROM obs_bench_results r")
        assert CANARY not in dump  # INV-OBS-2
    finally:
        await engine.dispose()


async def test_labels_record_relabel_and_errors(pg: Pg, tmp_path: Path) -> None:
    from pipeline.conftest import import_material, plan, synthetic_spans
    from reader.api.app import create_app
    from reader.pipeline import courses

    settings, engine, cookies = await _sessions(pg, tmp_path)
    try:
        owner = await scalar(engine, "SELECT id FROM users WHERE handle = 'ivan'")
        await import_material(engine, owner, synthetic_spans())
        async with engine.begin() as conn:
            await courses.import_plan(conn, owner, plan())
        lesson = str(await scalar(engine, "SELECT lesson_id FROM course_plans WHERE plan_lesson_id = 'L02'"))
        app = create_app(settings, engine, run_cleanup_task=False)
        async with _as(app, cookies["ivan"]) as c:
            first = await c.post("/api/obs/labels", json={"target": "lesson", "id": lesson, "ok": False,
                                                          "reason": "wrong_fact"})
            assert (first.status_code, first.json()) == (200, {"revision": 1, "mirror": "off"})
            second = await c.post("/api/obs/labels", json={"target": "lesson", "id": lesson, "ok": True})
            assert second.json() == {"revision": 2, "mirror": "off"}
            unknown = {"target": "lesson", "id": str(uuid.uuid4()), "ok": True}
            missing = await c.post("/api/obs/labels", json=unknown)
            assert (missing.status_code, missing.json()) == (404, {"error": "not_found"})
            for bad in ({"target": "course", "id": lesson, "ok": True},
                        {"target": "lesson", "id": lesson, "ok": "yes"},
                        {"target": "lesson", "id": lesson, "ok": True, "reason": CANARY},
                        {"target": "lesson", "id": lesson, "ok": True, "note": "free text"}):
                r = await c.post("/api/obs/labels", json=bad)
                assert (r.status_code, r.json()) == (400, {"error": "bad_request"}), bad
        async with _as(app, cookies["live-check"]) as c:
            assert (await c.post("/api/obs/labels", json={"target": "lesson", "id": lesson, "ok": True})
                    ).status_code == 403
        assert await scalar(engine, "SELECT count(*) FROM owner_labels") == 1
        assert await scalar(engine, "SELECT revision FROM owner_labels") == 2
        labels_jobs = "SELECT count(*) FROM job_keys WHERE kind = 'obs.label'"
        assert await scalar(engine, labels_jobs) == 0  # tracing off
        dump = await scalar(engine, "SELECT json_agg(r)::text FROM owner_labels r")
        assert CANARY not in dump and "free text" not in dump
    finally:
        await engine.dispose()


async def test_summary_carries_alerts_and_stack(pg: Pg, tmp_path: Path, monkeypatch: Any) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from reader.api.app import create_app

    settings, engine, cookies = await _sessions(pg, tmp_path)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO obs_stack_status (member, ok, detail, checked_at) VALUES "
                "('mlflow', true, 'healthy', now()), ('rotel', false, 'unreachable', now())"))
            with pytest.raises(IntegrityError, match="check"):  # the member CHECK
                async with conn.begin_nested():
                    await conn.execute(text("INSERT INTO obs_stack_status (member, ok, detail, checked_at) "
                                            "VALUES ('jaeger', true, 'healthy', now())"))
        from reader.obs import phase2

        app = create_app(settings, engine, run_cleanup_task=False)
        async with _as(app, cookies["ivan"]) as c:
            monkeypatch.setattr(phase2, "tracing_on", lambda: False)
            off = (await c.get("/api/obs/summary")).json()
            monkeypatch.setattr(phase2, "tracing_on", lambda: True)
            body = (await c.get("/api/obs/summary")).json()
        assert not any(a["code"] == "obs_unhealthy" for a in off["alerts"])  # tracing off: nothing probes
        assert {"alerts", "stack"} <= set(body)
        assert {"code": "obs_unhealthy", "subject": "rotel"}.items() <= next(
            a for a in body["alerts"] if a["code"] == "obs_unhealthy").items()
        assert body["stack"]["mlflow"]["ok"] is True and body["stack"]["rotel"]["ok"] is False
    finally:
        await engine.dispose()
