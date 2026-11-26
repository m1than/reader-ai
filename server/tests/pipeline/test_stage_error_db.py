# @trace FR-PIPELINE-16
"""A stopped step's SRV-AGENT error code in the compile status routes (for WEB-UI FR-UI-15). [db]

The fake agent-proxy client refuses to issue a run token for a handle in `missing`, as the real proxy answers 409
`provider_missing` for a user without a loaded token; other permanent codes are raised by replacing its `issue`. No
credit is spent anywhere.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

import pytest

from .conftest import BY_NAME_HINT, Pg, client, need, pipeline_env, scripts_for

pytestmark = pytest.mark.anyio

CLEAN = [
    {"stage": "draft", "cost": 0.6},
    {"stage": "fact_check", "cost": 0.24, "unsupported": 0, "contradicted": 0},
    {"stage": "adversarial_review", "cost": 0.5, "blocking": 0, "nonblocking": 0},
]
BAD_FACT = {"stage": "fact_check", "cost": 0.24, "invalid": "summary_only"}
STORED = "SELECT stage_error FROM compile_runs WHERE id = CAST(:r AS uuid)"
DEAD_INPUT = "SELECT args->>'input_sha256' FROM job_keys WHERE outcome = 'dead' AND args->>'run_id' = :r"


async def _set_stage_error(env: Any, run: str, value: str) -> None:
    from sqlalchemy import text

    async with env.engine.begin() as conn:
        await conn.execute(
            text("UPDATE compile_runs SET stage_error = CAST(:e AS jsonb) WHERE id = CAST(:r AS uuid)"),
            {"e": value, "r": run},
        )


def _raise_on_issue(env: Any, code: str) -> None:
    errors = need("reader.agent.errors")

    async def issue(handle: str, max_output_tokens: int, model: str) -> str:
        raise errors.agent_error(code)

    env.proxy.issue = issue


async def test_provider_missing_is_recorded_reported_and_cleared_by_resume(pg: Pg, tmp_path: Path) -> None:
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
            assert (await c.get(f"/api/lessons/{lesson}/compile")).json()["error"] == "provider_missing"
            dead_input = await env.scalar(DEAD_INPUT, r=run)
            assert await env.scalar(STORED, r=run) == {"step": 1, "input_sha256": dead_input, "code": "provider_missing"}
            # the job key itself still carries the class name (SRV-EVENTS unchanged)
            assert await env.scalar("SELECT error_code FROM job_keys WHERE outcome = 'dead'") == "AgentError"

            env.proxy.missing.clear()
            env.cli.scripts[:] = scripts_for(CLEAN, "pass")
            r = await c.post(f"/api/compiles/{run}/resume")
            assert r.status_code == 202, r.text
            assert r.json()["error"] is None
            assert await env.scalar(STORED, r=run) is None
            for _ in range(5):
                await env.work()
            body = (await c.get(f"/api/compiles/{run}")).json()
            assert (body["state"], body["error"]) == ("converged", None)


async def test_a_changed_input_at_the_same_step_is_not_reported(pg: Pg, tmp_path: Path) -> None:
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        lesson = await env.import_plan()
        env.proxy.missing.add("ivan")
        async with client(env) as c:
            run = (await c.post(f"/api/lessons/{lesson}/compile")).json()["run_id"]
            await env.work()
            assert (await c.get(f"/api/compiles/{run}")).json()["error"] == "provider_missing"
            await _set_stage_error(env, run, '{"step": 1, "input_sha256": "' + "0" * 64 + '", "code": "provider_missing"}')
            body = (await c.get(f"/api/compiles/{run}")).json()
            assert (body["state"], body["error"]) == ("stage_failed", None)


async def test_exhausted_transient_failure_has_no_code_even_with_a_matching_record(pg: Pg, tmp_path: Path) -> None:
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        lesson = await env.import_plan()
        env.cli.scripts[:] = scripts_for([{"stage": "draft", "cost": 0.6}, BAD_FACT, BAD_FACT, BAD_FACT], "pass")
        async with client(env) as c:
            run = (await c.post(f"/api/lessons/{lesson}/compile")).json()["run_id"]
            for _ in range(4):
                await env.work()
            body = (await c.get(f"/api/compiles/{run}")).json()
            assert (body["state"], body["error"]) == ("stage_failed", None)
            assert await env.scalar(STORED, r=run) is None  # a transient code is never recorded
            # a record for this very step and input, as if an earlier permanent failure had written it: the key
            # died of a transient exhaustion, so it is still not reported
            dead_input = await env.scalar(DEAD_INPUT, r=run)
            await _set_stage_error(env, run, '{"step": 2, "input_sha256": "' + dead_input + '", "code": "provider_missing"}')
            assert await env.scalar("SELECT error_code FROM job_keys WHERE outcome = 'dead'") == "TransientAgentError"
            assert (await c.get(f"/api/compiles/{run}")).json()["error"] is None


async def test_a_code_for_another_step_is_not_reported(pg: Pg, tmp_path: Path) -> None:
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        lesson = await env.import_plan()
        env.cli.scripts[:] = scripts_for([{"stage": "draft", "cost": 0.6}, BAD_FACT, BAD_FACT, BAD_FACT], "pass")
        async with client(env) as c:
            run = (await c.post(f"/api/lessons/{lesson}/compile")).json()["run_id"]
            for _ in range(4):
                await env.work()
            dead_input = await env.scalar(DEAD_INPUT, r=run)
            await _set_stage_error(env, run, '{"step": 1, "input_sha256": "' + dead_input + '", "code": "provider_missing"}')
            body = (await c.get(f"/api/compiles/{run}")).json()
            assert body["state"] == "stage_failed"
            assert body["steps"][-1]["step"] == 4  # draft, rules, lab_verification, then fact_check stopped
            assert body["error"] is None


@pytest.mark.parametrize(("code", "recorded"), [("run_token_unknown", False), ("settle_refused", True)])
async def test_only_allow_listed_codes_are_recorded(pg: Pg, tmp_path: Path, code: str, recorded: bool) -> None:
    runs = need("reader.pipeline.runs")
    assert getattr(runs, "STAGE_ERROR_CODES", None) == frozenset(
        {"sdk_error", "tools_mismatch", "unknown_model", "provider_missing", "usage_unmapped", "settle_refused", "void_refused"}
    )
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        lesson = await env.import_plan()
        _raise_on_issue(env, code)
        async with client(env) as c:
            run = (await c.post(f"/api/lessons/{lesson}/compile")).json()["run_id"]
            # a stale record from an earlier failure: an off-list permanent code must clear it (null)
            await _set_stage_error(env, run, '{"step": 9, "input_sha256": "' + "0" * 64 + '", "code": "sdk_error"}')
            await env.work()
            body = (await c.get(f"/api/compiles/{run}")).json()
            assert body["state"] == "stage_failed", code
            assert body["error"] == (code if recorded else None), code
            stored = await env.scalar(STORED, r=run)
            assert (stored is not None) is recorded, code


async def test_a_failing_record_write_lets_the_original_error_through(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runs = need("reader.pipeline.runs")
    assert hasattr(runs, "record_stage_error"), "reader.pipeline.runs.record_stage_error is missing"

    async def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("SECRET-WRITE-DETAIL")

    monkeypatch.setattr(runs, "record_stage_error", broken)
    caplog.set_level(logging.INFO)
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env:
        lesson = await env.import_plan()
        env.proxy.missing.add("ivan")
        async with client(env) as c:
            run = (await c.post(f"/api/lessons/{lesson}/compile")).json()["run_id"]
            await env.work()
            body = (await c.get(f"/api/compiles/{run}")).json()
            assert (body["state"], body["error"]) == ("stage_failed", None)
            assert await env.scalar("SELECT error_code FROM job_keys WHERE outcome = 'dead'") == "AgentError"
    lines = [r.getMessage() for r in caplog.records if r.name == "reader.pipeline"]
    assert any("provider_missing" in m and run in m for m in lines)
    assert all("SECRET-WRITE-DETAIL" not in r.getMessage() for r in caplog.records)


async def test_migration_adds_and_drops_the_column(pg: Pg, tmp_path: Path) -> None:
    from sqlalchemy import text

    path = Path(need("reader.pipeline").__file__).parent / "migrations" / "pipeline_0003_stage_error.py"
    assert path.exists(), path
    spec = importlib.util.spec_from_file_location("pipeline_0003", path)
    assert spec is not None
    assert spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert (migration.revision, migration.down_revision) == ("pipeline_0003", "pipeline_0002")
    assert migration.DOWNGRADE == ("ALTER TABLE compile_runs DROP COLUMN stage_error",)
    columns = "SELECT count(*) FROM information_schema.columns WHERE table_name = 'compile_runs' AND column_name = 'stage_error'"
    async with pipeline_env(pg, tmp_path, floors=BY_NAME_HINT) as env, env.engine.connect() as conn:
        tx = await conn.begin()
        assert (await conn.execute(text(columns))).scalar() == 1
        for statement in migration.DOWNGRADE:
            await conn.execute(text(statement))
        assert (await conn.execute(text(columns))).scalar() == 0
        await tx.rollback()
