# @trace FR-PIPELINE-16
"""A stopped step's SRV-AGENT error code in the compile status routes (for WEB-UI FR-UI-13). [db]

The fake agent-proxy client refuses to issue a run token for a handle in `missing`, as the real proxy answers 409
`provider_missing` for a user without a loaded token; no credit is spent anywhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import BY_NAME_HINT, Pg, client, pipeline_env, scripts_for

pytestmark = pytest.mark.anyio

CLEAN = [
    {"stage": "draft", "cost": 0.6},
    {"stage": "fact_check", "cost": 0.24, "unsupported": 0, "contradicted": 0},
    {"stage": "adversarial_review", "cost": 0.5, "blocking": 0, "nonblocking": 0},
]


async def test_provider_missing_is_reported_and_cleared_by_resume(pg: Pg, tmp_path: Path) -> None:
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        lesson = await env.import_plan()
        env.proxy.missing.add("ivan")
        async with client(env) as c:
            r = await c.post(f"/api/lessons/{lesson}/compile")
            assert r.status_code == 202, r.text
            run = r.json()["run_id"]
            assert (await c.get(f"/api/compiles/{run}")).json()["error"] is None  # queued, nothing failed yet
            await env.work()
            body = (await c.get(f"/api/compiles/{run}")).json()
            assert (body["state"], body["error"]) == ("stage_failed", "provider_missing")
            assert body["steps"][-1]["stage"] == "draft"
            latest = (await c.get(f"/api/lessons/{lesson}/compile")).json()
            assert latest["error"] == "provider_missing"
            stored = await env.scalar("SELECT stage_error FROM compile_runs WHERE id = CAST(:r AS uuid)", r=run)
            assert stored == {"step": 1, "code": "provider_missing"}
            # the job key itself still carries the class name (SRV-EVENTS unchanged)
            assert await env.scalar("SELECT error_code FROM job_keys WHERE outcome = 'dead'") == "AgentError"

            env.proxy.missing.clear()
            env.cli.scripts[:] = scripts_for(CLEAN, "pass")
            r = await c.post(f"/api/compiles/{run}/resume")
            assert r.status_code == 202, r.text
            assert r.json()["error"] is None
            assert await env.scalar("SELECT stage_error FROM compile_runs WHERE id = CAST(:r AS uuid)", r=run) is None
            for _ in range(5):
                await env.work()
            body = (await c.get(f"/api/compiles/{run}")).json()
            assert (body["state"], body["error"]) == ("converged", None)


async def test_exhausted_transient_failure_has_no_code(pg: Pg, tmp_path: Path) -> None:
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        lesson = await env.import_plan()
        bad = {"stage": "fact_check", "cost": 0.24, "invalid": "summary_only"}
        env.cli.scripts[:] = scripts_for([{"stage": "draft", "cost": 0.6}, bad, bad, bad], "pass")
        async with client(env) as c:
            run = (await c.post(f"/api/lessons/{lesson}/compile")).json()["run_id"]
            for _ in range(4):
                await env.work()
            body = (await c.get(f"/api/compiles/{run}")).json()
            assert (body["state"], body["error"]) == ("stage_failed", None)
            assert await env.scalar("SELECT stage_error FROM compile_runs WHERE id = CAST(:r AS uuid)", r=run) is None


async def test_a_stale_code_is_not_reported_for_another_step(pg: Pg, tmp_path: Path) -> None:
    """The code belongs to the step it was recorded for; a later failure of another step reports none."""
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        lesson = await env.import_plan()
        bad = {"stage": "fact_check", "cost": 0.24, "invalid": "summary_only"}
        env.cli.scripts[:] = scripts_for([{"stage": "draft", "cost": 0.6}, bad, bad, bad], "pass")
        async with client(env) as c:
            run = (await c.post(f"/api/lessons/{lesson}/compile")).json()["run_id"]
            from sqlalchemy import text

            async with env.engine.begin() as conn:  # as if step 1 had once recorded a code
                await conn.execute(
                    text(
                        "UPDATE compile_runs SET stage_error = CAST(:e AS jsonb) WHERE id = CAST(:r AS uuid)"
                    ),
                    {"e": '{"step": 1, "code": "provider_missing"}', "r": run},
                )
            for _ in range(4):
                await env.work()
            body = (await c.get(f"/api/compiles/{run}")).json()
            assert body["state"] == "stage_failed"
            assert body["steps"][-1]["step"] == 2
            assert body["error"] is None
