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


REFUSING = {"draft": 4.0, "fact_check": 0.3, "review": 4.0}  # the fact_check reserve 0.38 > 0.30


async def test_a_refusal_survives_a_rolled_back_step(
    pg: Pg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The refused step's transaction is lost after its refusal and findings enqueue; the refusal was already
    committed on its own connection (read from another connection before the loss), so the retry takes the
    stale-step branch (no second refusal, no reservation); the findings ran once, the run ends budget_exhausted
    and counts one end."""
    import logging

    from sqlalchemy import text

    steps = need("reader.pipeline.steps")
    errors = need("reader.pipeline.errors")
    caplog.set_level(logging.INFO, logger="reader.pipeline")
    async with pipeline_env(pg, tmp_path, task_budgets=REFUSING, floors=DEFAULTS["reserves"]) as env:
        real = steps._refused
        lost: list[int] = []
        seen: list[Any] = []

        async def refused_then_lost(*args: Any) -> None:
            await real(*args)
            async with env.engine.connect() as other:  # a separate connection sees the committed refusal
                query = text("SELECT budget_refused FROM compile_runs WHERE id = :r")
                seen.append((await other.execute(query, {"r": args[3].id})).scalar())
            lost.append(1)
            raise errors.InputNotReady()  # the job transaction rolls back; the job is retried

        monkeypatch.setattr(steps, "_refused", refused_then_lost)
        run = await _start(env, [{"stage": "draft", "cost": 0.6}])
        assert lost == [1]  # the retry never reached the model call, so it never refused again
        assert seen == ["task_budget_exhausted"]
        # the retry (attempt 2) returned early: by the stale-step branch, or, when the findings job ran first, by
        # the already-recorded branch; either way no reservation and no second refusal
        outcome = await env.scalar(
            "SELECT outcome FROM job_keys WHERE args->>'run_id' = :r AND args->>'stage' = 'fact_check'", r=str(run)
        )
        assert outcome == "succeeded"  # the retried job completed without reaching the model call
        assert await env.scalar("SELECT ends FROM compile_runs WHERE id = :r", r=run) == 1
        state = await env.state(run)
        assert state["state"] == "budget_exhausted" and state["refusals"] == ["task_budget_exhausted"]
        assert await _stages(env, run) == ["draft", "rules", "lab_verification", "findings"]
        assert await _reserves(env, run) == 1  # the draft's only: neither attempt of fact_check reserved


async def test_a_stale_findings_job_does_not_consume_its_key(pg: Pg, tmp_path: Path) -> None:
    """The coordinator's sequence: a findings job (keyed with the refusal code) runs while the run shows no
    refusal yet: it has no effect and does not complete its key; when the step is then refused with that same
    code, findings runs and the run ends budget_exhausted."""
    runs = need("reader.pipeline.runs")
    async with pipeline_env(pg, tmp_path, task_budgets=REFUSING, floors=DEFAULTS["reserves"]) as env:
        held = _dropping(env, "fact_check")  # the fact_check job has not run yet
        run = await _start(env, [{"stage": "draft", "cost": 0.6}])
        assert await _stages(env, run) == ["draft", "rules", "lab_verification"]
        async with env.engine.connect() as conn:
            row = await runs.load_run(conn, run)
            done = await runs.load_steps(conn, run)
            lesson = await runs.load_lesson(conn, env.runtime, row.owner_id, row.lesson_id)
        code = "task_budget_exhausted"
        findings = runs.step_args(
            row, len(done) + 1, "findings", runs.step_input(env.runtime, lesson, done, "findings", code)
        )
        env.jobs.enqueue = type(env.jobs).enqueue.__get__(env.jobs)
        await env.jobs.enqueue("pipeline.stage", findings)
        await env.work()  # the stale findings job: input_not_ready, no effect
        assert await _stages(env, run) == ["draft", "rules", "lab_verification"]
        await env.jobs.enqueue("pipeline.stage", held[0])  # the fact_check step, refused with that code
        for _ in range(4):
            await env.work()
        assert (await env.state(run))["state"] == "budget_exhausted"
        assert await _stages(env, run) == ["draft", "rules", "lab_verification", "findings"]


async def test_planned_sum_equals_the_reserves_written(pg: Pg, tmp_path: Path) -> None:
    """FR-PIPELINE-23 with custom_system (as every stage call): with floors below the estimate, the planned sum
    of a loop equals the sum of the reserve rows its three calls write."""
    floors = {stage: 0.0001 for stage in DEFAULTS["reserves"]}
    results = [*FACT_FAILS, {"stage": "revise", "cost": 0.87},
               {"stage": "fact_check", "cost": 0.24, "unsupported": 0, "contradicted": 0},
               {"stage": "adversarial_review", "cost": 0.5, "blocking": 0, "nonblocking": 0}]
    async with pipeline_env(pg, tmp_path, floors=floors) as env:
        run = await _start(env, results)
        assert (await env.state(run))["state"] == "converged"
        report = await env.scalar(
            "SELECT report FROM compile_steps WHERE run_id = :r AND stage = 'fact_check' AND step = 4", r=run
        )
        planned = Decimal(report["planned_usd"])
        reserved = Decimal(await env.scalar(
            "SELECT sum(usd) FROM agent_spend WHERE run_id = :r AND kind = 'reserve'"
            " AND stage IN ('revise', 'adversarial_review')"
            " OR (run_id = :r AND kind = 'reserve' AND stage = 'fact_check' AND id = (SELECT max(id)"
            " FROM agent_spend WHERE run_id = :r AND kind = 'reserve' AND stage = 'fact_check'))", r=run
        ))
        assert planned > Decimal("0.0003")  # the estimate, not the floors
        assert planned == reserved
