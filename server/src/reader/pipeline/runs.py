"""Compile runs: their persisted steps, step inputs, the derived state and `ensure` (FR-PIPELINE-5, -6, -11).

A run's state is never stored. It is derived from its steps and the job status of its next step's key:
`running` (queued or running), `stage_failed` (dead), `stalled` (no live job, a lost enqueue); a finished
run is `budget_exhausted`, `converged` or `open_findings`; a cancelled one is `cancelled` (FR-PIPELINE-20,
checked first). `ensure` re-enqueues the next step of a stalled run; the key makes it idempotent.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from reader.agent import ledger
from reader.agent.errors import AgentError
from reader.agent.pricing import resolve_model
from reader.config.effective import effective_settings
from reader.jobs import audit, idempotency_key
from reader.jobs.queue import dead_error_code_of
from reader.lessons.store import current_sha256
from reader.pipeline import runtime as runtime_mod
from reader.pipeline.courses import lock_course
from reader.pipeline.errors import PipelineError
from reader.pipeline.prompts import canonical_sha256, input_sha256
from reader.pipeline.runtime import Runtime
from reader.pipeline.source import Source, render
from reader.pipeline.stages import Done, next_stage

# FR-PIPELINE-16: the permanent SRV-AGENT codes a stopped step may report (FR-AGENT-10 and the
# ledger's two refusals).
STAGE_ERROR_CODES: Final = frozenset(
    {
        "sdk_error",
        "tools_mismatch",
        "unknown_model",
        "provider_missing",
        "usage_unmapped",
        "settle_refused",
        "void_refused",
    }
)
OPEN_RUNS_MAX: Final = 100  # bounds.open_runs_max (FR-PIPELINE-17)
KIND_STAGE: Final = "pipeline.stage"
KIND_VERIFY: Final = "pipeline.verify"
NOTEBOOK_STAGES: Final = frozenset({"draft", "revise"})
# pg_advisory_xact_lock(namespace, hashtext(lesson)): one run per lesson at a time ("RCMP").
LOCK_NAMESPACE: Final = 0x52434D50


def kind_for(stage: str) -> str:
    return KIND_VERIFY if stage == "lab_verification" else KIND_STAGE


@dataclass(frozen=True)
class StepRow:
    id: int
    step: int
    stage: str
    input_sha256: str
    output_sha256: str | None
    report: Mapping[str, Any]
    reused_from: int | None
    model: str | None
    cost_usd: Decimal
    duration_s: Decimal


@dataclass(frozen=True)
class RunRow:
    id: uuid.UUID
    owner_id: uuid.UUID
    lesson_id: uuid.UUID
    cap_usd: Decimal
    task_budgets: Mapping[str, str]
    budget_refused: str | None
    finished_at: Any
    g2: Mapping[str, Any] | None
    final_sha256: str | None
    stage_error: Mapping[str, Any] | None = None  # FR-PIPELINE-16: {step, input_sha256, code}
    cancelled_at: Any = None  # FR-PIPELINE-20


@dataclass(frozen=True)
class Lesson:
    source: Source
    title: str
    material_title: str
    course_id: uuid.UUID | None = None  # SRV-OBS FR-OBS-12 (d): an id for the trace, never the title


async def load_run(
    conn: AsyncConnection, run_id: uuid.UUID, owner_id: uuid.UUID | None = None
) -> RunRow | None:
    row = (
        await conn.execute(
            text(
                "SELECT id, owner_id, lesson_id, cap_usd, task_budgets, budget_refused, finished_at, g2,"
                " final_sha256, stage_error, cancelled_at FROM compile_runs WHERE id = :r"
            ),
            {"r": run_id},
        )
    ).first()
    if row is None or (owner_id is not None and row[1] != owner_id):
        return None
    return RunRow(
        row[0], row[1], row[2], Decimal(row[3]), row[4] or {}, row[5], row[6], row[7], row[8], row[9], row[10]
    )


async def lesson_gate(
    conn: AsyncConnection, owner_id: uuid.UUID, lesson_id: uuid.UUID, *, active: bool
) -> None:
    """FR-PIPELINE-22 / SRV-LESSON INV-LESSON-22: a deleted lesson, or a lesson of a deleted course, is
    `not_found`; with `active`, a skipped lesson or one of an archived course is `lesson_inactive`. A missing
    lesson passes (the callers' own reads refuse it)."""
    row = (
        await conn.execute(
            text(
                "SELECT l.deleted_at IS NOT NULL OR c.deleted_at IS NOT NULL,"
                " l.skipped_at IS NOT NULL OR c.archived_at IS NOT NULL"
                " FROM lessons l JOIN courses c ON c.id = l.course_id WHERE l.id = :l AND l.owner_id = :o"
            ),
            {"l": lesson_id, "o": owner_id},
        )
    ).first()
    if row is not None and row[0]:
        raise PipelineError("not_found")
    if row is not None and active and row[1]:
        raise PipelineError("lesson_inactive")


async def load_steps(conn: AsyncConnection, run_id: uuid.UUID) -> list[StepRow]:
    rows = await conn.execute(
        text(
            "SELECT id, step, stage, input_sha256, output_sha256, report, reused_from, model, cost_usd,"
            " duration_s FROM compile_steps WHERE run_id = :r ORDER BY step"
        ),
        {"r": run_id},
    )
    return [
        StepRow(r[0], r[1], r[2], r[3], r[4], r[5] or {}, r[6], r[7], Decimal(r[8]), Decimal(r[9]))
        for r in rows
    ]


async def load_lesson(
    conn: AsyncConnection, rt: Runtime, owner_id: uuid.UUID, lesson_id: uuid.UUID
) -> Lesson:
    plan = (
        await conn.execute(
            text(
                "SELECT p.material_sha256, p.pdf_first, p.pdf_last, l.title, c.title, c.id"
                " FROM course_plans p JOIN lessons l ON l.id = p.lesson_id"
                " JOIN courses c ON c.id = l.course_id"
                " WHERE p.lesson_id = :l AND p.owner_id = :o"
            ),
            {"l": lesson_id, "o": owner_id},
        )
    ).first()
    if plan is None:
        raise PipelineError("lesson_unknown")
    await lesson_gate(conn, owner_id, lesson_id, active=True)  # FR-PIPELINE-22, read after create_run's locks
    spans, _ = await rt.spans.load(conn, owner_id, plan[0], plan[1], plan[2])
    # citations name the course title (as SRV-TUTOR); the course id is for the trace only
    course_id = plan[5] if len(plan) > 5 and isinstance(plan[5], uuid.UUID) else None
    return Lesson(render(spans), str(plan[3]), str(plan[4]), course_id)


def done_list(steps: Sequence[StepRow]) -> list[Done]:
    out = []
    for s in steps:
        if s.stage == "fact_check":
            problems = any(v["verdict"] != "supported" for v in s.report.get("verdicts", []))
        elif s.stage == "adversarial_review":
            problems = any(f["blocking"] for f in s.report.get("findings", []))
        else:
            problems = False
        out.append(Done(s.stage, problems))
    return out


def model_sha(steps: Sequence[StepRow]) -> str | None:
    """The last model-written notebook (draft or revise)."""
    return next((s.output_sha256 for s in reversed(steps) if s.stage in NOTEBOOK_STAGES), None)


def latest_sha(steps: Sequence[StepRow]) -> str | None:
    return next((s.output_sha256 for s in reversed(steps) if s.output_sha256), None)


def last_report(steps: Sequence[StepRow], stage: str) -> Mapping[str, Any] | None:
    return next((s.report for s in reversed(steps) if s.stage == stage), None)


def stage_model(rt: Runtime, stage: str) -> str | None:
    task_name = rt.pipeline.stages[stage].task
    if task_name is None:
        return None
    task = rt.config.tasks.get(task_name)
    return resolve_model(task.model) if task else None


def step_input(rt: Runtime, lesson: Lesson, steps: Sequence[StepRow], stage: str) -> str:
    """FR-PIPELINE-3: sha256 over (stage, source, the stage's inputs, prompt version and content, model)."""
    m = model_sha(steps) or ""
    fact, review = last_report(steps, "fact_check"), last_report(steps, "adversarial_review")
    reports = [canonical_sha256(fact) if fact else "", canonical_sha256(review) if review else ""]
    if stage == "draft":
        inputs = [canonical_sha256(lesson.title)]
    elif stage in ("fact_check", "adversarial_review"):
        inputs = [m]
    elif stage in ("revise", "findings"):
        inputs = [m, *reports]
    else:
        inputs = [latest_sha(steps) or ""]
    model = stage_model(rt, stage)
    prompts = rt.prompts if model is not None else None
    return input_sha256(stage, lesson.source.sha256, inputs, prompts, model)


def next_step(rt: Runtime, run: RunRow, steps: Sequence[StepRow]) -> str | None:
    return next_stage(rt.pipeline, done_list(steps), refused=bool(run.budget_refused))


def step_args(run: RunRow, step: int, stage: str, input_sha: str) -> dict[str, Any]:
    return {"run_id": str(run.id), "step": step, "stage": stage, "input_sha256": input_sha}


async def enqueue_next(jobs: Any, rt: Runtime, run: RunRow, lesson: Lesson, steps: Sequence[StepRow]) -> None:
    stage = next_step(rt, run, steps)
    if stage is not None:
        args = step_args(run, len(steps) + 1, stage, step_input(rt, lesson, steps, stage))
        await jobs.enqueue(kind_for(stage), args)


def _str(value: Decimal) -> str:
    return str(value)


async def derive(engine: AsyncEngine, jobs: Any, owner_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
    rt = runtime_mod.get(engine)
    async with engine.connect() as conn:
        run = await load_run(conn, run_id, owner_id)
        if run is None:
            raise PipelineError("not_found")
        await lesson_gate(conn, owner_id, run.lesson_id, active=False)  # a deleted lesson's runs: not found
        steps = await load_steps(conn, run_id)
        latest = await current_sha256(conn, run.lesson_id)
        pending: dict[str, Any] | None = None
        error: str | None = None
        if run.cancelled_at is not None:  # FR-PIPELINE-20: checked first; no ensure, no requeue
            state = "cancelled"
        elif run.finished_at is not None:
            g2 = run.g2 or {}
            if run.budget_refused:
                state = "budget_exhausted"
            elif g2.get("blocking_open", 0) == 0:
                state = "converged"
            else:
                state = "open_findings"
        else:
            state = "stalled"
            stage = next_step(rt, run, steps)
            if stage is not None:
                try:
                    lesson = await load_lesson(conn, rt, owner_id, run.lesson_id)
                    args = step_args(run, len(steps) + 1, stage, step_input(rt, lesson, steps, stage))
                except (PipelineError, AgentError):  # unloadable lesson or unresolvable model: no args
                    args = None
                status = "none"
                if args is not None:
                    kind = kind_for(stage)
                    key = idempotency_key(jobs.catalog[kind], args)
                    status = await jobs.job_status(kind, key)
                    if status == "dead":
                        error = await _stage_error(conn, run, args, kind, key)
                state = {"queued": "running", "running": "running", "dead": "stage_failed"}.get(
                    status, "stalled"
                )
                pending = {
                    "step": len(steps) + 1,
                    "stage": stage,
                    "status": status,
                    "reused": False,
                    "args": args,
                }
    spent = await ledger.spent(engine, run_id=run_id)
    rows = [
        {
            "step": s.step,
            "stage": s.stage,
            "model": s.model,
            "cost_usd": _str(s.cost_usd),
            "duration_s": float(s.duration_s),
            "reused": s.reused_from is not None,
        }
        for s in steps
    ]
    if pending is not None:
        rows.append({k: v for k, v in pending.items() if k != "args"})
    return {
        "run_id": str(run.id),
        "lesson_id": str(run.lesson_id),
        "state": state,
        "cap_usd": _str(run.cap_usd),
        "steps": rows,
        "spent_usd": _str(spent),
        "task_budgets": dict(run.task_budgets),
        "refusals": [run.budget_refused] if run.budget_refused else [],
        "g2": run.g2,
        "latest_sha256": latest,
        "error": error,
        "_pending": pending,
    }


async def _stage_error(
    conn: AsyncConnection, run: RunRow, args: Mapping[str, Any], kind: str, key: str
) -> str | None:
    """FR-PIPELINE-16: the recorded code, only for this step and input and a key dead of an AgentError."""
    record = run.stage_error
    if not record or record.get("step") != args["step"] or record.get("input_sha256") != args["input_sha256"]:
        return None
    code = record.get("code")
    if code not in STAGE_ERROR_CODES:
        return None
    if await dead_error_code_of(conn, kind, key) != "AgentError":
        return None
    return str(code)


async def record_stage_error(engine: AsyncEngine, run_id: uuid.UUID, value: Mapping[str, Any] | None) -> None:
    """FR-PIPELINE-16: written on its own connection, committed at once (the FR-AGENT-9 allowance)."""
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE compile_runs SET stage_error = CAST(:e AS jsonb) WHERE id = :r"),
            {"e": None if value is None else json.dumps(dict(value)), "r": run_id},
        )


async def open_runs(engine: AsyncEngine, jobs: Any, owner_id: uuid.UUID) -> list[dict[str, Any]]:
    """FR-PIPELINE-17: the latest run of each lesson, unfinished or budget-stopped, newest first, bounded."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT id FROM (SELECT DISTINCT ON (lesson_id) id, created_at, finished_at,"
                    " budget_refused, cancelled_at FROM compile_runs WHERE owner_id = :o"
                    " ORDER BY lesson_id, created_at DESC, id) latest"  # ties: id ascending, as latest_run
                    " WHERE (finished_at IS NULL OR budget_refused IS NOT NULL)"
                    " AND cancelled_at IS NULL"  # FR-PIPELINE-20: a cancelled run is not open
                    " ORDER BY created_at DESC, id LIMIT :n"
                ),
                {"o": owner_id, "n": OPEN_RUNS_MAX},
            )
        ).all()
    out = []
    for (run_id,) in rows:
        try:
            result = await status(engine, jobs, owner_id, run_id)
        except PipelineError:
            continue
        if result["state"] not in ("converged", "open_findings"):
            out.append(result)
    return out


async def ensure(engine: AsyncEngine, jobs: Any, owner_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
    """Re-enqueue the next step of a stalled run (idempotent by key), then return its derived status."""
    status = await derive(engine, jobs, owner_id, run_id)
    pending = status["_pending"]
    if status["state"] == "stalled" and pending is not None and pending["args"] is not None:
        await jobs.enqueue(kind_for(pending["stage"]), pending["args"])
        status = await derive(engine, jobs, owner_id, run_id)
    return status


async def status(engine: AsyncEngine, jobs: Any, owner_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
    result = await ensure(engine, jobs, owner_id, run_id)
    result.pop("_pending", None)
    return result


async def cancel(engine: AsyncEngine, jobs: Any, owner_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
    """FR-PIPELINE-20: one UPDATE on its own connection; it waits only on the run row's lock, which a step
    holds from its second cancel read to its commit. Idempotent: a cancelled run answers its body again."""
    async with engine.begin() as conn:
        lesson = (
            await conn.execute(
                text(
                    "UPDATE compile_runs SET cancelled_at = now(), finished_at = now() WHERE id = :r"
                    " AND owner_id = :o AND finished_at IS NULL AND cancelled_at IS NULL RETURNING lesson_id"
                ),
                {"r": run_id, "o": owner_id},
            )
        ).scalar()
        if lesson is not None:
            await audit.record(conn, owner_id, "compile.cancel", str(run_id), {"lesson_id": str(lesson)})
    if lesson is None:
        async with engine.connect() as conn:
            run = await load_run(conn, run_id, owner_id)
        if run is None:
            raise PipelineError("not_found")
        if run.cancelled_at is None:
            raise PipelineError("not_running")
    result = await derive(engine, jobs, owner_id, run_id)
    result.pop("_pending", None)
    return result


HISTORY_MAX: Final = 20  # FR-PIPELINE-21


async def history(
    engine: AsyncEngine, jobs: Any, owner_id: uuid.UUID, lesson_id: uuid.UUID
) -> list[dict[str, Any]]:
    """FR-PIPELINE-21: the lesson's newest runs, read-only (derive, never ensure)."""
    async with engine.connect() as conn:
        known = (
            await conn.execute(
                text("SELECT 1 FROM lessons WHERE id = :l AND owner_id = :o"), {"l": lesson_id, "o": owner_id}
            )
        ).first()
        if known is None:
            raise PipelineError("not_found")
        await lesson_gate(conn, owner_id, lesson_id, active=False)
        rows = (
            await conn.execute(
                text(
                    "SELECT r.id, r.created_at, r.finished_at,"
                    " (SELECT count(*) FROM compile_steps s WHERE s.run_id = r.id) FROM compile_runs r"
                    " WHERE r.lesson_id = :l AND r.owner_id = :o ORDER BY r.created_at DESC, r.id LIMIT :n"
                ),
                {"l": lesson_id, "o": owner_id, "n": HISTORY_MAX},
            )
        ).all()
    out = []
    for run_id, created_at, finished_at, steps in rows:
        derived = await derive(engine, jobs, owner_id, run_id)
        out.append(
            {
                "run_id": str(run_id),
                "state": derived["state"],
                "created_at": created_at.isoformat(),
                "finished_at": None if finished_at is None else finished_at.isoformat(),
                "spent_usd": derived["spent_usd"],
                "cap_usd": derived["cap_usd"],
                "steps": int(steps),
                "error": derived["error"],
            }
        )
    return out


async def ensure_all(engine: AsyncEngine, jobs: Any) -> None:
    """At worker start: every unfinished run (FR-PIPELINE-6)."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT id, owner_id FROM compile_runs WHERE finished_at IS NULL"))
        ).all()
    for run_id, owner_id in rows:
        try:
            await ensure(engine, jobs, owner_id, run_id)
        except PipelineError:
            continue


async def task_budgets(conn: AsyncConnection, rt: Runtime) -> dict[str, str]:
    """The run's snapshot of the per-lesson task budgets: reader.yaml plus the owner's overrides at run
    creation (SRV-CONFIG FR-CONFIG-26, paired); a running run keeps it."""
    settings = await effective_settings(conn, rt.config)
    stages = {s.task for s in rt.pipeline.stages.values()}
    budgets = settings.task_budgets.items()
    return {name: str(budget) for name, budget in budgets if budget is not None and name in stages}


async def create_run(
    engine: AsyncEngine,
    jobs: Any,
    owner_id: uuid.UUID,
    lesson_id: uuid.UUID,
    cap_usd: Decimal,
    *,
    require_course_unstarted: bool = False,
) -> uuid.UUID:
    """A new run of the lesson; `compile_running` while another is running or stalled (FR-PIPELINE-11).

    FR-PIPELINE-15: the lesson's course lock first, then the lesson lock; the lesson is read after both, so a
    lesson removed by a concurrent re-plan is `lesson_unknown`. With `require_course_unstarted`, any run in
    the same plan course is `already_started`.
    """
    rt = runtime_mod.get(engine)
    async with engine.begin() as conn:
        course = (
            await conn.execute(
                text("SELECT plan_course_id FROM course_plans WHERE lesson_id = :l AND owner_id = :o"),
                {"l": lesson_id, "o": owner_id},
            )
        ).scalar()
        if course is not None:
            await lock_course(conn, owner_id, str(course))
        await conn.execute(
            text("SELECT pg_advisory_xact_lock(:ns, hashtext(:l))"),
            {"ns": LOCK_NAMESPACE, "l": str(lesson_id)},
        )
        lesson = await load_lesson(conn, rt, owner_id, lesson_id)
        if require_course_unstarted and course is not None:
            other = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM compile_runs r JOIN course_plans p ON p.lesson_id = r.lesson_id"
                        " WHERE p.owner_id = :o AND p.plan_course_id = :c LIMIT 1"
                    ),
                    {"o": owner_id, "c": course},
                )
            ).first()
            if other is not None:
                raise PipelineError("already_started")
        unfinished = (
            await conn.execute(
                text("SELECT id FROM compile_runs WHERE lesson_id = :l AND finished_at IS NULL"),
                {"l": lesson_id},
            )
        ).all()
        for (other,) in unfinished:
            if (await derive(engine, jobs, owner_id, other))["state"] in ("running", "stalled"):
                raise PipelineError("compile_running")
        budgets = await task_budgets(conn, rt)
        run_id: uuid.UUID = (
            await conn.execute(
                text(
                    "INSERT INTO compile_runs (owner_id, lesson_id, cap_usd, task_budgets, base_sha256)"
                    " VALUES (:o, :l, :c, CAST(:t AS jsonb), :b) RETURNING id"
                ),
                {
                    "o": owner_id,
                    "l": lesson_id,
                    "c": cap_usd,
                    "t": json.dumps(budgets),
                    "b": await current_sha256(conn, lesson_id),
                },
            )
        ).scalar_one()
    run = RunRow(run_id, owner_id, lesson_id, cap_usd, budgets, None, None, None, None)
    await enqueue_next(jobs, rt, run, lesson, [])
    return run_id
