# @trace VEC-PIPELINE-2, FR-PIPELINE-5, FR-PIPELINE-6, FR-PIPELINE-12, INV-PIPELINE-1, INV-PIPELINE-2, INV-PIPELINE-3, INV-PIPELINE-4
"""The compile loop end to end on the real job machinery: each VEC-PIPELINE-2 case and recompile, run state
derivation, provenance, citations and logs. [db]"""

from __future__ import annotations

import json
import logging
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from .conftest import (
    CANARY,
    FIXTURES,
    MATERIAL,
    Pg,
    load_yaml,
    need,
    pipeline_env,
    scripts_for,
    synthetic_spans,
)

pytestmark = pytest.mark.anyio
CASES = load_yaml(FIXTURES / "loop-cases.yaml")
DEFAULTS = CASES["defaults"]
BY_NAME = {c["name"]: c for c in CASES["cases"]}


def _env_kwargs(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_budgets": case.get("task_budgets", DEFAULTS["task_budgets"]),
        "labs": case.get("labs", "pass"),
        "floors": DEFAULTS["reserves"],
    }


async def _open_cells(env: Any, sha256: str) -> dict[str, int]:
    nb = await env.notebook(sha256)
    counts = {"blocking": 0, "nonblocking": 0}
    for cell in nb["cells"]:
        reader = cell["metadata"]["reader"]
        if reader["kind"] == "open" and reader.get("provenance", {}).get("stage") in (
            "findings",
            "lab_verification",
        ):
            counts["blocking" if reader["finding"]["blocking"] else "nonblocking"] += 1
    return counts


async def _spent(env: Any, run: uuid.UUID) -> Decimal:
    from reader.agent import ledger

    return await ledger.spent(env.engine, run_id=run)  # type: ignore[no-any-return]


async def _prior_spend(env: Any, task: str, usd: float) -> None:
    from reader.agent import ledger

    caps = ledger.Caps(Decimal(100), None, None, None)
    rid = await ledger.reserve(
        env.engine,
        ledger.Scope(env.users["ivan"], uuid.uuid4(), env.lesson, "x", task, 0),
        "claude-opus-5-5",
        Decimal(str(usd)),
        caps,
    )
    await ledger.settle(
        env.engine,
        rid,
        Decimal(str(usd)),
        Decimal(str(usd)),
        Decimal(str(usd)),
        None,
        ledger.UpstreamRecord((200,), ()),
    )


async def _run_case(env: Any, case: dict[str, Any]) -> tuple[uuid.UUID, dict[str, Any]]:
    env.cli.scripts[:] = scripts_for(case["results"], case.get("labs", "pass"))
    if not env.lesson:
        await env.import_plan()
    for task, usd in (case.get("prior_runs_spent") or {}).items():
        await _prior_spend(env, task, usd)
    run = await env.compile(str(case.get("cap", DEFAULTS["cap"])))
    for _ in range(4):  # the worker drains; transient retries are re-enqueued with no delay
        await env.work()
    return run, await env.state(run)


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: c["name"])
async def test_loop_case(
    pg: Pg, tmp_path: Path, case: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    async with pipeline_env(pg, tmp_path, **_env_kwargs(case)) as env:
        run, status = await _run_case(env, case)
        assert status["state"] == case["expect_state"], status
        assert [s["stage"] for s in status["steps"]] == case["expect_stages"]
        assert env.cli.scripts == []  # every scripted model call happened, no more
        if "expect_refusals" in case:
            assert status["refusals"] == case["expect_refusals"]
        if "expect_spent" in case:
            assert await _spent(env, run) == Decimal(str(case["expect_spent"]))
        if status["latest_sha256"] and case["expect_stages"]:
            assert await _open_cells(env, status["latest_sha256"]) == case["expect_open_cells"]
        else:
            assert case["expect_open_cells"] == {"blocking": 0, "nonblocking": 0}
        settled = sum((Decimal(str(r["cost"])) for r in case["results"] if "cost" in r), Decimal(0))
        if case["expect_state"] != "stage_failed" and not any("error" in r for r in case["results"]):
            assert await _spent(env, run) == settled  # INV-PIPELINE-1: spend = the reported costs, within cap
        assert await _spent(env, run) <= Decimal(str(case.get("cap", DEFAULTS["cap"])))
        # INV-PIPELINE-4: no source text, prompt or output in any log line
        assert CANARY not in "\n".join(r.getMessage() for r in caplog.records)
        assert "Claim 1." not in "\n".join(r.getMessage() for r in caplog.records)


async def test_provenance_and_citations_of_every_revision(pg: Pg, tmp_path: Path) -> None:
    """FR-PIPELINE-12 and INV-PIPELINE-3 on the measured outcome (not converged after one loop)."""
    case = BY_NAME["not-converged-after-loop"]
    async with pipeline_env(pg, tmp_path, **_env_kwargs(case)) as env:
        run, status = await _run_case(env, case)
        records = await env.rows(
            "SELECT stage, provenance, sha256 FROM lesson_revision_stages WHERE lesson_id = :l ORDER BY id",
            l=env.lesson,
        )
        assert [r[0] for r in records] == case["expect_stages"]
        for _, provenance, _sha in records:
            assert set(provenance) <= {"input_sha256", "prompt_version", "model", "cost_usd", "duration_s"}
            assert {"input_sha256", "cost_usd", "duration_s"} <= set(provenance)
        assert records[0][1]["model"] == "claude-opus-5-5" and records[1][1]["model"] == "claude-sonnet-5-5"
        spans = {s.span_id: s for s in env.spans.spans}
        for _, _, sha in records:
            nb = await env.notebook(sha)
            for cell in nb["cells"]:
                for claim in cell["metadata"]["reader"].get("claims", []):
                    for citation in claim["citations"]:
                        if "span" in citation:
                            span = spans[citation["span"]]
                            assert citation["page"] == span.page
                            assert citation["material_title"] == env.spans.title
        assert status["g2"]["publishable"] is False
        assert status["spent_usd"] == str(await _spent(env, run))
        assert status["task_budgets"] == {"draft": "4.0", "fact_check": "2.0", "review": "4.0"}


async def test_recompiles_reuse_unchanged_steps(pg: Pg, tmp_path: Path) -> None:
    """INV-PIPELINE-2: a model step never calls the provider when an earlier step of the lesson had its input."""
    prior = BY_NAME["not-converged-after-loop"]
    for recompile in CASES["recompiles"]:
        sub = tmp_path / recompile["name"]
        sub.mkdir()
        async with pipeline_env(pg, sub, **_env_kwargs(prior)) as env:
            await _run_case(env, prior)
            changed = recompile["changed"]
            if "adversarial_review.prompt_version" in changed:
                p = env.runtime.prompts
                env.runtime.prompts = p.with_text(
                    "adversarial_review", p.text("adversarial_review") + "\nNew rule."
                )
            if "source" in changed:
                env.spans.spans[:] = synthetic_spans(extra=" (edited)")
            results = recompile.get("results", prior["results"] if "source" in changed else [])
            env.cli.scripts[:] = scripts_for(results, "pass")
            calls_before = len(env.cli.requests)
            run = await env.compile()
            for _ in range(3):
                await env.work()
            status = await env.state(run)
            reused = [s["stage"] for s in status["steps"] if s["reused"]]
            assert reused == recompile["expect_reused"], recompile["name"]
            if "expect_provider_calls" in recompile:
                assert len(env.cli.requests) - calls_before == recompile["expect_provider_calls"]
            if "expect_spent" in recompile:
                assert await _spent(env, run) == Decimal(str(recompile["expect_spent"]))
            if "expect_state" in recompile:
                assert status["state"] == recompile["expect_state"]


async def test_stalled_run_is_resumed_by_ensure(pg: Pg, tmp_path: Path) -> None:
    """FR-PIPELINE-6: a lost enqueue leaves the run `stalled`; ensure re-enqueues its next step."""
    case = BY_NAME["clean-first-pass"]
    async with pipeline_env(pg, tmp_path, **_env_kwargs(case)) as env:
        env.cli.scripts[:] = scripts_for(case["results"], "pass")
        await env.import_plan()
        run = await env.compile()
        await env.app.job_manager.connector.execute_query_async(
            "DELETE FROM procrastinate_jobs WHERE status = 'todo'"
        )
        await env.rows("DELETE FROM job_keys RETURNING key")
        runs = need("reader.pipeline.runs")
        assert (await runs.derive(env.engine, env.jobs, env.users["ivan"], run))["state"] == "stalled"
        status = await env.state(run)  # the status read calls ensure
        assert status["state"] == "running"
        for _ in range(3):
            await env.work()
        assert (await env.state(run))["state"] == "converged"


async def test_one_run_per_lesson(pg: Pg, tmp_path: Path) -> None:
    errors = need("reader.pipeline.errors")
    async with pipeline_env(pg, tmp_path) as env:
        await env.import_plan()
        await env.compile()
        with pytest.raises(errors.PipelineError) as refused:
            await env.compile()
        assert refused.value.code == "compile_running"


async def test_source_comes_from_the_lessons_page_range(pg: Pg, tmp_path: Path) -> None:
    source = need("reader.pipeline.source")
    async with pipeline_env(pg, tmp_path) as env:
        await env.import_plan()
        async with env.engine.connect() as conn:
            spans, title = await source.DbSpanReader().load(conn, env.users["ivan"], MATERIAL, 61, 61)
        assert title == "synthetic-1"
        assert [s.span_id for s in spans] == [f"{MATERIAL}:{i}" for i in range(3)]
        assert all(s.page == 61 for s in spans)
        payload = json.dumps([s.text for s in spans])
        assert CANARY in payload
