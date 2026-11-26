# @trace FR-PIPELINE-5, FR-PIPELINE-23
"""The loop rule and the stale-step rules on the real job machinery (ADR-0039 decision 22, SRV-AGENT FR-AGENT-23).
[db]

A loop starts only when SRV-AGENT's `loop_fits_limit` admits the summed reserve of its calls; a refusal records
`loop_cap_<limit>`, runs `findings` and starts no call. The check's row and its decision commit together; a model
step of a refused run and a findings job of a run whose decision admitted a loop do nothing; `ensure` re-enqueues
the recorded next step and never asks `loop_fits_limit` again.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from .conftest import FIXTURES, Pg, load_yaml, need, pipeline_env, scripts_for

pytestmark = pytest.mark.anyio
DEFAULTS = load_yaml(FIXTURES / "loop-cases.yaml")["defaults"]
FACT_FAILS = [
    {"stage": "draft", "cost": 0.6},
    {"stage": "fact_check", "cost": 0.24, "unsupported": 1, "contradicted": 0},
]


class Recorder:
    """A fake `loop_fits_limit`: records each planned list and answers from `answers` (None: the loop fits)."""

    def __init__(self, *answers: str | None) -> None:
        self.answers = list(answers)
        self.calls: list[list[Any]] = []

    async def __call__(self, owner_id: Any, run_id: Any, cap: Any, planned: Any) -> str | None:
        self.calls.append(list(planned))
        return self.answers.pop(0) if self.answers else None


def _dropping(env: Any, stage: str) -> list[dict[str, Any]]:
    """Hold back every enqueue of `stage` (a delayed or lost enqueue); returns the list of held args."""
    held: list[dict[str, Any]] = []
    real = env.jobs.enqueue

    async def enqueue(kind: str, args: Any, schedule_in: int = 0) -> Any:
        if dict(args).get("stage") == stage:
            held.append(dict(args))
            return None
        return await real(kind, args, schedule_in)

    env.jobs.enqueue = enqueue
    return held


async def _start(env: Any, results: list[dict[str, Any]], cap: str = "8.00") -> uuid.UUID:
    env.cli.scripts[:] = scripts_for(results, "pass")
    await env.import_plan()
    run = await env.compile(cap)
    for _ in range(6):
        await env.work()
    return run


async def _stages(env: Any, run: uuid.UUID) -> list[str]:
    return [r[0] for r in await env.rows("SELECT stage FROM compile_steps WHERE run_id = :r ORDER BY step", r=run)]


async def _reserves(env: Any, run: uuid.UUID) -> int:
    return int(await env.scalar("SELECT count(*) FROM agent_spend WHERE run_id = :r AND kind = 'reserve'", r=run))


async def test_planned_calls_and_first_pass(pg: Pg, tmp_path: Path) -> None:
    """FR-PIPELINE-23: the first pass never asks; a failed check plans revise (task draft), fact_check and review
    (section mode: no judge.find) and records the planned sum; the fake's `None` starts the loop."""
    async with pipeline_env(pg, tmp_path, floors=DEFAULTS["reserves"]) as env:
        fake = Recorder(None)
        env.runtime.provider.loop_fits_limit = fake
        held = _dropping(env, "revise")
        run = await _start(env, FACT_FAILS)
        assert [[p.task for p in planned] for planned in fake.calls] == [["draft", "fact_check", "review"]]
        assert await _stages(env, run) == ["draft", "rules", "lab_verification", "fact_check"]
        report = await env.scalar("SELECT report FROM compile_steps WHERE run_id = :r AND stage = 'fact_check'", r=run)
        assert report["next"] == "revise" and Decimal(report["planned_usd"]) == Decimal("2.27")
        assert report["planned_judge_usd"] == "0"
        assert await env.scalar("SELECT loops FROM compile_runs WHERE id = :r", r=run) == 1
        assert held and {h["stage"] for h in held} == {"revise"}


@pytest.mark.parametrize(("cap", "starts"), [("3.11", True), ("3.10", False)])
async def test_a_loop_starts_only_when_its_sum_fits(pg: Pg, tmp_path: Path, cap: str, starts: bool) -> None:
    """Spent 0.84 + the loop's 2.27 = 3.11: fits exactly at 3.11; at 3.10 the loop does not start (no revise, no
    new reserve row), budget_refused loop_cap_run, findings ran, state budget_exhausted."""
    async with pipeline_env(pg, tmp_path, floors=DEFAULTS["reserves"]) as env:
        held = _dropping(env, "revise")
        run = await _start(env, FACT_FAILS, cap)
        state = await env.state(run)
        if starts:  # held: the check's enqueue and each worker start's ensure of the recorded revise
            assert held and {h["stage"] for h in held} == {"revise"} and state["refusals"] == []
            return
        assert held == []
        assert await _stages(env, run) == ["draft", "rules", "lab_verification", "fact_check", "findings"]
        assert await _reserves(env, run) == 2  # draft and fact_check only
        assert state["state"] == "budget_exhausted" and state["refusals"] == ["loop_cap_run"]
        assert state["stop"] == "loop_cap" and state["refused_limit"] == "run"


async def test_a_task_budget_crossing_records_loop_cap_task(pg: Pg, tmp_path: Path) -> None:
    budgets = {"draft": 4.0, "fact_check": 2.0, "review": 0.7}
    async with pipeline_env(pg, tmp_path, task_budgets=budgets, floors=DEFAULTS["reserves"]) as env:
        run = await _start(env, FACT_FAILS)
        state = await env.state(run)
        assert state["refusals"] == ["loop_cap_task"] and state["refused_limit"] == "task"


async def test_stale_steps_and_ensure(pg: Pg, tmp_path: Path) -> None:
    """FR-PIPELINE-5 (stale steps): ensure re-enqueues the recorded revise without asking loop_fits_limit again;
    a findings job while the decision admits a loop does nothing; a revise job of a run refused meanwhile reserves,
    calls and writes nothing and enqueues findings."""
    runs = need("reader.pipeline.runs")
    async with pipeline_env(pg, tmp_path, floors=DEFAULTS["reserves"]) as env:
        fake = Recorder(None)
        env.runtime.provider.loop_fits_limit = fake
        held = _dropping(env, "revise")
        run = await _start(env, FACT_FAILS)
        assert len(fake.calls) == 1 and held  # worker starts re-enqueue the recorded revise; no second ask
        # a stale findings job enqueued while the recorded decision admits a loop: no effect
        async with env.engine.connect() as conn:
            row = await runs.load_run(conn, run)
            steps = await runs.load_steps(conn, run)
            lesson = await runs.load_lesson(conn, env.runtime, row.owner_id, row.lesson_id)
        findings_args = runs.step_args(row, len(steps) + 1, "findings", runs.step_input(env.runtime, lesson, steps, "findings"))
        env.jobs.enqueue = type(env.jobs).enqueue.__get__(env.jobs)  # stop holding enqueues
        await env.jobs.enqueue("pipeline.stage", findings_args)
        await env.work()
        assert await _stages(env, run) == ["draft", "rules", "lab_verification", "fact_check"]
        assert await env.scalar("SELECT finished_at FROM compile_runs WHERE id = :r", r=run) is None
        # ensure: the recorded `next` (revise) is enqueued again, loop_fits_limit is not asked again
        status = await env.state(run)
        assert status["state"] == "running" and len(fake.calls) == 1
        # the revise job runs only after a refusal was recorded: it does nothing but enqueue findings
        from sqlalchemy import text

        async with env.engine.begin() as conn:
            await conn.execute(text("UPDATE compile_runs SET budget_refused = 'budget_exhausted' WHERE id = :r"), {"r": run})
        before = await _reserves(env, run)
        for _ in range(3):
            await env.work()
        assert await _reserves(env, run) == before
        assert await _stages(env, run) == ["draft", "rules", "lab_verification", "fact_check", "findings"]
        assert (await env.state(run))["state"] == "budget_exhausted"


async def test_a_refused_step_with_a_lost_findings_enqueue(pg: Pg, tmp_path: Path) -> None:
    """Round 5: a model step refused for budget writes no row; when its findings enqueue is lost, ensure enqueues
    findings (not the refused step) and never calls loop_fits_limit."""
    budgets = {"draft": 4.0, "fact_check": 0.3, "review": 4.0}  # the fact_check reserve 0.38 > 0.30
    async with pipeline_env(pg, tmp_path, task_budgets=budgets, floors=DEFAULTS["reserves"]) as env:
        fake = Recorder()
        env.runtime.provider.loop_fits_limit = fake
        held = _dropping(env, "findings")
        run = await _start(env, [{"stage": "draft", "cost": 0.6}])
        assert held and {h["stage"] for h in held} == {"findings"}
        assert await _stages(env, run) == ["draft", "rules", "lab_verification"]  # no fact_check row
        env.jobs.enqueue = type(env.jobs).enqueue.__get__(env.jobs)
        status = await env.state(run)  # ensure
        pending = status["steps"][-1]
        assert pending["stage"] == "findings" and status["state"] == "running"
        for _ in range(3):
            await env.work()
        assert (await env.state(run))["state"] == "budget_exhausted"
        assert fake.calls == []


async def test_the_decision_commits_with_the_row(pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A check whose transaction fails after its loop decision leaves neither its row nor loops + 1."""
    steps = need("reader.pipeline.steps")
    async with pipeline_env(pg, tmp_path, floors=DEFAULTS["reserves"]) as env:
        real = steps.write_revision

        async def failing(conn: Any, root: Any, owner: Any, lesson: Any, data: Any, stage: str, prov: Any) -> Any:
            if stage == "fact_check":
                raise RuntimeError("write failed")
            return await real(conn, root, owner, lesson, data, stage, prov)

        monkeypatch.setattr(steps, "write_revision", failing)
        run = await _start(env, [*FACT_FAILS, *FACT_FAILS[1:], *FACT_FAILS[1:]])
        assert "fact_check" not in await _stages(env, run)
        assert await env.scalar("SELECT loops FROM compile_runs WHERE id = :r", r=run) == 0
