"""SRV-OBS ADR-0039 [db]: the owner-only judge routes on real sessions — `GET /api/obs/judge-queue` and
`GET /api/obs/judge-items/{item_id}` (FR-OBS-40: 401/403, 400, 404, 409, no-store), `POST /api/obs/labels`
for a judge target (FR-OBS-24: 409 not_labelable), and `POST /api/obs/eval` (FR-OBS-39 a: refusals, 202
with rows).

@trace FR-OBS-39 FR-OBS-40 FR-OBS-24
"""

from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from auth_helpers import ORIGIN, bootstrap, bootstrap_token, client, ready_engine, settings_for
from conftest import Pg
from softauthn import SoftAuthenticator
from sqlalchemy import text

pytestmark = pytest.mark.anyio
SET = "questions:\n  - {id: e01, question: What is a hardened key?, question_type: exact, chapter: 3}\n"


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


async def _judge_input(engine: Any, handle: str, labelable: bool) -> str:
    rid = uuid.uuid4()
    async with engine.begin() as conn:
        found = await conn.execute(text("SELECT id FROM users WHERE handle = :h"), {"h": handle})
        owner = found.scalar_one()
        await conn.execute(text(
            "INSERT INTO judge_inputs (id, owner_id, slot, input_sha256, judge, query, card_ids, output,"
            " labelable) VALUES (:id, :o, 'judge.find', :h, :j, :q, ARRAY['spn:a', 'spn:b'],"
            " CAST(:out AS jsonb), :l)"),
            {"id": rid, "o": owner, "h": uuid.uuid4().hex * 2, "j": "judge.find@" + "a" * 12,
             "q": "a question" if labelable else None, "out": json.dumps({"keep": [1], "drop": [2]}),
             "l": labelable})
    return str(rid)


async def test_judge_queue_items_and_labels(pg: Pg, tmp_path: Path) -> None:
    from reader.api.app import create_app

    settings, engine, cookies = await _sessions(pg, tmp_path)
    try:
        app = create_app(settings, engine, run_cleanup_task=False)
        open_id = await _judge_input(engine, "ivan", True)
        closed_id = await _judge_input(engine, "live-check", False)
        for cookie, status in ((None, 401), (cookies["live-check"], 403)):
            async with _as(app, cookie) as c:
                assert (await c.get("/api/obs/judge-queue?slot=judge.find")).status_code == status
                assert (await c.get(f"/api/obs/judge-items/ji:{open_id}")).status_code == status
        async with _as(app, cookies["ivan"]) as c:
            for bad in ("slot=judge.align", "slot=judge.find&limit=26", "slot=judge.find&limit=0", ""):
                r = await c.get(f"/api/obs/judge-queue?{bad}")
                assert (r.status_code, r.json()) == (400, {"error": "bad_request"}), bad
            r = await c.get("/api/obs/judge-queue?slot=judge.find")
            assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
            assert [i["id"] for i in r.json()["items"]] == [f"ji:{open_id}"]
            r = await c.get(f"/api/obs/judge-items/ji:{open_id}")
            assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
            assert r.json()["query"] == "a question" and r.json()["output"]["keep"] == ["spn:a"]
            assert (await c.get(f"/api/obs/judge-items/ji:{closed_id}")).status_code == 409
            assert (await c.get(f"/api/obs/judge-items/ji:{uuid.uuid4()}")).status_code == 404
            assert (await c.get("/api/obs/judge-items/nonsense")).status_code == 404
            r = await c.post("/api/obs/labels", json={"target": "judge", "id": f"ji:{closed_id}", "ok": True})
            assert (r.status_code, r.json()) == (409, {"error": "not_labelable"})
            r = await c.post("/api/obs/labels", json={"target": "judge", "id": f"ji:{open_id}", "ok": False,
                                                      "note": "kept the wrong card"})
            assert r.status_code == 200 and r.json()["revision"] == 1
            r = await c.get("/api/obs/judge-queue?slot=judge.find")
            assert r.json()["items"] == []  # labelled: no longer queued
    finally:
        await engine.dispose()


async def test_eval_start(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from reader.api.app import create_app
    from reader.obs import eval as evaluation

    settings, engine, cookies = await _sessions(pg, tmp_path)
    try:
        app = create_app(settings, engine, run_cleanup_task=False)
        body = {"set": SET, "budget_profile": "mcp"}
        async with _as(app, cookies["live-check"]) as c:
            assert (await c.post("/api/obs/eval", json=body)).status_code == 403
        async with _as(app, cookies["ivan"]) as c:
            r = await c.post("/api/obs/eval", json=body)
            assert (r.status_code, r.json()) == (400, {"error": "set_not_pinned"})  # the pin is null today
            monkeypatch.setitem(evaluation.EVAL_SETS["brain-eval-v1"], "sha256",
                                hashlib.sha256(SET.encode()).hexdigest())
            r = await c.post("/api/obs/eval", json=body)
            assert (r.status_code, r.json()) == (400, {"error": "cap_missing"})  # no eval cap configured
            monkeypatch.setattr(evaluation, "BUDGET", (Decimal(5), {}))  # no floor reserve: fails closed
            r = await c.post("/api/obs/eval", json=body)
            assert (r.status_code, r.json()) == (400, {"error": "cap_missing"})
            floors = {s: Decimal("0.01") for s in evaluation.EVAL_SLOTS}
            monkeypatch.setattr(evaluation, "BUDGET", (Decimal(5), floors))
            shapes = ({"set": SET}, {**body, "note": "x"}, {**body, "judges": "judge.eval.relevance"}, [])
            for shape in shapes:
                r = await c.post("/api/obs/eval", json=shape)
                assert (r.status_code, r.json()) == (400, {"error": "bad_request"}), shape
            r = await c.post("/api/obs/eval", json={**body, "judges": ["judge.eval.vibes"]})
            assert (r.status_code, r.json()) == (400, {"error": "bad_request"})
            r = await c.post("/api/obs/eval", json=body)
            assert r.status_code == 202
            eval_id = r.json()["eval_id"]
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT records FROM obs_eval_runs WHERE id = :i"),
                                       {"i": eval_id})).scalar_one() == 1
            assert (await conn.execute(text("SELECT count(*) FROM job_keys WHERE kind = 'obs.eval'"))
                    ).scalar_one() == 1
    finally:
        await engine.dispose()
