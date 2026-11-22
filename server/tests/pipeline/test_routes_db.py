# @trace FR-PIPELINE-1, FR-PIPELINE-11
"""Course import (gate G1) and the compile routes through the ASGI app. [db]"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from .conftest import BY_NAME_HINT, MATERIAL, Pg, client, pipeline_env, plan, scripts_for

pytestmark = pytest.mark.anyio


async def test_course_import_is_idempotent_and_owner_only(pg: Pg, tmp_path: Path) -> None:
    async with pipeline_env(pg, tmp_path) as env:
        async with client(env) as c:
            r = await c.post("/api/pipeline/courses", json=plan())
            assert r.status_code == 200, r.text
            first = r.json()
            assert [x["plan_lesson_id"] for x in first["lessons"]] == ["L01", "L02"]
            again = (await c.post("/api/pipeline/courses", json=plan())).json()
            assert again["lessons"] == first["lessons"]  # same lesson uuids on every import
            changed = plan((61, 63))
            r = await c.post("/api/pipeline/courses", json=changed)
            assert r.json()["lessons"][1]["lesson_id"] == first["lessons"][1]["lesson_id"]
            rows = await env.rows(
                "SELECT pdf_first, pdf_last, g1_at IS NOT NULL FROM course_plans WHERE plan_lesson_id = 'L02'"
            )
            assert rows == [(61, 63, True)]
            for bad in (
                {"course": {}},
                {**plan(), "lessons": [{"id": "L9", "title": "x", "chapter": 1, "pages": {"pdf": [5, 4]}}]},
            ):
                assert (await c.post("/api/pipeline/courses", json=bad)).status_code == 400
            other = copy.deepcopy(plan())
            other["course"]["material_sha256"] = "cd" * 32
            r = await c.post("/api/pipeline/courses", json=other)
            assert (r.status_code, r.json()) == (409, {"error": "no_material"})
        async with client(env, "anna") as c:
            assert (await c.post("/api/pipeline/courses", json=plan())).status_code == 404
        lessons = await env.rows("SELECT count(*) FROM lessons")
        assert lessons == [(2,)]
        assert MATERIAL in str(await env.rows("SELECT material_sha256 FROM courses"))


async def test_a_lesson_inserted_mid_plan_gets_its_own_lesson(pg: Pg, tmp_path: Path) -> None:
    """Code review S5: lessons map by plan lesson id, never by position; course_plans.lesson_id is unique."""
    async with pipeline_env(pg, tmp_path) as env:
        async with client(env) as c:
            first = {
                x["plan_lesson_id"]: x["lesson_id"]
                for x in (await c.post("/api/pipeline/courses", json=plan())).json()["lessons"]
            }
            inserted = plan()
            inserted["lessons"].insert(
                1, {"id": "L01b", "title": "Between", "chapter": 1, "pages": {"pdf": [60, 61]}}
            )
            again = {
                x["plan_lesson_id"]: x["lesson_id"]
                for x in (await c.post("/api/pipeline/courses", json=inserted)).json()["lessons"]
            }
        assert again["L01"] == first["L01"]
        assert again["L02"] == first["L02"]
        assert again["L01b"] not in (first["L01"], first["L02"])
        assert len(set(again.values())) == 3
        titles = dict(await env.rows("SELECT id::text, title FROM lessons"))
        assert titles[again["L01b"]] == "Between"
        assert titles[again["L02"]] == "Consensus"
        unique = await env.rows(
            "SELECT count(*) FROM pg_indexes WHERE tablename = 'course_plans' AND indexdef LIKE '%UNIQUE%(lesson_id)'"
        )
        assert unique == [(1,)]


async def test_compile_routes(pg: Pg, tmp_path: Path) -> None:
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        lesson = await env.import_plan()
        clean = [
            {"stage": "draft", "cost": 0.6},
            {"stage": "fact_check", "cost": 0.24, "unsupported": 0, "contradicted": 0},
            {"stage": "adversarial_review", "cost": 0.5, "blocking": 0, "nonblocking": 1},
        ]
        env.cli.scripts[:] = scripts_for(clean, "pass")
        async with client(env) as c:
            for cap in (0, -1, 8.01, 1.234, "x"):
                r = await c.post(f"/api/lessons/{lesson}/compile", json={"cap_usd": cap})
                assert (r.status_code, r.json()) == (400, {"error": "cap_invalid"}), cap
            r = await c.post(f"/api/lessons/{lesson}/compile")  # no body at all: the default cap
            assert r.status_code == 202, r.text
            assert (await c.get(f"/api/compiles/{r.json()['run_id']}")).json()["cap_usd"] == "8.00"
            run = r.json()["run_id"]
            busy = await c.post(f"/api/lessons/{lesson}/compile", json={"cap_usd": 5})
            assert (busy.status_code, busy.json()) == (409, {"error": "compile_running"})
            assert (await c.get(f"/api/compiles/{run}")).json()["state"] == "running"
            for _ in range(3):
                await env.work()
            body = (await c.get(f"/api/compiles/{run}")).json()
            assert body["state"] == "converged"
            assert [s["stage"] for s in body["steps"]][:3] == ["draft", "fact_check", "adversarial_review"]
            assert body["steps"][0]["model"] == "claude-opus-5-5"
            assert body["spent_usd"] == "1.340000"
            assert body["latest_sha256"]
            latest = (await c.get(f"/api/lessons/{lesson}/compile")).json()
            assert latest["run_id"] == run
            assert (await c.post(f"/api/compiles/{run}/resume")).status_code == 409  # not stage_failed
            assert (await c.get(f"/api/compiles/{'0' * 8}-0000-0000-0000-{'0' * 12}")).status_code == 404
        async with client(env, "anna") as c:
            assert (await c.get(f"/api/compiles/{run}")).status_code == 404
            assert (await c.post(f"/api/lessons/{lesson}/compile", json={})).status_code == 404


async def test_resume_requeues_a_failed_step(pg: Pg, tmp_path: Path) -> None:
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        lesson = await env.import_plan()
        bad = {"stage": "fact_check", "cost": 0.24, "invalid": "summary_only"}
        env.cli.scripts[:] = scripts_for([{"stage": "draft", "cost": 0.6}, bad, bad, bad], "pass")
        async with client(env) as c:
            run = (await c.post(f"/api/lessons/{lesson}/compile", json={})).json()["run_id"]
            for _ in range(4):
                await env.work()
            assert (await c.get(f"/api/compiles/{run}")).json()["state"] == "stage_failed"
            env.cli.scripts[:] = scripts_for(
                [
                    {"stage": "fact_check", "cost": 0.24, "unsupported": 0, "contradicted": 0},
                    {"stage": "adversarial_review", "cost": 0.5, "blocking": 0, "nonblocking": 0},
                ],
                "pass",
            )
            r = await c.post(f"/api/compiles/{run}/resume")
            assert r.status_code == 202, r.text
            for _ in range(3):
                await env.work()
            assert (await c.get(f"/api/compiles/{run}")).json()["state"] == "converged"
